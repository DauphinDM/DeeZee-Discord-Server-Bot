"""Moderation commands.

Covers 32 commands from the command sheet: bans, kicks, mutes, warnings, notes,
cases, purging, channel locks and voice moderation.

Every command that acts against a member routes through
``core.permissions.enforce_hierarchy`` first. No command performs its own
permission reasoning -- a check written thirty-two times is a check that is
wrong in one of them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import modlog, permissions
from core.constants import (
    BULK_ACTION_DELAY,
    BULK_DELETE_MAX_AGE_DAYS,
    COLOUR_DEFAULT,
    COLOUR_ERROR,
    COLOUR_INFO,
    COLOUR_MUTED,
    COLOUR_SUCCESS,
    COLOUR_WARNING,
    EMOJI_SUCCESS,
    EMOJI_WARNING,
    MAX_PURGE_LIMIT,
    MAX_TIMEOUT_DAYS,
    TS_LONG_DATE_TIME,
    TS_RELATIVE,
)
from core.errors import DeezeeError, NotConfigured
from core.modlog import Case
from services.timeparse import (
    format_duration,
    parse_duration,
    split_duration_and_reason,
)
from ui.paginator import Paginator, paginate_fields, paginate_lines
from ui.views import BaseView, confirm

log = logging.getLogger(__name__)

#: Default text DMed to a banned member when no ban message is configured.
DEFAULT_BAN_MESSAGE = "You have been banned from {server}.\n**Reason:** {reason}"

#: Actions that count as an active mute for lookup purposes.
_MUTE_ACTIONS = ("mute",)
_BAN_ACTIONS = ("ban", "tempban")


class MassbanModal(discord.ui.Modal, title="Mass ban"):
    """Collects a newline-separated ID list.

    A modal rather than a command argument because a hundred IDs pasted into a
    message is unreadable, gets truncated at 2000 characters, and cannot be
    edited before submitting.
    """

    ids = discord.ui.TextInput(
        label="User IDs",
        style=discord.TextStyle.paragraph,
        placeholder="One ID per line, or separated by spaces or commas.",
        required=True,
        max_length=4000,
    )
    reason = discord.ui.TextInput(
        label="Reason",
        style=discord.TextStyle.short,
        placeholder="Recorded on every case.",
        required=False,
        max_length=400,
    )

    def __init__(self, cog: Moderation, invoker: discord.abc.User) -> None:
        super().__init__(timeout=300.0)
        self.cog = cog
        self.invoker = invoker

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.ids.value.replace(",", " ").split()
        parsed: list[int] = []
        rejected: list[str] = []
        for token in raw:
            token = token.strip("<@!>")
            if token.isdigit() and len(token) >= 15:
                parsed.append(int(token))
            elif token:
                rejected.append(token)

        await self.cog.run_massban(
            interaction, parsed, rejected, self.reason.value or "Mass ban"
        )


class MassbanLauncher(BaseView):
    """One button that opens the mass-ban modal.

    A prefix command has no interaction to attach a modal to, so the prefix path
    sends this view and the button provides the interaction.
    """

    def __init__(self, cog: Moderation, invoker: discord.abc.User) -> None:
        super().__init__(invoker, timeout=120.0)
        self.cog = cog

    @discord.ui.button(label="Paste user IDs", style=discord.ButtonStyle.danger)
    async def open_modal(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(MassbanModal(self.cog, interaction.user))
        self.disable_all()
        self.stop()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ReasonModal(discord.ui.Modal, title="Edit case reason"):
    """Long-form reason editor for the slash version of ``?reason``."""

    new_reason = discord.ui.TextInput(
        label="New reason",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    def __init__(self, cog: Moderation, case: Case) -> None:
        super().__init__(timeout=300.0)
        self.cog = cog
        self.case = case
        self.new_reason.default = case.reason or ""

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.apply_reason_edit(interaction, self.case, self.new_reason.value)


class BanMessageModal(discord.ui.Modal, title="Ban DM message"):
    """Editor for the DM sent to banned members."""

    body = discord.ui.TextInput(
        label="Message",
        style=discord.TextStyle.paragraph,
        placeholder="Supports {user} {server} {reason} {moderator} {duration}",
        required=True,
        max_length=1500,
    )

    def __init__(self, cog: Moderation, current: str | None) -> None:
        super().__init__(timeout=300.0)
        self.cog = cog
        self.body.default = current or DEFAULT_BAN_MESSAGE

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.save_ban_message(interaction, self.body.value)


class PurgeFlags(commands.FlagConverter, delimiter=" ", prefix="--"):
    """Filters for ``?purge``.

    As a ``FlagConverter`` these become ``--bots`` on the prefix side and named
    options on the slash side, from one definition.
    """

    user: Optional[discord.User] = commands.flag(
        default=None, description="Only delete messages from this user"
    )
    contains: Optional[str] = commands.flag(
        default=None, description="Only delete messages containing this text"
    )
    bots: bool = commands.flag(default=False, description="Only delete bot messages")
    humans: bool = commands.flag(default=False, description="Only delete human messages")
    links: bool = commands.flag(default=False, description="Only delete messages with links")
    images: bool = commands.flag(
        default=False, description="Only delete messages with image attachments"
    )
    embeds: bool = commands.flag(default=False, description="Only delete messages with embeds")


class Moderation(commands.Cog):
    """Bans, kicks, mutes, warnings, notes, cases and channel control."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        """Register the scheduler handlers this cog owns.

        Anything queued while the bot was down runs on the scheduler's first
        tick after boot, so a restart never leaves someone muted forever.
        """
        self.bot.scheduler.register("unban", self._scheduled_unban)
        self.bot.scheduler.register("unmute", self._scheduled_unmute)
        self.bot.scheduler.register("unlock", self._scheduled_unlock)

    async def cog_unload(self) -> None:
        for action in ("unban", "unmute", "unlock"):
            self.bot.scheduler.unregister(action)

    # =======================================================================
    # Shared helpers
    # =======================================================================

    @staticmethod
    def _ok(description: str, title: str | None = None) -> discord.Embed:
        return discord.Embed(
            title=title,
            description=f"{EMOJI_SUCCESS}  {description}",
            colour=COLOUR_SUCCESS,
        )

    @staticmethod
    def _info(description: str, title: str | None = None) -> discord.Embed:
        return discord.Embed(title=title, description=description, colour=COLOUR_INFO)

    async def _respond(
        self, ctx: commands.Context, embed: discord.Embed, **kwargs: Any
    ) -> None:
        """Report the result, falling back to a DM if the channel refuses us.

        Channel commands can legitimately remove the bot's own ability to post
        in the channel it was invoked from -- ``?lock`` and ``?lockdown`` do
        exactly that. Without this fallback the action succeeds and the user is
        told nothing, which is indistinguishable from the command doing nothing.
        """
        try:
            await ctx.send(embed=embed, **kwargs)
            return
        except discord.Forbidden:
            pass
        except discord.HTTPException as exc:
            log.warning("Could not send result in channel %s: %s", ctx.channel.id, exc)
            return

        try:
            await ctx.author.send(embed=embed)
        except discord.HTTPException:
            log.warning(
                "Command %s succeeded but neither the channel nor a DM could be reached "
                "for %s (%s)",
                ctx.command.qualified_name if ctx.command else "?",
                ctx.author,
                ctx.author.id,
            )

    @staticmethod
    async def _defer(ctx: commands.Context) -> None:
        """Acknowledge a slash command before doing slow work.

        Discord discards an interaction token after three seconds. Scanning a
        ban list or purging a thousand messages easily outruns that, and the
        result then fails with 10062 -- the command works, the user sees
        "application did not respond".
        """
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.defer()

    async def _dm(
        self,
        target: discord.abc.User,
        guild: discord.Guild,
        action: str,
        reason: str | None,
        *,
        duration: timedelta | None = None,
        body: str | None = None,
    ) -> bool:
        """Tell a member what happened to them, before it happens.

        Returns:
            True if the DM was delivered. False covers closed DMs, blocks, and
            a member who shares no other server with the bot -- none of which is
            an error, and none of which may stop the punishment.
        """
        if not await self.bot.guild_config.flag(guild.id, "dm_on_punish"):
            return False

        if body is None:
            body = f"You have been **{action}** in {guild.name}."
            if reason:
                body += f"\n**Reason:** {reason}"
            if duration:
                body += f"\n**Duration:** {format_duration(duration)}"

        try:
            await target.send(
                embed=discord.Embed(description=body, colour=COLOUR_WARNING)
            )
        except (discord.Forbidden, discord.HTTPException):
            return False
        return True

    def _render_ban_message(
        self,
        template: str,
        *,
        target: discord.abc.User,
        guild: discord.Guild,
        moderator: discord.abc.User,
        reason: str | None,
        duration: timedelta | None,
    ) -> str:
        """Substitute the supported variables into a ban DM template.

        Deliberately not a scripting language: five named replacements and
        nothing else. See ``services/tagvars.py`` for the same reasoning applied
        to tags.
        """
        return (
            template.replace("{user}", str(target))
            .replace("{server}", guild.name)
            .replace("{moderator}", str(moderator))
            .replace("{reason}", reason or "No reason given")
            .replace("{duration}", format_duration(duration) if duration else "permanent")
        )

    async def _resolve_target(
        self, ctx: commands.Context, user: discord.User
    ) -> discord.Member | discord.User:
        """Return the guild Member for a user if they are in the guild.

        Startup chunking is off, so a member the bot has not seen since boot is
        not cached and must be fetched. Returning the plain ``User`` is correct
        for ban-by-ID against someone who is not here.
        """
        if ctx.guild is None:
            return user
        member = await self.bot.get_or_fetch_member(ctx.guild, user.id)
        return member or user

    async def _find_case(self, ctx: commands.Context, number: int) -> Case:
        """Fetch a case or raise a readable error."""
        case = await modlog.fetch_case(self.bot, ctx.guild.id, number)
        if case is None:
            raise DeezeeError(f"There is no case #{number} in this server.")
        return case

    # =======================================================================
    # Scheduler handlers
    # =======================================================================

    async def _scheduled_unban(self, guild_id: int, payload: dict[str, Any]) -> None:
        """Lift a temporary ban when it expires."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        user_id = int(payload["user_id"])
        try:
            await guild.unban(
                discord.Object(id=user_id), reason="Temporary ban expired"
            )
        except discord.NotFound:
            # Already unbanned by hand. Closing the case below is still correct.
            pass

        case_id = payload.get("case_id")
        if case_id:
            await modlog.deactivate(self.bot, int(case_id))

        user = self.bot.get_user(user_id) or discord.Object(id=user_id)
        await modlog.create_case(
            self.bot,
            guild,
            action="unban",
            target=user if isinstance(user, discord.User) else _StubUser(user_id),
            moderator=self.bot.user,
            reason="Temporary ban expired",
        )

    async def _scheduled_unmute(self, guild_id: int, payload: dict[str, Any]) -> None:
        """Remove the mute role when a long mute expires.

        Only used for mutes longer than Discord's 28-day native timeout cap;
        shorter ones expire on their own and never reach the scheduler.
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        member = await self.bot.get_or_fetch_member(guild, int(payload["user_id"]))
        role_id = await self.bot.guild_config.channel_id(guild_id, "mute_role_id")

        if member is not None and role_id:
            role = guild.get_role(role_id)
            if role is not None and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Mute expired")
                except discord.HTTPException as exc:
                    log.warning("Could not remove mute role in guild %s: %s", guild_id, exc)

        case_id = payload.get("case_id")
        if case_id:
            await modlog.deactivate(self.bot, int(case_id))

        if member is not None:
            await modlog.create_case(
                self.bot,
                guild,
                action="unmute",
                target=member,
                moderator=self.bot.user,
                reason="Mute expired",
            )

    async def _scheduled_unlock(self, guild_id: int, payload: dict[str, Any]) -> None:
        """Unlock a channel when a timed lock expires."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(int(payload["channel_id"]))
        if isinstance(channel, discord.TextChannel):
            await self._restore_channel(channel, reason="Lock expired")

    # =======================================================================
    # Bans
    # =======================================================================

    @commands.hybrid_command(name="ban")
    @app_commands.describe(
        target="Member, or a raw user ID for someone not in the server",
        duration="Optional: 7d, 12h, 30m. Leave empty for a permanent ban",
        reason="Recorded on the case and DMed to the member",
    )
    @permissions.mod_only()
    @permissions.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def ban(
        self,
        ctx: commands.Context,
        target: discord.User,
        duration: Optional[str] = None,
        *,
        reason: str = "",
    ) -> None:
        """Ban a member or a raw user ID, optionally for a fixed duration."""
        # "?ban @user spamming" arrives with duration="spamming"; give it back.
        delta, reason = split_duration_and_reason(duration, reason)
        reason = reason or "No reason given"

        member = await self._resolve_target(ctx, target)
        await permissions.enforce_hierarchy(
            ctx, member, action="ban", bot_permission="ban_members"
        )

        expires_at = int(time.time() + delta.total_seconds()) if delta else None
        summary = format_duration(delta) if delta else "permanent"

        if not await confirm(
            ctx,
            title=f"Ban {target}?",
            description=f"{target.mention} (`{target.id}`) will be banned from this server.",
            confirm_label="Ban",
            fields={"Duration": summary, "Reason": reason[:1000]},
        ):
            return

        template = await self.bot.guild_config.value(ctx.guild.id, "ban_message")
        body = self._render_ban_message(
            template or DEFAULT_BAN_MESSAGE,
            target=target,
            guild=ctx.guild,
            moderator=ctx.author,
            reason=reason,
            duration=delta,
        )
        dmed = await self._dm(target, ctx.guild, "banned", reason, duration=delta, body=body)

        try:
            await ctx.guild.ban(
                discord.Object(id=target.id),
                reason=f"{ctx.author} ({ctx.author.id}): {reason}",
                delete_message_seconds=0,
            )
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["ban_members"]) from None

        case = await modlog.create_case(
            self.bot,
            ctx.guild,
            action="tempban" if delta else "ban",
            target=target,
            moderator=ctx.author,
            reason=reason,
            expires_at=expires_at,
        )

        if expires_at is not None:
            await self.bot.scheduler.schedule(
                ctx.guild.id,
                "unban",
                expires_at,
                {"user_id": target.id, "case_id": case.id},
            )

        note = "" if dmed else "\nThey could not be DMed."
        await ctx.send(
            embed=self._ok(
                f"Banned **{target}** ({summary}). Case #{case.number}.{note}"
            )
        )

    @commands.hybrid_command(name="unban", aliases=["pardon"])
    @app_commands.describe(user="User ID or tag of someone currently banned", reason="Why")
    @permissions.mod_only()
    @permissions.guild_only()
    async def unban(
        self, ctx: commands.Context, user: str, *, reason: str = "No reason given"
    ) -> None:
        """Lift an active ban and close the matching case."""
        # Resolving a tag means walking the ban list, which on a large server is
        # thousands of entries and well past the interaction deadline.
        await self._defer(ctx)
        target = await self._find_banned_user(ctx.guild, user)

        try:
            await ctx.guild.unban(target, reason=f"{ctx.author} ({ctx.author.id}): {reason}")
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["ban_members"]) from None
        except discord.NotFound:
            raise DeezeeError(f"**{target}** is not banned from this server.") from None

        # Close the ban case and cancel any scheduled expiry, so a manual unban
        # does not leave a scheduler row firing against nothing later.
        case = await modlog.active_case_for(self.bot, ctx.guild.id, target.id, _BAN_ACTIONS)
        if case is not None:
            await modlog.deactivate(self.bot, case.id)
        await self.bot.scheduler.cancel_matching(ctx.guild.id, "unban", user_id=target.id)

        new_case = await modlog.create_case(
            self.bot,
            ctx.guild,
            action="unban",
            target=target,
            moderator=ctx.author,
            reason=reason,
        )
        await ctx.send(embed=self._ok(f"Unbanned **{target}**. Case #{new_case.number}."))

    async def _find_banned_user(
        self, guild: discord.Guild, query: str
    ) -> discord.User:
        """Resolve a ban entry from an ID or a tag.

        Streams the ban list rather than fetching it whole: a large server's ban
        list can run to thousands of entries, and only one is needed.
        """
        query = query.strip().strip("<@!>")

        if query.isdigit():
            try:
                entry = await guild.fetch_ban(discord.Object(id=int(query)))
            except discord.NotFound:
                raise DeezeeError(f"`{query}` is not banned from this server.") from None
            except discord.Forbidden:
                raise commands.BotMissingPermissions(["ban_members"]) from None
            return entry.user

        lowered = query.lower()
        async for entry in guild.bans(limit=None):
            if str(entry.user).lower() == lowered or entry.user.name.lower() == lowered:
                return entry.user

        raise DeezeeError(
            f"Could not find a banned user matching `{query}`. Try their user ID."
        )

    @commands.hybrid_command(name="softban")
    @app_commands.describe(
        target="Member to softban",
        delete_days="Days of their messages to delete (0-7, default 1)",
        reason="Why",
    )
    @permissions.mod_only()
    @permissions.guild_only()
    async def softban(
        self,
        ctx: commands.Context,
        target: discord.Member,
        delete_days: int = 1,
        *,
        reason: str = "No reason given",
    ) -> None:
        """Ban then immediately unban, to bulk-delete a member's recent messages."""
        await permissions.enforce_hierarchy(
            ctx, target, action="softban", bot_permission="ban_members"
        )
        delete_days = max(0, min(delete_days, 7))

        if not await confirm(
            ctx,
            title=f"Softban {target}?",
            description=(
                f"{target.mention} will be banned and immediately unbanned, deleting "
                f"their messages from the last {delete_days} day(s). They can rejoin."
            ),
            confirm_label="Softban",
            fields={"Reason": reason[:1000]},
        ):
            return

        await self._dm(target, ctx.guild, "softbanned", reason)

        audit = f"{ctx.author} ({ctx.author.id}): {reason}"
        try:
            await ctx.guild.ban(
                target, reason=audit, delete_message_seconds=delete_days * 86400
            )
            await ctx.guild.unban(target, reason=f"Softban by {ctx.author}")
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["ban_members"]) from None

        case = await modlog.create_case(
            self.bot,
            ctx.guild,
            action="softban",
            target=target,
            moderator=ctx.author,
            reason=reason,
        )
        await ctx.send(embed=self._ok(f"Softbanned **{target}**. Case #{case.number}."))

    @commands.hybrid_command(name="massban", aliases=["banlist"])
    @app_commands.describe(reason="Recorded on every case created")
    @permissions.admin_only()
    @permissions.guild_only()
    async def massban(self, ctx: commands.Context, *, reason: str = "Mass ban") -> None:
        """Ban a pasted list of user IDs in one action."""
        if not ctx.guild.me.guild_permissions.ban_members:
            raise commands.BotMissingPermissions(["ban_members"])

        if ctx.interaction is not None:
            # Slash: the interaction is still unanswered, so the modal can go
            # straight up without an intermediate message.
            await ctx.interaction.response.send_modal(MassbanModal(self, ctx.author))
            return

        view = MassbanLauncher(self, ctx.author)
        view.message = await ctx.send(
            embed=self._info(
                "Press the button to paste the user IDs.\n"
                "A modal holds far more IDs than a chat message, and lets you "
                "check the list before submitting.",
                title="Mass ban",
            ),
            view=view,
        )

    async def run_massban(
        self,
        interaction: discord.Interaction,
        user_ids: list[int],
        rejected: list[str],
        reason: str,
    ) -> None:
        """Execute a mass ban with a live progress embed.

        Throttled between batches: an unpaced loop over three hundred bans would
        spend the whole run in rate-limit backoff and pin the CPU while doing it.
        """
        guild = interaction.guild
        if guild is None:
            return

        if not user_ids:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description=f"{EMOJI_WARNING}  No valid user IDs in that list.",
                    colour=COLOUR_ERROR,
                ),
                ephemeral=True,
            )
            return

        # Root owners are removed from the list before anything is confirmed, so
        # a mass ban cannot be used to reach one by hiding them in a long paste.
        protected = [uid for uid in user_ids if self.bot.config.is_root_owner(uid)]
        user_ids = [uid for uid in user_ids if uid not in protected]

        embed = discord.Embed(
            title="Mass ban",
            description=f"Banning **{len(user_ids)}** user(s)...",
            colour=COLOUR_WARNING,
        )
        if rejected:
            embed.add_field(
                name="Skipped (not IDs)", value=f"{len(rejected)} entries", inline=True
            )
        if protected:
            embed.add_field(name="Protected", value=f"{len(protected)} root owner(s)", inline=True)

        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()

        audit = f"{interaction.user} ({interaction.user.id}): {reason}"
        banned = 0
        failed = 0

        for index, user_id in enumerate(user_ids, start=1):
            try:
                await guild.ban(discord.Object(id=user_id), reason=audit, delete_message_seconds=0)
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
            else:
                banned += 1
                await modlog.create_case(
                    self.bot,
                    guild,
                    action="ban",
                    target=_StubUser(user_id),
                    moderator=interaction.user,
                    reason=reason,
                    post=False,
                )

            # Update every ten, so a long run shows progress without spending its
            # rate limit budget on message edits.
            if index % 10 == 0 or index == len(user_ids):
                embed.description = (
                    f"Banning... **{index}/{len(user_ids)}**\n"
                    f"Succeeded: {banned} • Failed: {failed}"
                )
                try:
                    await message.edit(embed=embed)
                except discord.HTTPException:
                    pass
                await asyncio.sleep(BULK_ACTION_DELAY)

        embed.title = "Mass ban complete"
        embed.colour = COLOUR_SUCCESS if not failed else COLOUR_WARNING
        embed.description = f"Banned **{banned}**, failed **{failed}**."
        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            pass

    # =======================================================================
    # Kick, mute, warn
    # =======================================================================

    @commands.hybrid_command(name="kick")
    @app_commands.describe(target="Member to remove", reason="Why")
    @permissions.mod_only()
    @permissions.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def kick(
        self, ctx: commands.Context, target: discord.Member, *, reason: str = "No reason given"
    ) -> None:
        """Remove a member from the server. They can rejoin with a new invite."""
        await permissions.enforce_hierarchy(
            ctx, target, action="kick", bot_permission="kick_members"
        )

        if not await confirm(
            ctx,
            title=f"Kick {target}?",
            description=f"{target.mention} will be removed. They can rejoin with a new invite.",
            confirm_label="Kick",
            fields={"Reason": reason[:1000]},
        ):
            return

        dmed = await self._dm(target, ctx.guild, "kicked", reason)

        try:
            await target.kick(reason=f"{ctx.author} ({ctx.author.id}): {reason}")
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["kick_members"]) from None

        case = await modlog.create_case(
            self.bot, ctx.guild, action="kick", target=target,
            moderator=ctx.author, reason=reason,
        )
        note = "" if dmed else "\nThey could not be DMed."
        await ctx.send(embed=self._ok(f"Kicked **{target}**. Case #{case.number}.{note}"))

    @commands.hybrid_command(name="mute", aliases=["timeout", "tempmute"])
    @app_commands.describe(
        target="Member to mute",
        duration="1h, 30m, 7d. Defaults to 1 hour",
        reason="Why",
    )
    @permissions.mod_only()
    @permissions.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def mute(
        self,
        ctx: commands.Context,
        target: discord.Member,
        duration: Optional[str] = None,
        *,
        reason: str = "",
    ) -> None:
        """Time out a member so they cannot speak, react or join voice."""
        delta, reason = split_duration_and_reason(duration, reason)
        delta = delta or timedelta(hours=1)
        reason = reason or "No reason given"

        await permissions.enforce_hierarchy(
            ctx, target, action="mute", bot_permission="moderate_members"
        )

        native = delta <= timedelta(days=MAX_TIMEOUT_DAYS)
        expires_at = int(time.time() + delta.total_seconds())

        if not await confirm(
            ctx,
            title=f"Mute {target}?",
            description=f"{target.mention} will be muted for {format_duration(delta)}.",
            confirm_label="Mute",
            fields={
                "Method": "Discord timeout" if native else "Mute role",
                "Reason": reason[:1000],
            },
            danger=False,
        ):
            return

        await self._dm(target, ctx.guild, "muted", reason, duration=delta)

        audit = f"{ctx.author} ({ctx.author.id}): {reason}"
        if native:
            # Discord's own timeout is better than a role: it blocks voice and
            # reactions too, and survives the bot being offline.
            try:
                await target.timeout(delta, reason=audit)
            except discord.Forbidden:
                raise commands.BotMissingPermissions(["moderate_members"]) from None
        else:
            role = await self._require_mute_role(ctx.guild)
            try:
                await target.add_roles(role, reason=audit)
            except discord.Forbidden:
                raise commands.BotMissingPermissions(["manage_roles"]) from None

        case = await modlog.create_case(
            self.bot, ctx.guild, action="mute", target=target,
            moderator=ctx.author, reason=reason, expires_at=expires_at,
        )

        if not native:
            # Native timeouts expire on their own; role mutes need us to undo it.
            await self.bot.scheduler.schedule(
                ctx.guild.id, "unmute", expires_at,
                {"user_id": target.id, "case_id": case.id},
            )

        await ctx.send(
            embed=self._ok(
                f"Muted **{target}** for {format_duration(delta)}. Case #{case.number}."
            )
        )

    async def _require_mute_role(self, guild: discord.Guild) -> discord.Role:
        """Fetch the configured mute role or explain how to set one."""
        role_id = await self.bot.guild_config.value(guild.id, "mute_role_id")
        role = guild.get_role(int(role_id)) if role_id else None
        if role is None:
            raise NotConfigured("a mute role", "Moderation")

        usable, why = permissions.can_manage_role(guild, role)
        if not usable:
            raise DeezeeError(f"I cannot use the configured mute role: {why}")
        return role

    @commands.hybrid_command(name="unmute", aliases=["untimeout"])
    @app_commands.describe(target="Member to unmute", reason="Why")
    @permissions.mod_only()
    @permissions.guild_only()
    async def unmute(
        self, ctx: commands.Context, target: discord.Member, *, reason: str = "No reason given"
    ) -> None:
        """Clear an active timeout or remove the mute role."""
        await permissions.enforce_hierarchy(ctx, target, action="unmute")

        cleared = False
        audit = f"{ctx.author} ({ctx.author.id}): {reason}"

        if target.is_timed_out():
            try:
                await target.timeout(None, reason=audit)
                cleared = True
            except discord.Forbidden:
                raise commands.BotMissingPermissions(["moderate_members"]) from None

        role_id = await self.bot.guild_config.value(ctx.guild.id, "mute_role_id")
        if role_id:
            role = ctx.guild.get_role(int(role_id))
            if role is not None and role in target.roles:
                try:
                    await target.remove_roles(role, reason=audit)
                    cleared = True
                except discord.Forbidden:
                    raise commands.BotMissingPermissions(["manage_roles"]) from None

        if not cleared:
            raise DeezeeError(f"**{target}** is not muted.")

        case = await modlog.active_case_for(self.bot, ctx.guild.id, target.id, _MUTE_ACTIONS)
        if case is not None:
            await modlog.deactivate(self.bot, case.id)
        await self.bot.scheduler.cancel_matching(ctx.guild.id, "unmute", user_id=target.id)

        new_case = await modlog.create_case(
            self.bot, ctx.guild, action="unmute", target=target,
            moderator=ctx.author, reason=reason,
        )
        await ctx.send(embed=self._ok(f"Unmuted **{target}**. Case #{new_case.number}."))

    @commands.hybrid_command(name="warn")
    @app_commands.describe(target="Member to warn", reason="Shown to them in a DM")
    @permissions.mod_only()
    @permissions.guild_only()
    async def warn(
        self, ctx: commands.Context, target: discord.Member, *, reason: str
    ) -> None:
        """Issue a formal warning, log a case and DM the member."""
        await permissions.enforce_hierarchy(ctx, target, action="warn")

        case = await modlog.create_case(
            self.bot, ctx.guild, action="warn", target=target,
            moderator=ctx.author, reason=reason,
        )
        await self.bot.db.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at, "
            "source, case_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ctx.guild.id, target.id, ctx.author.id, reason, int(time.time()), "manual", case.id),
        )

        count = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM warnings WHERE guild_id = ? AND user_id = ? AND active = 1",
            (ctx.guild.id, target.id),
        )

        dmed = await self._dm(target, ctx.guild, "warned", reason)
        note = "" if dmed else " They could not be DMed."
        await ctx.send(
            embed=self._ok(
                f"Warned **{target}** (warning {count} on record). Case #{case.number}.{note}"
            )
        )

    @commands.hybrid_command(name="warnings", aliases=["warns", "infractions"])
    @app_commands.describe(target="Member to look up. Defaults to you")
    @permissions.mod_only()
    @permissions.guild_only()
    async def warnings(
        self, ctx: commands.Context, target: Optional[discord.Member] = None
    ) -> None:
        """List a member's warnings with IDs, moderators and timestamps."""
        target = target or ctx.author

        rows = await self.bot.db.fetchall(
            "SELECT id, moderator_id, reason, created_at, source FROM warnings "
            "WHERE guild_id = ? AND user_id = ? AND active = 1 ORDER BY created_at DESC",
            (ctx.guild.id, target.id),
        )

        lines = [
            f"**#{row['id']}** • <t:{row['created_at']}:{TS_RELATIVE}> • "
            f"by <@{row['moderator_id']}>"
            + (f" • *{row['source']}*" if row["source"] != "manual" else "")
            + f"\n{(row['reason'] or 'No reason given')[:200]}"
            for row in rows
        ]

        pages = paginate_lines(
            lines,
            title=f"Warnings for {target}",
            per_page=5,
            colour=COLOUR_WARNING if lines else COLOUR_MUTED,
            description=f"{len(lines)} active warning(s).",
            empty_message="No active warnings.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @commands.hybrid_command(name="delwarn", aliases=["removewarn"])
    @app_commands.describe(warning_id="The number shown in ?warnings", reason="Why")
    @permissions.mod_only()
    @permissions.guild_only()
    async def delwarn(
        self, ctx: commands.Context, warning_id: int, *, reason: str = "No reason given"
    ) -> None:
        """Delete a single warning by its ID."""
        row = await self.bot.db.fetchone(
            "SELECT user_id FROM warnings WHERE id = ? AND guild_id = ? AND active = 1",
            (warning_id, ctx.guild.id),
        )
        if row is None:
            raise DeezeeError(f"There is no active warning #{warning_id} in this server.")

        # Deactivated rather than deleted: the history stays auditable, and
        # ?modlogs can still show that a warning was issued and later removed.
        await self.bot.db.execute(
            "UPDATE warnings SET active = 0 WHERE id = ?", (warning_id,)
        )

        target = await self.bot.get_or_fetch_member(ctx.guild, row["user_id"]) or _StubUser(
            row["user_id"]
        )
        case = await modlog.create_case(
            self.bot, ctx.guild, action="unwarn", target=target,
            moderator=ctx.author, reason=reason,
        )
        await ctx.send(
            embed=self._ok(f"Removed warning #{warning_id}. Case #{case.number}.")
        )

    @commands.hybrid_command(name="clearwarns", with_app_command=False, aliases=["clearwarn"])
    @app_commands.describe(target="Member whose warnings should be cleared")
    @permissions.admin_only()
    @permissions.guild_only()
    async def clearwarns(self, ctx: commands.Context, target: discord.Member) -> None:
        """Delete every warning on a member's record."""
        count = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM warnings WHERE guild_id = ? AND user_id = ? AND active = 1",
            (ctx.guild.id, target.id),
        )
        if not count:
            raise DeezeeError(f"**{target}** has no active warnings.")

        if not await confirm(
            ctx,
            title=f"Clear {count} warning(s)?",
            description=(
                f"Every active warning on {target.mention} will be cleared. "
                "The mod-log cases stay, so the history remains auditable."
            ),
            confirm_label=f"Clear {count}",
        ):
            return

        await self.bot.db.execute(
            "UPDATE warnings SET active = 0 WHERE guild_id = ? AND user_id = ? AND active = 1",
            (ctx.guild.id, target.id),
        )
        await ctx.send(embed=self._ok(f"Cleared {count} warning(s) from **{target}**."))

    # =======================================================================
    # Cases
    # =======================================================================

    @commands.hybrid_command(name="case")
    @app_commands.describe(number="Case number, as shown in the mod-log")
    @permissions.mod_only()
    @permissions.guild_only()
    async def case(self, ctx: commands.Context, number: int) -> None:
        """Show a single mod-log case in full."""
        found = await self._find_case(ctx, number)
        embed = modlog.build_embed(found, guild=ctx.guild)
        embed.add_field(
            name="Created",
            value=f"<t:{found.created_at}:{TS_LONG_DATE_TIME}>",
            inline=False,
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="reason", aliases=["editcase"])
    @app_commands.describe(number="Case number", new_reason="Replacement reason")
    @permissions.mod_only()
    @permissions.guild_only()
    async def reason(
        self, ctx: commands.Context, number: int, *, new_reason: str = ""
    ) -> None:
        """Edit the reason on a case and update the posted mod-log embed."""
        found = await self._find_case(ctx, number)

        # No reason given on the slash version: open a modal prefilled with the
        # current text, which is far easier than retyping a long reason.
        if not new_reason.strip():
            if ctx.interaction is None:
                raise commands.MissingRequiredArgument(
                    ctx.command.clean_params["new_reason"]
                )
            await ctx.interaction.response.send_modal(ReasonModal(self, found))
            return

        edited = await modlog.update_reason(self.bot, ctx.guild, found, new_reason)
        suffix = "" if edited else " The mod-log message could not be edited."
        await ctx.send(embed=self._ok(f"Updated case #{number}.{suffix}"))

    async def apply_reason_edit(
        self, interaction: discord.Interaction, case: Case, new_reason: str
    ) -> None:
        """Apply a reason edit submitted through the modal."""
        edited = await modlog.update_reason(self.bot, interaction.guild, case, new_reason)
        suffix = "" if edited else " The mod-log message could not be edited."
        await interaction.response.send_message(
            embed=self._ok(f"Updated case #{case.number}.{suffix}"), ephemeral=True
        )

    @commands.hybrid_command(name="modlogs", aliases=["history"])
    @app_commands.describe(target="Member whose case history to show")
    @permissions.mod_only()
    @permissions.guild_only()
    async def modlogs(self, ctx: commands.Context, target: discord.User) -> None:
        """Show every case tied to a member, across all action types."""
        rows = await self.bot.db.fetchall(
            "SELECT * FROM mod_cases WHERE guild_id = ? AND target_id = ? "
            "ORDER BY created_at DESC",
            (ctx.guild.id, target.id),
        )
        cases = [Case.from_row(row) for row in rows]

        fields = [
            (
                f"#{c.number} • {c.label}" + ("" if c.active else " (inactive)"),
                f"<t:{c.created_at}:{TS_RELATIVE}> by <@{c.moderator_id}>\n"
                f"{(c.reason or 'No reason given')[:300]}",
                False,
            )
            for c in cases
        ]

        counts: dict[str, int] = {}
        for c in cases:
            counts[c.label] = counts.get(c.label, 0) + 1
        summary = " • ".join(f"{label}: {n}" for label, n in sorted(counts.items()))

        pages = paginate_fields(
            fields,
            title=f"Case history for {target}",
            per_page=5,
            description=summary or None,
            empty_message="No cases on record.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @commands.hybrid_command(name="moderations", aliases=["activemods"])
    @app_commands.describe(target="Optional: only this member's active punishments")
    @permissions.mod_only()
    @permissions.guild_only()
    async def moderations(
        self, ctx: commands.Context, target: Optional[discord.User] = None
    ) -> None:
        """List punishments still active and scheduled to expire."""
        if target is None:
            rows = await self.bot.db.fetchall(
                "SELECT * FROM mod_cases WHERE guild_id = ? AND active = 1 "
                "AND expires_at IS NOT NULL ORDER BY expires_at ASC",
                (ctx.guild.id,),
            )
        else:
            rows = await self.bot.db.fetchall(
                "SELECT * FROM mod_cases WHERE guild_id = ? AND target_id = ? AND active = 1 "
                "AND expires_at IS NOT NULL ORDER BY expires_at ASC",
                (ctx.guild.id, target.id),
            )

        lines = [
            f"**#{row['case_number']}** {modlog.ACTION_LABELS.get(row['action'], row['action'])}"
            f" • <@{row['target_id']}>\n"
            f"Expires <t:{row['expires_at']}:{TS_RELATIVE}> "
            f"(<t:{row['expires_at']}:{TS_LONG_DATE_TIME}>)"
            for row in rows
        ]

        pages = paginate_lines(
            lines,
            title="Active timed punishments",
            per_page=6,
            description=f"{len(lines)} still running.",
            empty_message="Nothing is currently scheduled to expire.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @commands.hybrid_command(name="duration", with_app_command=False)
    @app_commands.describe(
        number="Case number of an active timed punishment",
        new_duration="New total duration from now, e.g. 2h or 3d",
    )
    @permissions.mod_only()
    @permissions.guild_only()
    async def duration(
        self, ctx: commands.Context, number: int, new_duration: str
    ) -> None:
        """Change the remaining duration of an active mute or timed ban."""
        found = await self._find_case(ctx, number)

        if not found.active or found.expires_at is None:
            raise DeezeeError(f"Case #{number} is not an active timed punishment.")

        delta = parse_duration(new_duration)
        if delta is None:
            raise DeezeeError(
                f"`{new_duration}` is not a duration. Try `30m`, `2h`, `7d`."
            )

        expires_at = int(time.time() + delta.total_seconds())
        await self.bot.db.execute(
            "UPDATE mod_cases SET expires_at = ? WHERE id = ?", (expires_at, found.id)
        )

        action_type = "unban" if found.action in _BAN_ACTIONS else "unmute"
        await self.bot.scheduler.cancel_matching(
            ctx.guild.id, action_type, user_id=found.target_id
        )

        # A native Discord timeout is enforced by Discord, so the timeout itself
        # has to move as well -- rescheduling our own row would not shorten it.
        if found.action == "mute":
            member = await self.bot.get_or_fetch_member(ctx.guild, found.target_id)
            if member is not None and member.is_timed_out():
                if delta <= timedelta(days=MAX_TIMEOUT_DAYS):
                    await member.timeout(delta, reason=f"Duration changed by {ctx.author}")
                else:
                    raise DeezeeError(
                        f"A Discord timeout cannot exceed {MAX_TIMEOUT_DAYS} days. "
                        "Unmute and re-mute to switch to the mute role."
                    )
            else:
                await self.bot.scheduler.schedule(
                    ctx.guild.id, "unmute", expires_at,
                    {"user_id": found.target_id, "case_id": found.id},
                )
        else:
            await self.bot.scheduler.schedule(
                ctx.guild.id, "unban", expires_at,
                {"user_id": found.target_id, "case_id": found.id},
            )

        await ctx.send(
            embed=self._ok(
                f"Case #{number} now expires <t:{expires_at}:{TS_RELATIVE}> "
                f"({format_duration(delta)} from now)."
            )
        )

    @commands.hybrid_command(name="modstats", with_app_command=False)
    @app_commands.describe(
        moderator="Optional: only this moderator", days="Window in days, default 30"
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def modstats(
        self,
        ctx: commands.Context,
        moderator: Optional[discord.User] = None,
        days: int = 30,
    ) -> None:
        """Show per-moderator action counts over a time window."""
        days = max(1, min(days, 365))
        since = int(time.time()) - days * 86400

        if moderator is None:
            rows = await self.bot.db.fetchall(
                "SELECT moderator_id, moderator_tag, COUNT(*) AS total FROM mod_cases "
                "WHERE guild_id = ? AND created_at >= ? "
                "GROUP BY moderator_id ORDER BY total DESC LIMIT 25",
                (ctx.guild.id, since),
            )
            lines = [
                f"**{i}.** <@{row['moderator_id']}> — **{row['total']}** action(s)"
                for i, row in enumerate(rows, start=1)
            ]
            embed = discord.Embed(
                title=f"Moderator activity, last {days} day(s)",
                description="\n".join(lines) or "No actions recorded in this window.",
                colour=COLOUR_DEFAULT,
            )
            await ctx.send(embed=embed)
            return

        rows = await self.bot.db.fetchall(
            "SELECT action, COUNT(*) AS total FROM mod_cases "
            "WHERE guild_id = ? AND moderator_id = ? AND created_at >= ? "
            "GROUP BY action ORDER BY total DESC",
            (ctx.guild.id, moderator.id, since),
        )
        total = sum(row["total"] for row in rows)
        embed = discord.Embed(
            title=f"{moderator} — last {days} day(s)",
            description=f"**{total}** action(s) total.",
            colour=COLOUR_DEFAULT,
        )
        for row in rows:
            embed.add_field(
                name=modlog.ACTION_LABELS.get(row["action"], row["action"]),
                value=str(row["total"]),
                inline=True,
            )
        await ctx.send(embed=embed)

    # =======================================================================
    # Notes
    # =======================================================================

    @commands.hybrid_command(name="note", aliases=["addnote"])
    @app_commands.describe(target="Member the note is about", text="The note")
    @permissions.mod_only()
    @permissions.guild_only()
    async def note(
        self, ctx: commands.Context, target: discord.User, *, text: str
    ) -> None:
        """Attach a private staff note to a member. Never DMed to them."""
        cursor = await self.bot.db.execute(
            "INSERT INTO notes (guild_id, user_id, author_id, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (ctx.guild.id, target.id, ctx.author.id, text, int(time.time())),
        )
        await ctx.send(
            embed=self._ok(
                f"Note #{cursor.lastrowid} added to **{target}**. "
                "Notes are staff-only and are never sent to the member."
            )
        )

    @commands.hybrid_command(name="notes")
    @app_commands.describe(target="Member whose notes to list")
    @permissions.mod_only()
    @permissions.guild_only()
    async def notes(self, ctx: commands.Context, target: discord.User) -> None:
        """List private staff notes on a member."""
        rows = await self.bot.db.fetchall(
            "SELECT id, author_id, content, created_at FROM notes "
            "WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC",
            (ctx.guild.id, target.id),
        )
        lines = [
            f"**#{row['id']}** • <t:{row['created_at']}:{TS_RELATIVE}> • "
            f"by <@{row['author_id']}>\n{row['content'][:300]}"
            for row in rows
        ]
        pages = paginate_lines(
            lines,
            title=f"Staff notes on {target}",
            per_page=5,
            description=f"{len(lines)} note(s). Staff-only.",
            empty_message="No notes on this member.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @commands.hybrid_command(name="delnote", with_app_command=False)
    @app_commands.describe(note_id="The number shown in ?notes")
    @permissions.mod_only()
    @permissions.guild_only()
    async def delnote(self, ctx: commands.Context, note_id: int) -> None:
        """Delete a single staff note by its ID."""
        cursor = await self.bot.db.execute(
            "DELETE FROM notes WHERE id = ? AND guild_id = ?", (note_id, ctx.guild.id)
        )
        if not cursor.rowcount:
            raise DeezeeError(f"There is no note #{note_id} in this server.")
        await ctx.send(embed=self._ok(f"Deleted note #{note_id}."))

    @commands.hybrid_command(name="clearnotes", with_app_command=False)
    @app_commands.describe(target="Member whose notes should all be deleted")
    @permissions.admin_only()
    @permissions.guild_only()
    async def clearnotes(self, ctx: commands.Context, target: discord.User) -> None:
        """Delete every staff note on a member."""
        count = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM notes WHERE guild_id = ? AND user_id = ?",
            (ctx.guild.id, target.id),
        )
        if not count:
            raise DeezeeError(f"**{target}** has no notes.")

        if not await confirm(
            ctx,
            title=f"Delete {count} note(s)?",
            description=f"Every staff note on {target.mention} will be permanently deleted.",
            confirm_label=f"Delete {count}",
        ):
            return

        await self.bot.db.execute(
            "DELETE FROM notes WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, target.id)
        )
        await ctx.send(embed=self._ok(f"Deleted {count} note(s) on **{target}**."))

    # =======================================================================
    # Messages and channels
    # =======================================================================

    @commands.hybrid_command(name="purge", aliases=["clear", "prune"])
    @app_commands.describe(count="How many messages to scan, 1 to 1000")
    @permissions.mod_only()
    @permissions.guild_only()
    @commands.max_concurrency(1, commands.BucketType.channel)
    async def purge(
        self, ctx: commands.Context, count: int, *, flags: PurgeFlags
    ) -> None:
        """Bulk-delete recent messages, optionally filtered."""
        await self._defer(ctx)

        if not ctx.channel.permissions_for(ctx.guild.me).manage_messages:
            raise commands.BotMissingPermissions(["manage_messages"])

        count = max(1, min(count, MAX_PURGE_LIMIT))
        threshold = await self.bot.guild_config.value(
            ctx.guild.id, "purge_confirm_threshold"
        )

        if count > int(threshold or 100):
            if not await confirm(
                ctx,
                title=f"Purge up to {count} messages?",
                description=(
                    f"Up to **{count}** messages in {ctx.channel.mention} will be scanned "
                    "and matching ones deleted. This cannot be undone."
                ),
                confirm_label=f"Purge {count}",
            ):
                return

        def matches(message: discord.Message) -> bool:
            """Whether this message should be deleted.

            Filters are AND-ed. Passing none deletes everything scanned.
            """
            if message.id == ctx.message.id:
                return False
            if flags.user and message.author.id != flags.user.id:
                return False
            if flags.bots and not message.author.bot:
                return False
            if flags.humans and message.author.bot:
                return False
            if flags.contains and flags.contains.lower() not in message.content.lower():
                return False
            if flags.links and "http://" not in message.content and "https://" not in message.content:
                return False
            if flags.images and not any(
                a.content_type and a.content_type.startswith("image/")
                for a in message.attachments
            ):
                return False
            if flags.embeds and not message.embeds:
                return False
            return True

        # Discord refuses to bulk-delete anything older than 14 days. Bounding
        # the scan by that means the API never rejects the batch, and the report
        # can say honestly how far back it reached.
        cutoff = datetime.now(timezone.utc) - timedelta(days=BULK_DELETE_MAX_AGE_DAYS)

        try:
            deleted = await ctx.channel.purge(
                limit=count, check=matches, after=cutoff, reason=f"Purge by {ctx.author}"
            )
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["manage_messages"]) from None

        await modlog.create_case(
            self.bot,
            ctx.guild,
            action="purge",
            target=ctx.author,
            moderator=ctx.author,
            reason=f"{len(deleted)} message(s) in #{ctx.channel.name}",
        )

        await self._respond(
            ctx,
            self._ok(
                f"Deleted **{len(deleted)}** message(s).\n"
                f"Messages older than {BULK_DELETE_MAX_AGE_DAYS} days cannot be "
                "bulk-deleted by any bot and were skipped."
            ),
        )

    @commands.hybrid_command(name="slowmode", aliases=["sm"])
    @app_commands.describe(
        seconds="Delay in seconds, or 'off'. Maximum 21600 (6 hours)",
        channel="Channel to change. Defaults to this one",
    )
    @permissions.mod_only()
    @permissions.guild_only()
    async def slowmode(
        self,
        ctx: commands.Context,
        seconds: str,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Set or clear per-channel slowmode."""
        channel = channel or ctx.channel

        delta = parse_duration(seconds)
        value = int(delta.total_seconds()) if delta else 0
        value = max(0, min(value, 21600))  # Discord's own ceiling.

        try:
            await channel.edit(slowmode_delay=value, reason=f"Slowmode by {ctx.author}")
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["manage_channels"]) from None

        if value:
            await ctx.send(
                embed=self._ok(
                    f"Slowmode in {channel.mention} set to **{format_duration(value)}**."
                )
            )
        else:
            await ctx.send(embed=self._ok(f"Slowmode disabled in {channel.mention}."))

    @commands.hybrid_command(name="lock")
    @app_commands.describe(
        channel="Channel to lock. Defaults to this one",
        duration="Optional: unlock automatically after this long",
        reason="Why",
    )
    @permissions.mod_only()
    @permissions.guild_only()
    async def lock(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        duration: Optional[str] = None,
        *,
        reason: str = "",
    ) -> None:
        """Deny Send Messages to @everyone in a channel."""
        channel = channel or ctx.channel
        delta, reason = split_duration_and_reason(duration, reason)
        reason = reason or "No reason given"

        locked = await self._lock_channel(
            channel, ctx.author, reason=reason, from_lockdown=False
        )
        if not locked:
            raise DeezeeError(f"{channel.mention} is already locked.")

        if delta is not None:
            await self.bot.scheduler.schedule(
                ctx.guild.id,
                "unlock",
                int(time.time() + delta.total_seconds()),
                {"channel_id": channel.id},
            )

        suffix = f" for {format_duration(delta)}" if delta else ""
        await modlog.create_case(
            self.bot, ctx.guild, action="lock", target=ctx.author,
            moderator=ctx.author, reason=f"#{channel.name}: {reason}",
        )
        await self._respond(ctx, self._ok(f"Locked {channel.mention}{suffix}."))

    async def _lock_channel(
        self,
        channel: discord.TextChannel,
        moderator: discord.abc.User,
        *,
        reason: str,
        from_lockdown: bool,
    ) -> bool:
        """Deny Send Messages and remember the previous overwrite.

        Discord has three states -- allow, deny and unset -- and "unset" is not
        "allow". Recording the old value is the only way ``?unlock`` can restore
        the channel rather than silently granting a permission it never had.

        The bot also grants itself an explicit Send Messages overwrite here. A
        channel-level deny on @everyone beats a guild-level role allow, so
        without it the bot locks itself out and cannot even report that the lock
        worked. Its previous value is stored so ``?unlock`` removes the grant
        again.

        Returns:
            False if the channel was already locked by this bot.
        """
        existing = await self.bot.db.fetchone(
            "SELECT channel_id FROM channel_locks WHERE guild_id = ? AND channel_id = ?",
            (channel.guild.id, channel.id),
        )
        if existing is not None:
            return False

        me = channel.guild.me
        bot_overwrite = channel.overwrites_for(me)
        previous_bot = bot_overwrite.send_messages

        everyone = channel.guild.default_role
        overwrite = channel.overwrites_for(everyone)
        previous = overwrite.send_messages  # True, False, or None for unset.

        try:
            # Grant first, deny second. In the other order there is a window in
            # which the bot cannot post in its own channel.
            if previous_bot is not True:
                bot_overwrite.send_messages = True
                await channel.set_permissions(me, overwrite=bot_overwrite, reason=reason)

            overwrite.send_messages = False
            await channel.set_permissions(everyone, overwrite=overwrite, reason=reason)
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["manage_channels"]) from None

        await self.bot.db.execute(
            "INSERT INTO channel_locks (guild_id, channel_id, previous, from_lockdown, "
            "locked_by, locked_at, reason, previous_bot) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                channel.guild.id,
                channel.id,
                None if previous is None else int(previous),
                int(from_lockdown),
                moderator.id,
                int(time.time()),
                reason,
                None if previous_bot is None else int(previous_bot),
            ),
        )
        return True

    async def _restore_channel(self, channel: discord.TextChannel, *, reason: str) -> bool:
        """Put a locked channel's @everyone overwrite back exactly as it was."""
        row = await self.bot.db.fetchone(
            "SELECT previous, previous_bot FROM channel_locks "
            "WHERE guild_id = ? AND channel_id = ?",
            (channel.guild.id, channel.id),
        )
        if row is None:
            return False

        everyone = channel.guild.default_role
        overwrite = channel.overwrites_for(everyone)
        overwrite.send_messages = None if row["previous"] is None else bool(row["previous"])

        me = channel.guild.me
        bot_overwrite = channel.overwrites_for(me)
        bot_overwrite.send_messages = (
            None if row["previous_bot"] is None else bool(row["previous_bot"])
        )

        try:
            await channel.set_permissions(everyone, overwrite=overwrite, reason=reason)
            # Drop the bot's grant last, so it can still report the result.
            await channel.set_permissions(me, overwrite=bot_overwrite, reason=reason)
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["manage_channels"]) from None

        await self.bot.db.execute(
            "DELETE FROM channel_locks WHERE guild_id = ? AND channel_id = ?",
            (channel.guild.id, channel.id),
        )
        await self.bot.scheduler.cancel_matching(
            channel.guild.id, "unlock", channel_id=channel.id
        )
        return True

    @commands.hybrid_command(name="unlock")
    @app_commands.describe(channel="Channel to unlock. Defaults to this one")
    @permissions.mod_only()
    @permissions.guild_only()
    async def unlock(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Restore a locked channel's original permission overwrites."""
        channel = channel or ctx.channel

        if not await self._restore_channel(channel, reason=f"Unlock by {ctx.author}"):
            raise DeezeeError(
                f"{channel.mention} is not locked by me. If it was locked by hand, "
                "change the permission overwrite directly -- I do not know what it was before."
            )

        await modlog.create_case(
            self.bot, ctx.guild, action="unlock", target=ctx.author,
            moderator=ctx.author, reason=f"#{channel.name}",
        )
        await self._respond(ctx, self._ok(f"Unlocked {channel.mention}."))

    @commands.hybrid_command(name="lockdown")
    @app_commands.describe(state="start or end", reason="Why")
    @permissions.admin_only()
    @permissions.guild_only()
    async def lockdown(
        self, ctx: commands.Context, state: str, *, reason: str = "No reason given"
    ) -> None:
        """Lock or unlock every channel in the configured lockdown set."""
        await self._defer(ctx)

        state = state.lower().strip()
        if state not in {"start", "end"}:
            raise DeezeeError("Use `?lockdown start` or `?lockdown end`.")

        raw = await self.bot.guild_config.value(ctx.guild.id, "lockdown_channels")
        try:
            configured = [int(c) for c in json.loads(raw or "[]")]
        except (ValueError, TypeError):
            configured = []

        if configured:
            channels = [
                c for c in (ctx.guild.get_channel(cid) for cid in configured)
                if isinstance(c, discord.TextChannel)
            ]
        else:
            # No set configured: every text channel the bot can actually manage.
            channels = [
                c for c in ctx.guild.text_channels
                if c.permissions_for(ctx.guild.me).manage_channels
            ]

        if not channels:
            raise DeezeeError(
                "No channels to act on. Pick a lockdown set in `?config` -> Moderation."
            )

        verb = "Lock" if state == "start" else "Unlock"
        if not await confirm(
            ctx,
            title=f"{verb} {len(channels)} channel(s)?",
            description=(
                f"{verb} every channel in the lockdown set. "
                + ("Members will not be able to talk." if state == "start"
                   else "Previous permissions will be restored exactly.")
            ),
            confirm_label=f"{verb} {len(channels)}",
            fields={"Reason": reason[:1000]},
        ):
            return

        changed = 0
        for index, channel in enumerate(channels, start=1):
            try:
                if state == "start":
                    changed += await self._lock_channel(
                        channel, ctx.author, reason=reason, from_lockdown=True
                    )
                else:
                    changed += await self._restore_channel(channel, reason=reason)
            except commands.BotMissingPermissions:
                continue
            # Paced so a fifty-channel lockdown does not burn the rate limit.
            if index % 5 == 0:
                await asyncio.sleep(BULK_ACTION_DELAY)

        await modlog.create_case(
            self.bot, ctx.guild, action="lockdown", target=ctx.author,
            moderator=ctx.author, reason=f"{state}: {reason}",
        )
        await self._respond(
            ctx,
            self._ok(
                f"{verb}ed **{changed}** of {len(channels)} channel(s). "
                "Channels already in that state were skipped."
            ),
        )

    @commands.hybrid_command(name="nuke", aliases=["clone"])
    @app_commands.describe(channel="Channel to wipe. Defaults to this one")
    @permissions.admin_only()
    @permissions.guild_only()
    async def nuke(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Clone a channel and delete the original, wiping all history."""
        channel = channel or ctx.channel

        if not ctx.guild.me.guild_permissions.manage_channels:
            raise commands.BotMissingPermissions(["manage_channels"])

        # Two confirmations. This is the only command in the bot that destroys
        # message history with no recovery path of any kind.
        if not await confirm(
            ctx,
            title=f"Nuke {channel.name}?",
            description=(
                f"**{channel.mention} will be deleted and recreated.**\n\n"
                "Every message in it is destroyed permanently. Permissions, topic, "
                "slowmode and position are kept. Pins, webhooks and threads are not."
            ),
            confirm_label="Continue",
        ):
            return

        if not await confirm(
            ctx,
            title="Are you certain?",
            description=(
                f"This is irreversible. There is no undo and no backup.\n"
                f"Channel: {channel.mention}"
            ),
            confirm_label="Nuke it",
        ):
            return

        position = channel.position
        try:
            clone = await channel.clone(reason=f"Nuked by {ctx.author} ({ctx.author.id})")
            await channel.delete(reason=f"Nuked by {ctx.author} ({ctx.author.id})")
            await clone.edit(position=position)
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["manage_channels"]) from None

        # The lock row referenced a channel that no longer exists.
        await self.bot.db.execute(
            "DELETE FROM channel_locks WHERE guild_id = ? AND channel_id = ?",
            (ctx.guild.id, channel.id),
        )

        await modlog.create_case(
            self.bot, ctx.guild, action="nuke", target=ctx.author,
            moderator=ctx.author, reason=f"#{channel.name}",
        )
        await clone.send(
            embed=self._ok(
                f"Channel nuked by {ctx.author.mention}. All previous messages are gone."
            )
        )

        # Nuking a different channel leaves the invoker looking at a channel that
        # was never told anything. The original channel no longer exists when
        # they nuked the one they were standing in, so only answer there when it
        # is a different channel.
        if ctx.channel.id != channel.id:
            await self._respond(
                ctx, self._ok(f"Nuked **#{channel.name}**. It is now {clone.mention}.")
            )

    # =======================================================================
    # Member and voice
    # =======================================================================

    @commands.hybrid_command(name="nickname", aliases=["nick", "setnick"])
    @app_commands.describe(
        target="Member to rename", nickname="New nickname. Leave empty to clear it"
    )
    @permissions.mod_only()
    @permissions.guild_only()
    async def nickname(
        self, ctx: commands.Context, target: discord.Member, *, nickname: str = ""
    ) -> None:
        """Change or clear a member's nickname."""
        await permissions.enforce_hierarchy(
            ctx, target, action="nickname", bot_permission="manage_nicknames"
        )

        nickname = nickname.strip()
        if len(nickname) > 32:
            raise DeezeeError("Nicknames cannot be longer than 32 characters.")

        before = target.display_name
        try:
            await target.edit(
                nick=nickname or None, reason=f"Nickname change by {ctx.author}"
            )
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["manage_nicknames"]) from None

        await modlog.create_case(
            self.bot, ctx.guild, action="nickname", target=target,
            moderator=ctx.author, reason=f"{before} -> {nickname or target.name}",
            post=False,
        )

        if nickname:
            await ctx.send(embed=self._ok(f"Renamed **{before}** to **{nickname}**."))
        else:
            await ctx.send(embed=self._ok(f"Cleared **{before}**'s nickname."))

    @commands.hybrid_command(name="voicekick", aliases=["vckick"])
    @app_commands.describe(target="Member to disconnect", reason="Why")
    @permissions.mod_only()
    @permissions.guild_only()
    async def voicekick(
        self, ctx: commands.Context, target: discord.Member, *, reason: str = "No reason given"
    ) -> None:
        """Disconnect a member from their voice channel."""
        await permissions.enforce_hierarchy(
            ctx, target, action="voicekick", bot_permission="move_members"
        )

        if target.voice is None or target.voice.channel is None:
            raise DeezeeError(f"**{target}** is not in a voice channel.")

        channel_name = target.voice.channel.name
        try:
            await target.move_to(None, reason=f"{ctx.author}: {reason}")
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["move_members"]) from None

        await modlog.create_case(
            self.bot, ctx.guild, action="voicekick", target=target,
            moderator=ctx.author, reason=reason,
        )
        await ctx.send(
            embed=self._ok(f"Disconnected **{target}** from **{channel_name}**.")
        )

    @commands.hybrid_command(name="voicemute", aliases=["vcmute"])
    @app_commands.describe(target="Member to server-mute", reason="Why")
    @permissions.mod_only()
    @permissions.guild_only()
    async def voicemute(
        self, ctx: commands.Context, target: discord.Member, *, reason: str = "No reason given"
    ) -> None:
        """Server-mute a member in voice, or unmute them if already muted."""
        await permissions.enforce_hierarchy(
            ctx, target, action="voicemute", bot_permission="mute_members"
        )

        if target.voice is None:
            raise DeezeeError(
                f"**{target}** is not connected to voice. "
                "A server mute can only be applied to a connected member."
            )

        muting = not target.voice.mute
        try:
            await target.edit(mute=muting, reason=f"{ctx.author}: {reason}")
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["mute_members"]) from None

        await modlog.create_case(
            self.bot, ctx.guild,
            action="voicemute" if muting else "voiceunmute",
            target=target, moderator=ctx.author, reason=reason,
        )
        verb = "Server-muted" if muting else "Server-unmuted"
        await ctx.send(embed=self._ok(f"{verb} **{target}**."))

    @commands.hybrid_command(name="deafen", with_app_command=False, aliases=["undeafen"])
    @app_commands.describe(target="Member to deafen or undeafen", reason="Why")
    @permissions.mod_only()
    @permissions.guild_only()
    async def deafen(
        self, ctx: commands.Context, target: discord.Member, *, reason: str = "No reason given"
    ) -> None:
        """Server-deafen or undeafen a member in voice. Toggles on current state."""
        await permissions.enforce_hierarchy(
            ctx, target, action="deafen", bot_permission="deafen_members"
        )

        if target.voice is None:
            raise DeezeeError(
                f"**{target}** is not connected to voice. "
                "A server deafen can only be applied to a connected member."
            )

        deafening = not target.voice.deaf
        try:
            await target.edit(deafen=deafening, reason=f"{ctx.author}: {reason}")
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["deafen_members"]) from None

        await modlog.create_case(
            self.bot, ctx.guild,
            action="deafen" if deafening else "undeafen",
            target=target, moderator=ctx.author, reason=reason,
        )
        verb = "Deafened" if deafening else "Undeafened"
        await ctx.send(embed=self._ok(f"{verb} **{target}**."))

    # =======================================================================
    # Ban message
    # =======================================================================

    @commands.hybrid_command(name="banmessage", with_app_command=False)
    @app_commands.describe(
        message="New DM text. Leave empty to open an editor, or use 'reset' for the default"
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def banmessage(self, ctx: commands.Context, *, message: str = "") -> None:
        """Set the DM sent to a member when they are banned."""
        current = await self.bot.guild_config.value(ctx.guild.id, "ban_message")
        message = message.strip()

        if message.lower() in {"reset", "default"}:
            await self.bot.guild_config.set(ctx.guild.id, "ban_message", None)
            await ctx.send(embed=self._ok("Ban DM reset to the default text."))
            return

        if not message:
            if ctx.interaction is not None:
                await ctx.interaction.response.send_modal(BanMessageModal(self, current))
                return

            preview = self._render_ban_message(
                current or DEFAULT_BAN_MESSAGE,
                target=ctx.author, guild=ctx.guild, moderator=ctx.author,
                reason="Example reason", duration=None,
            )
            embed = self._info(
                f"**Current template**\n```\n{current or DEFAULT_BAN_MESSAGE}\n```\n"
                f"**Preview**\n{preview}\n\n"
                "Variables: `{user}` `{server}` `{reason}` `{moderator}` `{duration}`\n"
                "Set a new one with `?banmessage <text>`, or `?banmessage reset`.",
                title="Ban DM message",
            )
            await ctx.send(embed=embed)
            return

        await self.bot.guild_config.set(ctx.guild.id, "ban_message", message)
        preview = self._render_ban_message(
            message, target=ctx.author, guild=ctx.guild, moderator=ctx.author,
            reason="Example reason", duration=None,
        )
        embed = self._ok("Ban DM saved.", title="Ban DM message")
        embed.add_field(name="Preview", value=preview[:1000], inline=False)
        await ctx.send(embed=embed)

    async def save_ban_message(self, interaction: discord.Interaction, body: str) -> None:
        """Persist a ban message submitted through the modal."""
        await self.bot.guild_config.set(interaction.guild.id, "ban_message", body)
        preview = self._render_ban_message(
            body, target=interaction.user, guild=interaction.guild,
            moderator=interaction.user, reason="Example reason", duration=None,
        )
        embed = self._ok("Ban DM saved.", title="Ban DM message")
        embed.add_field(name="Preview", value=preview[:1000], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class _StubUser:
    """Stand-in for a user object the bot cannot resolve.

    Mass bans and expiring punishments frequently involve accounts that are gone
    or were never cached. A case still needs an ID and a printable tag, and
    fetching each one would be an API call per row for no benefit.
    """

    __slots__ = ("id",)

    def __init__(self, user_id: int) -> None:
        self.id = user_id

    def __str__(self) -> str:
        return f"Unknown User ({self.id})"


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point."""
    await bot.add_cog(Moderation(bot))
