"""XP, levels, level roles and rank cards.

Covers 9 of the 19 commands in the command sheet's "Leveling & Economy"
section; the other 10 are in ``cogs/economy.py``, because the two share only a
user ID and splitting them means the economy can be turned off without losing
anyone's XP.

Two design points worth stating.

**The cooldown lives in the database, not in memory.** A restart is otherwise a
free XP farm: hold a message, wait for the bot to restart, send it. One column
costs nothing and closes that.

**Levels are derived from XP, never stored.** ``services/levelcurve.py`` owns the
formula, so changing the curve can never leave a stored level disagreeing with
the XP that produced it.
"""

from __future__ import annotations

import csv
import io
import logging
import random
import time
from typing import Any, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core import permissions
from core.constants import (
    COLOUR_DEFAULT,
    COLOUR_INFO,
    COLOUR_SUCCESS,
    EMOJI_SUCCESS,
    IMPORT_BATCH_SIZE,
)
from core.errors import DeezeeError, ServiceUnavailable
from services import levelcard, levelcurve
from ui.paginator import Paginator, paginate_lines
from ui.panels.leveling import open_panel
from ui.views import confirm

log = logging.getLogger(__name__)

#: How often voice time is banked, in seconds. Voice XP is granted from the
#: length of a session when it ends rather than on a ticking timer, so an idle
#: voice channel costs nothing at all.
VOICE_MIN_SECONDS = 60

#: MEE6's public leaderboard endpoint. Unofficial, frequently rate-limited, and
#: only works if the server has set its leaderboard public.
MEE6_URL = "https://mee6.xyz/api/plugins/levels/leaderboard/{guild_id}"

#: Pages of 1000 pulled from MEE6 before giving up.
MEE6_MAX_PAGES = 10

#: Seconds before an external leaderboard request is abandoned.
HTTP_TIMEOUT = 20


class Leveling(commands.Cog):
    """Message and voice XP, level roles, rank cards and leaderboards."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        #: When each member joined their current voice channel, per guild.
        #: In memory: a lost session on restart costs at most a few minutes of
        #: voice XP, which is not worth a write per member per minute.
        self._voice_since: dict[tuple[int, int], float] = {}
        #: Cached XP blacklists, keyed by guild.
        self._blacklist: dict[int, set[tuple[int, str]]] = {}

    async def open_config_panel(self, interaction: discord.Interaction) -> None:
        """Open this cog's panel from the ``?config`` root panel."""
        from ui.panels.leveling import LevelingPanel

        config = await self.bot.guild_config.get(interaction.guild.id)
        roles = await self.load_level_roles(interaction.guild.id)
        view = LevelingPanel(self, interaction.user, config, roles)
        await interaction.response.send_message(
            embed=view.embed(interaction.guild), view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    # =======================================================================
    # State
    # =======================================================================

    async def load_level_roles(self, guild_id: int) -> list[dict[str, Any]]:
        rows = await self.bot.db.fetchall(
            "SELECT level, role_id FROM level_roles WHERE guild_id = ? ORDER BY level",
            (guild_id,),
        )
        return [dict(row) for row in rows]

    async def blacklist(self, guild_id: int) -> set[tuple[int, str]]:
        cached = self._blacklist.get(guild_id)
        if cached is not None:
            return cached
        rows = await self.bot.db.fetchall(
            "SELECT entity_id, entity_type FROM xp_blacklist WHERE guild_id = ?",
            (guild_id,),
        )
        result = {(row["entity_id"], row["entity_type"]) for row in rows}
        self._blacklist[guild_id] = result
        return result

    def invalidate(self, guild_id: int) -> None:
        self._blacklist.pop(guild_id, None)

    async def _is_blocked(self, member: discord.Member, channel: Any) -> bool:
        """Whether this member, in this channel, earns nothing."""
        entries = await self.blacklist(member.guild.id)
        if not entries:
            return False
        if (channel.id, "channel") in entries:
            return True
        category = getattr(channel, "category_id", None)
        if category and (category, "channel") in entries:
            return True
        return any((role.id, "role") in entries for role in member.roles)

    async def get_row(self, guild_id: int, user_id: int) -> dict[str, Any]:
        """One member's XP row, created at zero if absent."""
        row = await self.bot.db.fetchone(
            "SELECT * FROM levels WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        if row is None:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO levels (guild_id, user_id) VALUES (?, ?)",
                (guild_id, user_id),
            )
            return {
                "guild_id": guild_id, "user_id": user_id, "xp": 0,
                "messages": 0, "voice_seconds": 0, "last_award_at": 0,
            }
        return dict(row)

    async def rank_of(self, guild_id: int, user_id: int) -> tuple[int, int]:
        """Return ``(rank, total_ranked)`` for one member. Rank 1 is highest."""
        total = int(
            await self.bot.db.fetchval(
                "SELECT COUNT(*) FROM levels WHERE guild_id = ? AND xp > 0", (guild_id,)
            )
            or 0
        )
        xp = await self.bot.db.fetchval(
            "SELECT xp FROM levels WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        if not xp:
            return total + 1, max(total, 1)
        above = int(
            await self.bot.db.fetchval(
                "SELECT COUNT(*) FROM levels WHERE guild_id = ? AND xp > ?",
                (guild_id, xp),
            )
            or 0
        )
        return above + 1, max(total, 1)

    # =======================================================================
    # Awarding
    # =======================================================================

    @commands.Cog.listener("on_message")
    async def award_message_xp(self, message: discord.Message) -> None:
        """Grant XP for a message, subject to the cooldown and the blacklist."""
        if message.guild is None or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return
        if not await self.bot.guild_config.flag(message.guild.id, "xp_enabled"):
            return
        # A command invocation is a request, not conversation. Counting it would
        # make ?help the cheapest way to level up.
        context = await self.bot.get_context(message)
        if context.valid:
            return
        if await self._is_blocked(message.author, message.channel):
            return

        config = await self.bot.guild_config.get(message.guild.id)
        now = int(time.time())
        row = await self.get_row(message.guild.id, message.author.id)

        if now - int(row["last_award_at"]) < int(config["xp_cooldown"]):
            # Still count the message; only the XP is on cooldown.
            await self.bot.db.execute(
                "UPDATE levels SET messages = messages + 1 "
                "WHERE guild_id = ? AND user_id = ?",
                (message.guild.id, message.author.id),
            )
            return

        gain = random.randint(int(config["xp_min"]), int(config["xp_max"]))
        gain *= max(1, int(config["xp_multiplier"]))

        before = levelcurve.level_from_xp(int(row["xp"]))
        after_xp = int(row["xp"]) + gain

        await self.bot.db.execute(
            "UPDATE levels SET xp = ?, messages = messages + 1, last_award_at = ? "
            "WHERE guild_id = ? AND user_id = ?",
            (after_xp, now, message.guild.id, message.author.id),
        )

        after = levelcurve.level_from_xp(after_xp)
        if after > before:
            await self.handle_level_up(message.author, after, after_xp, message.channel)

    @commands.Cog.listener("on_voice_state_update")
    async def award_voice_xp(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Bank voice time when a session ends, and grant XP for it.

        Session length is measured on exit rather than counted by a timer. An
        idle voice channel then costs exactly nothing, which is the whole point
        against the CPU budget.
        """
        key = (member.guild.id, member.id)

        def qualifies(state: discord.VoiceState) -> bool:
            # AFK and self-deafened members are present but not participating.
            if state.channel is None:
                return False
            if state.channel == member.guild.afk_channel:
                return False
            return not state.self_deaf and not state.deaf

        was, now_ok = qualifies(before), qualifies(after)

        if not was and now_ok:
            self._voice_since[key] = time.monotonic()
            return

        if was and not now_ok:
            started = self._voice_since.pop(key, None)
            if started is None:
                return
            seconds = int(time.monotonic() - started)
            if seconds < VOICE_MIN_SECONDS:
                return

            await self.bot.db.execute(
                "INSERT INTO levels (guild_id, user_id, voice_seconds) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "voice_seconds = voice_seconds + excluded.voice_seconds",
                (member.guild.id, member.id, seconds),
            )

            config = await self.bot.guild_config.get(member.guild.id)
            if not config["voice_xp_enabled"] or not config["xp_enabled"]:
                return
            if before.channel is not None and await self._is_blocked(
                member, before.channel
            ):
                return

            gain = (seconds // 60) * int(config["voice_xp_rate"])
            gain *= max(1, int(config["xp_multiplier"]))
            if gain <= 0:
                return

            row = await self.get_row(member.guild.id, member.id)
            was_level = levelcurve.level_from_xp(int(row["xp"]))
            after_xp = int(row["xp"]) + gain
            await self.bot.db.execute(
                "UPDATE levels SET xp = ? WHERE guild_id = ? AND user_id = ?",
                (after_xp, member.guild.id, member.id),
            )
            new_level = levelcurve.level_from_xp(after_xp)
            if new_level > was_level:
                await self.handle_level_up(member, new_level, after_xp, None)

    async def handle_level_up(
        self,
        member: discord.Member,
        level: int,
        total_xp: int,
        channel: Any,
    ) -> None:
        """Announce a level-up and apply any level roles."""
        await self.apply_level_roles(member, level)

        config = await self.bot.guild_config.get(member.guild.id)
        mode = config["level_up_mode"]
        if mode == "off":
            return

        text = (
            str(config["level_up_message"])
            .replace("{mention}", member.mention)
            .replace("{user}", member.display_name)
            .replace("{level}", str(level))
            .replace("{xp}", str(total_xp))
            .replace("{server}", member.guild.name)
        )

        destination: Any = None
        if mode == "dm":
            destination = member
        elif mode == "channel":
            channel_id = config["level_up_channel_id"]
            destination = (
                member.guild.get_channel(int(channel_id)) if channel_id else None
            )
        elif mode == "current":
            destination = channel

        if destination is None:
            return

        try:
            await destination.send(
                text, allowed_mentions=discord.AllowedMentions(users=True)
            )
        except discord.HTTPException:
            # A closed DM or a locked channel must not undo the level-up itself.
            log.debug("Could not announce a level-up in guild %s", member.guild.id)

    async def apply_level_roles(self, member: discord.Member, level: int) -> None:
        """Grant the roles earned at this level, and honour the stacking setting."""
        rows = await self.load_level_roles(member.guild.id)
        if not rows:
            return

        stack = await self.bot.guild_config.flag(member.guild.id, "level_role_stack")
        earned = [row for row in rows if row["level"] <= level]
        if not earned:
            return

        keep = earned if stack else earned[-1:]
        keep_ids = {row["role_id"] for row in keep}

        to_add: list[discord.Role] = []
        to_remove: list[discord.Role] = []

        for row in rows:
            role = member.guild.get_role(row["role_id"])
            if role is None:
                continue
            usable, _ = permissions.can_manage_role(member.guild, role)
            if not usable:
                continue
            has = role in member.roles
            wanted = role.id in keep_ids
            if wanted and not has:
                to_add.append(role)
            elif not wanted and has and row["level"] <= level:
                # Only ever removes a *level* role that this member has outgrown,
                # never one they were given for some other reason.
                to_remove.append(role)

        try:
            if to_add:
                await member.add_roles(*to_add, reason=f"Reached level {level}")
            if to_remove and not stack:
                await member.remove_roles(*to_remove, reason="Level role replaced")
        except discord.HTTPException as exc:
            log.warning("Level role change failed in %s: %s", member.guild.id, exc)

    # =======================================================================
    # Commands
    # =======================================================================

    @commands.hybrid_command(name="rank", aliases=["level", "xp"])
    @app_commands.describe(target="Whose rank to show. Defaults to you")
    @commands.cooldown(1, 10, commands.BucketType.member)
    @permissions.guild_only()
    async def rank(
        self, ctx: commands.Context, target: Optional[discord.Member] = None
    ) -> None:
        """Show a level, XP, progress bar and server position as a rendered card."""
        member = target or ctx.author
        if member.bot:
            raise DeezeeError("Bots do not earn XP.")

        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.defer()

        row = await self.get_row(ctx.guild.id, member.id)
        total_xp = int(row["xp"])
        level, into, needed = levelcurve.progress(total_xp)
        rank, ranked = await self.rank_of(ctx.guild.id, member.id)

        avatar: bytes | None = None
        try:
            avatar = await member.display_avatar.replace(size=128, format="png").read()
        except (discord.HTTPException, discord.NotFound):
            avatar = None

        try:
            png = await levelcard.render_card(
                name=member.display_name,
                level=level,
                rank=rank,
                total_members=ranked,
                xp_into_level=into,
                xp_for_level=needed,
                total_xp=total_xp,
                messages=int(row["messages"]),
                avatar=avatar,
            )
        except Exception:
            # A rendering failure falls back to an embed rather than to nothing.
            # The numbers are the point; the picture is decoration.
            log.exception("Rank card rendering failed")
            await ctx.send(embed=self._rank_embed(member, level, rank, ranked,
                                                  into, needed, total_xp, row))
            return

        await ctx.send(
            file=discord.File(io.BytesIO(png), filename=f"rank-{member.id}.png")
        )

    @staticmethod
    def _rank_embed(
        member: discord.Member, level: int, rank: int, ranked: int,
        into: int, needed: int, total_xp: int, row: dict[str, Any],
    ) -> discord.Embed:
        """Plain-embed fallback for when the card cannot be drawn."""
        filled = 0 if needed <= 0 else int(20 * min(1.0, into / needed))
        embed = discord.Embed(
            title=f"{member.display_name} — level {level}",
            description=f"`{'█' * filled}{'░' * (20 - filled)}`  {into}/{needed}",
            colour=COLOUR_INFO,
        )
        embed.add_field(name="Rank", value=f"#{rank} of {ranked}", inline=True)
        embed.add_field(name="Total XP", value=f"{total_xp:,}", inline=True)
        embed.add_field(name="Messages", value=f"{int(row['messages']):,}", inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text="The rank card could not be drawn; here are the numbers.")
        return embed

    @commands.hybrid_command(name="leaderboard", aliases=["lb", "top"])
    @commands.cooldown(1, 10, commands.BucketType.guild)
    @permissions.guild_only()
    async def leaderboard(self, ctx: commands.Context) -> None:
        """Paginated XP leaderboard for the server."""
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.defer()

        lines: list[str] = []
        position = 0
        async for row in self.bot.db.iterate(
            "SELECT user_id, xp, messages FROM levels WHERE guild_id = ? AND xp > 0 "
            "ORDER BY xp DESC LIMIT 500",
            (ctx.guild.id,),
        ):
            position += 1
            level = levelcurve.level_from_xp(int(row["xp"]))
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(position, f"`{position:>3}`")
            lines.append(
                f"{medal} <@{row['user_id']}> — level **{level}** "
                f"({int(row['xp']):,} XP, {int(row['messages']):,} msgs)"
            )

        pages = paginate_lines(
            lines,
            title=f"XP leaderboard — {ctx.guild.name}",
            per_page=10,
            colour=COLOUR_INFO,
            description="Top 500 by lifetime XP.",
            empty_message="Nobody has earned XP yet.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @commands.hybrid_command(name="levelconfig")
    @permissions.admin_only()
    @permissions.guild_only()
    async def levelconfig(self, ctx: commands.Context) -> None:
        """Open the leveling panel: rates, cooldown, announcements, roles, voice."""
        await open_panel(ctx, self)

    @commands.hybrid_group(name="levelrole", with_app_command=False, fallback="list")
    @permissions.admin_only()
    @permissions.guild_only()
    async def levelrole(self, ctx: commands.Context) -> None:
        """Roles awarded at a given level."""
        rows = await self.load_level_roles(ctx.guild.id)
        stack = await self.bot.guild_config.flag(ctx.guild.id, "level_role_stack")
        lines = []
        for row in rows:
            role = ctx.guild.get_role(row["role_id"])
            name = role.mention if role else f"*deleted role* `{row['role_id']}`"
            lines.append(f"Level **{row['level']}** → {name}")
        pages = paginate_lines(
            lines,
            title="Level roles",
            per_page=15,
            description=(
                f"Behaviour: **{'stacking' if stack else 'replacing'}** "
                "(change it in `?levelconfig`).\n"
                "`?levelrole add <level> <role>`"
            ),
            empty_message="No level roles configured.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @levelrole.command(name="add")
    @app_commands.describe(level="Level at which the role is granted", role="The role")
    @permissions.admin_only()
    @permissions.guild_only()
    async def levelrole_add(
        self, ctx: commands.Context, level: int, role: discord.Role
    ) -> None:
        """Grant a role when a member reaches a level."""
        if not 1 <= level <= levelcurve.MAX_LEVEL:
            raise DeezeeError(f"Level must be between 1 and {levelcurve.MAX_LEVEL}.")

        usable, why = permissions.can_manage_role(ctx.guild, role)
        if not usable:
            raise DeezeeError(why)

        await self.bot.db.execute(
            "INSERT INTO level_roles (guild_id, level, role_id) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, level) DO UPDATE SET role_id = excluded.role_id",
            (ctx.guild.id, level, role.id),
        )
        needed = levelcurve.total_xp_for_level(level)
        await ctx.send(
            embed=discord.Embed(
                description=(
                    f"{EMOJI_SUCCESS}  {role.mention} is granted at **level {level}** "
                    f"({needed:,} XP).\n"
                    "Members already past that level get it on their next level-up "
                    "or when you run `?levelrole sync`."
                ),
                colour=COLOUR_SUCCESS,
            )
        )

    @levelrole.command(name="remove")
    @app_commands.describe(level="Level whose reward to remove")
    @permissions.admin_only()
    @permissions.guild_only()
    async def levelrole_remove(self, ctx: commands.Context, level: int) -> None:
        """Stop granting a role at a level."""
        cursor = await self.bot.db.execute(
            "DELETE FROM level_roles WHERE guild_id = ? AND level = ?",
            (ctx.guild.id, level),
        )
        if not cursor.rowcount:
            raise DeezeeError(f"No role is granted at level {level}.")
        await ctx.send(
            embed=discord.Embed(
                description=f"{EMOJI_SUCCESS}  Level {level} no longer grants a role. "
                "Members who already have it keep it.",
                colour=COLOUR_SUCCESS,
            )
        )

    @levelrole.command(name="sync")
    @permissions.admin_only()
    @permissions.guild_only()
    async def levelrole_sync(self, ctx: commands.Context) -> None:
        """Give every member the level roles their XP already earns them.

        Needed after adding a level role: existing members are already past that
        level and would otherwise wait until their next level-up.
        """
        rows = await self.load_level_roles(ctx.guild.id)
        if not rows:
            raise DeezeeError("No level roles are configured.")

        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.defer()

        ranked = await self.bot.db.fetchall(
            "SELECT user_id, xp FROM levels WHERE guild_id = ? AND xp > 0", (ctx.guild.id,)
        )
        proceed = await confirm(
            ctx,
            title=f"Sync level roles for {len(ranked)} member(s)?",
            description=(
                f"Every member with XP will be given the level roles their level "
                f"already earns. This runs {len(ranked)} role check(s) and can take "
                "a while."
            ),
            confirm_label="Sync",
            danger=False,
        )
        if not proceed:
            return

        changed = 0
        for row in ranked:
            member = ctx.guild.get_member(row["user_id"])
            if member is None:
                continue
            level = levelcurve.level_from_xp(int(row["xp"]))
            before = {r.id for r in member.roles}
            await self.apply_level_roles(member, level)
            if {r.id for r in member.roles} != before:
                changed += 1

        await ctx.send(
            embed=discord.Embed(
                description=f"{EMOJI_SUCCESS}  Synced. **{changed}** member(s) changed.\n"
                "Members not currently cached were skipped -- they are picked up "
                "on their next message.",
                colour=COLOUR_SUCCESS,
            )
        )

    @commands.hybrid_group(name="xpblacklist", with_app_command=False, fallback="list")
    @permissions.admin_only()
    @permissions.guild_only()
    async def xpblacklist(self, ctx: commands.Context) -> None:
        """Channels and roles that earn no XP."""
        entries = await self.blacklist(ctx.guild.id)
        lines = []
        for entity_id, kind in sorted(entries, key=lambda e: e[1]):
            mention = f"<#{entity_id}>" if kind == "channel" else f"<@&{entity_id}>"
            lines.append(f"{mention} — {kind}")
        pages = paginate_lines(
            lines,
            title="XP blacklist",
            per_page=15,
            description="`?xpblacklist add #channel` or `?xpblacklist add @role`.",
            empty_message="Nothing is excluded from earning XP.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @staticmethod
    async def _resolve_channel_or_role(
        ctx: commands.Context, query: str
    ) -> tuple[Any, str]:
        """Turn a mention, ID or name into a channel or a role.

        A plain string rather than two typed options because an application
        command parameter cannot be "a channel or a role" -- Discord has no such
        option type, and declaring one raises at registration.
        """
        try:
            channel = await commands.GuildChannelConverter().convert(ctx, query)
            return channel, "channel"
        except commands.BadArgument:
            pass
        try:
            return await commands.RoleConverter().convert(ctx, query), "role"
        except commands.BadArgument:
            raise DeezeeError(
                f"`{query}` is not a channel or role I can find here."
            ) from None

    @xpblacklist.command(name="add")
    @app_commands.describe(target="Channel or role that should earn nothing")
    @permissions.admin_only()
    @permissions.guild_only()
    async def xpblacklist_add(self, ctx: commands.Context, *, target: str) -> None:
        """Exclude a channel or role from earning XP."""
        entity, kind = await self._resolve_channel_or_role(ctx, target)
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO xp_blacklist (guild_id, entity_id, entity_type) "
            "VALUES (?, ?, ?)",
            (ctx.guild.id, entity.id, kind),
        )
        self.invalidate(ctx.guild.id)
        await ctx.send(
            embed=discord.Embed(
                description=f"{EMOJI_SUCCESS}  {entity.mention} earns no XP.",
                colour=COLOUR_SUCCESS,
            )
        )

    @xpblacklist.command(name="remove")
    @app_commands.describe(target="Channel or role that should earn XP again")
    @permissions.admin_only()
    @permissions.guild_only()
    async def xpblacklist_remove(self, ctx: commands.Context, *, target: str) -> None:
        """Let a channel or role earn XP again."""
        entity, kind = await self._resolve_channel_or_role(ctx, target)
        cursor = await self.bot.db.execute(
            "DELETE FROM xp_blacklist WHERE guild_id = ? AND entity_id = ? "
            "AND entity_type = ?",
            (ctx.guild.id, entity.id, kind),
        )
        self.invalidate(ctx.guild.id)
        if not cursor.rowcount:
            raise DeezeeError(f"{entity.mention} was not blacklisted.")
        await ctx.send(
            embed=discord.Embed(
                description=f"{EMOJI_SUCCESS}  {entity.mention} earns XP again.",
                colour=COLOUR_SUCCESS,
            )
        )

    @commands.hybrid_command(name="setxp", aliases=["givexp"])
    @app_commands.describe(
        target="Whose XP to change", action="set, add or remove", amount="How much"
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def setxp(
        self, ctx: commands.Context, target: discord.Member, action: str, amount: int
    ) -> None:
        """Set, add or subtract a member's XP."""
        choice = action.lower()
        if choice not in {"set", "add", "remove", "subtract"}:
            raise DeezeeError("Action must be `set`, `add` or `remove`.")
        if amount < 0:
            raise DeezeeError("Amount cannot be negative -- use `remove` instead.")
        if target.bot:
            raise DeezeeError("Bots do not earn XP.")

        row = await self.get_row(ctx.guild.id, target.id)
        current = int(row["xp"])
        if choice == "set":
            new_xp = amount
        elif choice == "add":
            new_xp = current + amount
        else:
            new_xp = max(0, current - amount)

        await self.bot.db.execute(
            "UPDATE levels SET xp = ? WHERE guild_id = ? AND user_id = ?",
            (new_xp, ctx.guild.id, target.id),
        )

        before_level = levelcurve.level_from_xp(current)
        after_level = levelcurve.level_from_xp(new_xp)
        await self.apply_level_roles(target, after_level)

        embed = discord.Embed(
            description=(
                f"{EMOJI_SUCCESS}  {target.mention}: **{current:,}** → **{new_xp:,}** XP"
            ),
            colour=COLOUR_SUCCESS,
        )
        embed.add_field(
            name="Level", value=f"{before_level} → {after_level}", inline=True
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="resetxp")
    @app_commands.describe(target="A member, or the word 'all' for the whole server")
    @permissions.admin_only()
    @permissions.guild_only()
    async def resetxp(self, ctx: commands.Context, *, target: str) -> None:
        """Reset XP for one member or the whole server."""
        if target.strip().lower() in {"all", "everyone", "server"}:
            total = int(
                await self.bot.db.fetchval(
                    "SELECT COUNT(*) FROM levels WHERE guild_id = ? AND xp > 0",
                    (ctx.guild.id,),
                )
                or 0
            )
            proceed = await confirm(
                ctx,
                title="Reset XP for the whole server?",
                description=(
                    f"**{total}** member(s) will lose all their XP, levels and "
                    "message counts.\n\n**This cannot be undone.** There is no "
                    "backup of leveling data -- export it first with `?configexport` "
                    "if you might want it back."
                ),
                confirm_label=f"Wipe XP for {total} member(s)",
                fields={"Affected": str(total)},
            )
            if not proceed:
                return

            await self.bot.db.execute(
                "DELETE FROM levels WHERE guild_id = ?", (ctx.guild.id,)
            )
            await ctx.send(
                embed=discord.Embed(
                    description=f"{EMOJI_SUCCESS}  Reset XP for **{total}** member(s).",
                    colour=COLOUR_SUCCESS,
                )
            )
            return

        member = await commands.MemberConverter().convert(ctx, target.strip())
        await self.bot.db.execute(
            "DELETE FROM levels WHERE guild_id = ? AND user_id = ?",
            (ctx.guild.id, member.id),
        )
        await ctx.send(
            embed=discord.Embed(
                description=f"{EMOJI_SUCCESS}  Reset {member.mention}'s XP.",
                colour=COLOUR_SUCCESS,
            )
        )

    @commands.hybrid_command(name="voicexp", with_app_command=False)
    @app_commands.describe(
        action="on, off, rate or status", amount="XP per minute, for the rate action"
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def voicexp(
        self, ctx: commands.Context, action: str = "status", amount: int = 0
    ) -> None:
        """Toggle XP for voice time and set the per-minute rate."""
        choice = action.lower()
        config = await self.bot.guild_config.get(ctx.guild.id)

        if choice in {"on", "enable"}:
            await self.bot.guild_config.set(ctx.guild.id, "voice_xp_enabled", 1)
            await ctx.send(
                embed=discord.Embed(
                    description=f"{EMOJI_SUCCESS}  Voice XP is **on** at "
                    f"**{config['voice_xp_rate']}** XP per minute.\n"
                    "The AFK channel and self-deafened members earn nothing.",
                    colour=COLOUR_SUCCESS,
                )
            )
            return

        if choice in {"off", "disable"}:
            await self.bot.guild_config.set(ctx.guild.id, "voice_xp_enabled", 0)
            await ctx.send(
                embed=discord.Embed(
                    description=f"{EMOJI_SUCCESS}  Voice XP is **off**.",
                    colour=COLOUR_SUCCESS,
                )
            )
            return

        if choice == "rate":
            if not 0 < amount <= 100:
                raise DeezeeError("The rate must be between 1 and 100 XP per minute.")
            await self.bot.guild_config.set(ctx.guild.id, "voice_xp_rate", amount)
            await ctx.send(
                embed=discord.Embed(
                    description=f"{EMOJI_SUCCESS}  Voice XP is now **{amount}** "
                    "per minute.",
                    colour=COLOUR_SUCCESS,
                )
            )
            return

        if choice != "status":
            raise DeezeeError("Use `on`, `off`, `rate <amount>` or `status`.")

        embed = discord.Embed(
            title="Voice XP",
            colour=COLOUR_SUCCESS if config["voice_xp_enabled"] else COLOUR_DEFAULT,
        )
        embed.add_field(
            name="Status",
            value="On" if config["voice_xp_enabled"] else "Off",
            inline=True,
        )
        embed.add_field(
            name="Rate", value=f"{config['voice_xp_rate']} XP/minute", inline=True
        )
        embed.add_field(
            name="Not counted",
            value="The AFK channel, self-deafened members, and sessions under "
            f"{VOICE_MIN_SECONDS} seconds.",
            inline=False,
        )
        embed.add_field(
            name="In progress",
            value=f"{sum(1 for k in self._voice_since if k[0] == ctx.guild.id)} "
            "session(s) being timed right now.",
            inline=False,
        )
        await ctx.send(embed=embed)

    # =======================================================================
    # ?importlevels
    # =======================================================================

    @commands.hybrid_command(name="importlevels", with_app_command=False, aliases=["mee6import"])
    @app_commands.describe(
        source="mee6 or csv",
        attachment="For csv: a file of user_id,xp",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def importlevels(
        self,
        ctx: commands.Context,
        source: str = "csv",
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        """Import a leveling table from another bot, applying it immediately.

        This writes straight to live XP. ``?import levels`` in the migration
        panel stages the same data into a draft for review instead -- use that
        one if you want to look before it lands.
        """
        choice = source.lower()
        if choice not in {"mee6", "csv"}:
            raise DeezeeError("Source must be `mee6` or `csv`.")

        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.defer()

        if choice == "csv":
            entries = await self._read_csv(ctx, attachment)
            note = "CSV file"
        else:
            entries = await self.fetch_mee6(ctx.guild.id)
            note = "MEE6 public leaderboard"

        if not entries:
            raise DeezeeError("No usable rows were found.")

        proceed = await confirm(
            ctx,
            title=f"Import {len(entries)} member(s) from {note}?",
            description=(
                f"**{len(entries)}** XP row(s) will be written into live leveling "
                "data. Existing XP for those members is **overwritten**, not added "
                "to.\nMembers not in the file keep whatever they have."
            ),
            confirm_label=f"Import {len(entries)}",
        )
        if not proceed:
            return

        written = await self.write_levels(ctx.guild.id, entries)
        await ctx.send(
            embed=discord.Embed(
                description=(
                    f"{EMOJI_SUCCESS}  Imported **{written}** member(s) from {note}.\n"
                    "Run `?levelrole sync` if level roles should be applied now."
                ),
                colour=COLOUR_SUCCESS,
            )
        )

    async def _read_csv(
        self, ctx: commands.Context, attachment: discord.Attachment | None
    ) -> list[tuple[int, int]]:
        """Parse a ``user_id,xp`` CSV attachment.

        Accepts an optional header row and tolerates a third column, which is
        what most exports carry.
        """
        if attachment is None and ctx.message is not None and ctx.message.attachments:
            attachment = ctx.message.attachments[0]
        if attachment is None:
            raise DeezeeError(
                "Attach a CSV file of `user_id,xp`. One row per member, header "
                "optional."
            )
        if attachment.size > 4 * 1024 * 1024:
            raise DeezeeError("That file is larger than 4 MiB.")

        raw = (await attachment.read()).decode("utf-8", errors="replace")
        entries: list[tuple[int, int]] = []
        for fields in csv.reader(io.StringIO(raw)):
            if len(fields) < 2:
                continue
            user_raw, xp_raw = fields[0].strip(), fields[1].strip()
            if not user_raw.isdigit() or not xp_raw.replace(".", "", 1).isdigit():
                continue  # Header row, or a line that is not data.
            entries.append((int(user_raw), int(float(xp_raw))))
        return entries

    async def fetch_mee6(self, guild_id: int) -> list[tuple[int, int]]:
        """Pull a leaderboard from MEE6's public endpoint.

        Unofficial and frequently unavailable: it only answers when the server's
        leaderboard has been made public in MEE6's own dashboard, and it
        rate-limits aggressively. Failure here is reported plainly rather than
        retried -- the CSV path always works.
        """
        entries: list[tuple[int, int]] = []
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for page in range(MEE6_MAX_PAGES):
                    async with session.get(
                        MEE6_URL.format(guild_id=guild_id),
                        params={"page": str(page), "limit": "1000"},
                    ) as response:
                        if response.status == 401:
                            raise ServiceUnavailable(
                                "The MEE6 leaderboard",
                                "This server's MEE6 leaderboard is private. Make it "
                                "public in MEE6's dashboard, or use the CSV path.",
                            )
                        if response.status == 429:
                            raise ServiceUnavailable(
                                "The MEE6 leaderboard",
                                "MEE6 is rate-limiting the request. Try again later, "
                                "or use the CSV path.",
                            )
                        if response.status != 200:
                            raise ServiceUnavailable(
                                "The MEE6 leaderboard",
                                f"It answered HTTP {response.status}.",
                            )
                        payload = await response.json()

                    players = payload.get("players") or []
                    if not players:
                        break
                    for player in players:
                        try:
                            entries.append(
                                (int(player["id"]), levelcurve.convert_from_mee6(
                                    int(player.get("xp", 0))
                                ))
                            )
                        except (KeyError, TypeError, ValueError):
                            continue
        except aiohttp.ClientError as exc:
            raise ServiceUnavailable("The MEE6 leaderboard", str(exc)) from exc

        return entries

    async def write_levels(
        self, guild_id: int, entries: list[tuple[int, int]]
    ) -> int:
        """Write imported XP in batches, so peak memory stays flat."""
        written = 0
        batch: list[tuple[int, int, int]] = []

        for user_id, xp in entries:
            batch.append((guild_id, user_id, max(0, xp)))
            if len(batch) >= IMPORT_BATCH_SIZE:
                await self._flush_levels(batch)
                written += len(batch)
                batch.clear()

        if batch:
            await self._flush_levels(batch)
            written += len(batch)

        return written

    async def _flush_levels(self, batch: list[tuple[int, int, int]]) -> None:
        await self.bot.db.executemany(
            "INSERT INTO levels (guild_id, user_id, xp) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = excluded.xp",
            batch,
        )


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point."""
    await bot.add_cog(Leveling(bot))
