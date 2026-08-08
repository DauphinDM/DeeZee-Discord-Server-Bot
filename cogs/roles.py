"""Role management, reaction-role menus, autoroles, sticky roles and self-roles.

Covers the 15 commands in the command sheet's "Roles & Reaction Roles" section.

Two things in here are worth knowing before reading the code.

**Member counts are fetched, not cached.** ``chunk_guilds_at_startup`` is off to
stay inside the memory budget, so ``role.members`` would silently under-report.
Anything that counts members streams the member list over HTTP instead and is
rate-limited accordingly. A number that is quietly wrong is worse than one that
takes two seconds.

**Published menus use dynamic persistent items.** A button carries
``rr:<menu_id>:<option_id>`` in its custom_id and the handler is matched by
pattern on every boot, so one registration covers every menu that will ever
exist rather than one ``add_view`` per menu.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections import Counter
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import permissions
from core.constants import (
    BULK_ACTION_DELAY,
    COLOUR_DEFAULT,
    COLOUR_INFO,
    COLOUR_SUCCESS,
    COLOUR_WARNING,
    EMOJI_OFF,
    EMOJI_ON,
    EMOJI_SUCCESS,
    MAX_SELECT_OPTIONS,
    TS_LONG_DATE_TIME,
)
from core.errors import DeezeeError, NotConfigured
from services.timeparse import format_duration, parse_duration
from ui.paginator import Paginator, paginate_lines
from ui.panels.roles import MODES, open_builder, open_self_roles
from ui.views import confirm

log = logging.getLogger(__name__)

#: Members handled per batch in ``?massrole`` before pausing. Keeps a mass
#: action from pinning the CPU or burning the whole rate-limit bucket at once.
MASSROLE_BATCH = 10

#: Hard ceiling on one ``?massrole`` run. Above this the job would outlive the
#: interaction and most of the reasons for running it.
MASSROLE_MAX = 2000

#: Buttons Discord allows on one message. A menu above this must use a select.
MAX_MENU_BUTTONS = 25

#: Colour names accepted by ``?rolecolor`` in addition to hex.
NAMED_COLOURS: dict[str, int] = {
    "red": 0xE74C3C,
    "orange": 0xE67E22,
    "yellow": 0xF1C40F,
    "green": 0x2ECC71,
    "teal": 0x1ABC9C,
    "blue": 0x3498DB,
    "blurple": 0x5865F2,
    "purple": 0x9B59B6,
    "magenta": 0xE91E63,
    "pink": 0xFF69B4,
    "white": 0xFFFFFF,
    "black": 0x010101,
    "grey": 0x95A5A6,
    "gray": 0x95A5A6,
    "default": 0x000000,
}

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_colour(text: str) -> discord.Colour | None:
    """Turn ``#ff0000``, ``red`` or ``random`` into a colour.

    Returns ``None`` when the text is not a colour, so callers can say so rather
    than silently applying black.
    """
    cleaned = text.strip().lower()
    if cleaned in {"random", "rand"}:
        return discord.Colour(random.randint(0, 0xFFFFFF))
    if cleaned in NAMED_COLOURS:
        return discord.Colour(NAMED_COLOURS[cleaned])
    match = _HEX_RE.match(cleaned)
    if match:
        return discord.Colour(int(match.group(1), 16))
    return None


# ---------------------------------------------------------------------------
# Persistent menu components
# ---------------------------------------------------------------------------


class MenuRoleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"rr:(?P<menu>[0-9]+):(?P<option>[0-9]+)",
):
    """One role button on a published menu.

    A dynamic item rather than a stored view: the handler is matched from the
    custom_id on every interaction, so a menu created next year works after a
    restart without anything being registered for it specifically.
    """

    def __init__(
        self,
        menu_id: int,
        option_id: int,
        *,
        label: str,
        emoji: str | None = None,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        row: int | None = None,
    ) -> None:
        super().__init__(
            discord.ui.Button(
                label=label[:80] or "Role",
                emoji=emoji or None,
                style=style,
                custom_id=f"rr:{menu_id}:{option_id}",
            ),
            # Row belongs to the wrapper, not the wrapped button: DynamicItem
            # takes its own ``row`` and ignores the inner item's.
            row=row,
        )
        self.menu_id = menu_id
        self.option_id = option_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> MenuRoleButton:
        return cls(
            int(match["menu"]),
            int(match["option"]),
            label=item.label or "Role",
            emoji=str(item.emoji) if item.emoji else None,
            style=item.style,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Roles")
        if cog is None:  # The cog failed to load; say so rather than hang.
            await interaction.response.send_message(
                "Role menus are unavailable right now.", ephemeral=True
            )
            return
        await cog.handle_menu_choice(interaction, self.menu_id, [self.option_id])


class MenuRoleSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"rr:(?P<menu>[0-9]+):select",
):
    """The dropdown form of a published menu."""

    def __init__(
        self,
        menu_id: int,
        *,
        placeholder: str = "Pick your roles",
        options: list[discord.SelectOption] | None = None,
        max_values: int = 1,
    ) -> None:
        super().__init__(
            discord.ui.Select(
                custom_id=f"rr:{menu_id}:select",
                placeholder=placeholder,
                min_values=0,
                max_values=max_values,
                options=options or [discord.SelectOption(label="None", value="0")],
            )
        )
        self.menu_id = menu_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Select,
        match: re.Match[str],
    ) -> MenuRoleSelect:
        return cls(int(match["menu"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Roles")
        if cog is None:
            await interaction.response.send_message(
                "Role menus are unavailable right now.", ephemeral=True
            )
            return

        # Read the chosen values straight off the interaction payload rather than
        # off the rebuilt component: the payload is what Discord actually sent.
        raw = (interaction.data or {}).get("values") or []
        chosen = [int(value) for value in raw if value.isdigit() and value != "0"]
        await cog.handle_menu_choice(interaction, self.menu_id, chosen, exclusive=True)


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------


class Roles(commands.Cog):
    """Everything that grants, removes or organises roles."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        #: Guilds with a mass-role job in flight, so two cannot overlap.
        self._massrole_busy: set[int] = set()

    async def cog_load(self) -> None:
        self.bot.scheduler.register("temprole_remove", self._scheduled_temprole)
        self.bot.scheduler.register("autorole_grant", self._scheduled_autorole)

    async def cog_unload(self) -> None:
        for action in ("temprole_remove", "autorole_grant"):
            self.bot.scheduler.unregister(action)

    def register_persistent_views(self, bot: Any) -> None:
        """Re-attach handlers to menu components posted before the restart."""
        bot.add_dynamic_items(MenuRoleButton, MenuRoleSelect)

    # =======================================================================
    # Shared helpers
    # =======================================================================

    @staticmethod
    def _ok(description: str) -> discord.Embed:
        return discord.Embed(
            description=f"{EMOJI_SUCCESS}  {description}", colour=COLOUR_SUCCESS
        )

    @staticmethod
    async def _defer(ctx: commands.Context) -> None:
        """Acknowledge a slash command before slow work."""
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.defer()

    async def _respond(self, ctx: commands.Context, embed: discord.Embed, **kwargs: Any) -> None:
        """Report a result, falling back to a DM if the channel refuses us.

        A role command can remove the bot's own ability to post -- taking away a
        role that granted it Send Messages, for one -- and a command that worked
        but said nothing looks broken.
        """
        try:
            await ctx.send(embed=embed, **kwargs)
            return
        except discord.Forbidden:
            pass
        except discord.HTTPException as exc:
            log.warning("Could not respond in channel %s: %s", ctx.channel.id, exc)
            return

        try:
            await ctx.author.send(embed=embed)
        except discord.HTTPException:
            log.warning(
                "Command %s succeeded but could not be reported to %s",
                ctx.command, ctx.author.id,
            )

    def _require_usable(self, guild: discord.Guild, role: discord.Role) -> None:
        """Raise unless the bot can assign this role."""
        usable, why = permissions.can_manage_role(guild, role)
        if not usable:
            raise DeezeeError(why)

    async def _check_invoker_outranks(
        self, ctx: commands.Context, role: discord.Role
    ) -> None:
        """Refuse to hand out a role at or above the invoker's own position.

        Without this, any moderator could grant themselves Administrator by
        pointing ``?role`` at it. Bot owners and the server owner are exempt,
        since they sit outside the role tree by design.
        """
        if self.bot.config.is_owner(ctx.author.id):
            return
        if ctx.author.id == ctx.guild.owner_id:
            return
        if not isinstance(ctx.author, discord.Member):
            return
        if role >= ctx.author.top_role:
            raise DeezeeError(
                f"{role.mention} is at or above your highest role "
                f"({ctx.author.top_role.mention}), so you cannot hand it out."
            )

    async def _count_members_by_role(self, guild: discord.Guild) -> Counter[int]:
        """Count members per role by streaming the member list.

        Streaming rather than reading ``role.members``: startup chunking is off,
        so the cache holds only members the bot has seen and the cached count
        would be wrong without saying so. ``fetch_members`` does not populate the
        cache, which is what keeps this affordable against the memory budget.
        """
        counts: Counter[int] = Counter()
        async for member in guild.fetch_members(limit=None):
            for role in member.roles:
                counts[role.id] += 1
        return counts

    # =======================================================================
    # ?role
    # =======================================================================

    @commands.hybrid_command(name="role", aliases=["r"])
    @app_commands.describe(
        target="Member to change",
        role="Role to add or remove",
        action="add, remove, or leave blank to toggle",
    )
    @permissions.mod_only()
    @permissions.guild_only()
    async def role_cmd(
        self,
        ctx: commands.Context,
        target: discord.Member,
        role: discord.Role,
        action: Optional[str] = None,
    ) -> None:
        """Add or remove a role. With no action, toggles it.

        On the prefix version a role name containing spaces needs quotes, or use
        the role mention or ID.
        """
        await permissions.enforce_hierarchy(
            ctx, target, action="role", bot_permission="manage_roles"
        )
        self._require_usable(ctx.guild, role)
        await self._check_invoker_outranks(ctx, role)

        held = role in target.roles
        if action is None:
            adding = not held
        else:
            choice = action.lower()
            if choice in {"add", "give", "+"}:
                adding = True
            elif choice in {"remove", "take", "del", "-"}:
                adding = False
            else:
                raise DeezeeError(
                    f"`{action}` is not an action. Use `add`, `remove`, or nothing to toggle."
                )

        if adding and held:
            await self._respond(
                ctx,
                discord.Embed(
                    description=f"{target.mention} already has {role.mention}.",
                    colour=COLOUR_DEFAULT,
                ),
            )
            return
        if not adding and not held:
            await self._respond(
                ctx,
                discord.Embed(
                    description=f"{target.mention} does not have {role.mention}.",
                    colour=COLOUR_DEFAULT,
                ),
            )
            return

        reason = f"{ctx.author} via ?role"
        try:
            if adding:
                await target.add_roles(role, reason=reason)
            else:
                await target.remove_roles(role, reason=reason)
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["manage_roles"]) from None

        await self._respond(
            ctx,
            self._ok(
                f"{'Added' if adding else 'Removed'} {role.mention} "
                f"{'to' if adding else 'from'} {target.mention}."
            ),
        )

    # =======================================================================
    # ?massrole
    # =======================================================================

    @commands.hybrid_command(name="massrole", aliases=["roleall"])
    @app_commands.describe(
        action="add or remove",
        role="Role to apply",
        population="everyone, humans, bots, or in:<role name or ID>",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def massrole(
        self,
        ctx: commands.Context,
        action: str,
        role: discord.Role,
        *,
        population: str = "everyone",
    ) -> None:
        """Add or remove a role across a whole population.

        Runs as a throttled background job with a live progress embed rather than
        a tight loop, so a two-thousand-member change cannot pin the CPU.
        """
        choice = action.lower()
        if choice not in {"add", "remove"}:
            raise DeezeeError("First argument must be `add` or `remove`.")
        if ctx.guild.id in self._massrole_busy:
            raise DeezeeError("A mass-role job is already running in this server.")

        self._require_usable(ctx.guild, role)
        await self._check_invoker_outranks(ctx, role)

        await self._defer(ctx)

        filter_role: discord.Role | None = None
        target_spec = population.strip().lower()
        if target_spec.startswith("in:"):
            query = population.strip()[3:].strip()
            filter_role = await self._resolve_role(ctx, query)
            if filter_role is None:
                raise DeezeeError(f"No role matches `{query}`.")
        elif target_spec not in {"everyone", "all", "humans", "bots"}:
            raise DeezeeError(
                "Population must be `everyone`, `humans`, `bots`, or `in:<role>`."
            )

        # Build the target list up front so the confirmation can state a real
        # number rather than an estimate.
        targets: list[discord.Member] = []
        async for member in ctx.guild.fetch_members(limit=None):
            if target_spec == "humans" and member.bot:
                continue
            if target_spec == "bots" and not member.bot:
                continue
            if filter_role is not None and filter_role not in member.roles:
                continue
            has = role in member.roles
            if (choice == "add") == has:
                continue  # Already in the desired state.
            targets.append(member)
            if len(targets) >= MASSROLE_MAX:
                break

        if not targets:
            await self._respond(
                ctx,
                discord.Embed(
                    description="Nobody needs that change -- everyone matched is "
                    "already in the target state.",
                    colour=COLOUR_DEFAULT,
                ),
            )
            return

        estimate = len(targets) / MASSROLE_BATCH * BULK_ACTION_DELAY
        proceed = await confirm(
            ctx,
            title=f"{choice.title()} {role.name} for {len(targets)} member(s)?",
            description=(
                f"This will **{choice}** {role.mention} "
                f"{'to' if choice == 'add' else 'from'} **{len(targets)}** member(s) "
                f"matching `{population}`."
            ),
            confirm_label=f"{choice.title()} {len(targets)}",
            fields={
                "Estimated time": format_duration(int(estimate)) or "under a minute",
                "Rate": f"{MASSROLE_BATCH} per {BULK_ACTION_DELAY:.0f}s",
            },
        )
        if not proceed:
            return

        self._massrole_busy.add(ctx.guild.id)
        try:
            await self._run_massrole(ctx, targets, role, choice)
        finally:
            self._massrole_busy.discard(ctx.guild.id)

    async def _run_massrole(
        self,
        ctx: commands.Context,
        targets: list[discord.Member],
        role: discord.Role,
        choice: str,
    ) -> None:
        """Apply the change in batches, editing one progress embed as it goes."""
        reason = f"{ctx.author} via ?massrole"
        done = 0
        failed = 0

        embed = discord.Embed(
            title=f"{choice.title()}ing {role.name}",
            description=f"0 / {len(targets)}",
            colour=COLOUR_INFO,
        )
        try:
            progress = await ctx.send(embed=embed)
        except discord.HTTPException:
            progress = None

        for index, member in enumerate(targets, start=1):
            try:
                if choice == "add":
                    await member.add_roles(role, reason=reason)
                else:
                    await member.remove_roles(role, reason=reason)
                done += 1
            except discord.HTTPException:
                failed += 1

            if index % MASSROLE_BATCH == 0:
                await asyncio.sleep(BULK_ACTION_DELAY)
                if progress is not None:
                    embed.description = (
                        f"{index} / {len(targets)}  •  {done} done, {failed} failed"
                    )
                    try:
                        await progress.edit(embed=embed)
                    except discord.HTTPException:
                        progress = None

        embed.title = f"{choice.title()}ed {role.name}"
        embed.description = (
            f"**{done}** member(s) changed."
            + (f"\n**{failed}** failed -- most likely above my role." if failed else "")
        )
        embed.colour = COLOUR_SUCCESS if not failed else COLOUR_WARNING
        if progress is not None:
            try:
                await progress.edit(embed=embed)
                return
            except discord.HTTPException:
                pass
        await self._respond(ctx, embed)

    async def _resolve_role(
        self, ctx: commands.Context, query: str
    ) -> discord.Role | None:
        """Find a role by mention, ID or name."""
        try:
            return await commands.RoleConverter().convert(ctx, query)
        except commands.BadArgument:
            lowered = query.lower()
            for role in ctx.guild.roles:
                if role.name.lower() == lowered:
                    return role
            return None

    # =======================================================================
    # ?temprole
    # =======================================================================

    @commands.hybrid_command(name="temprole")
    @app_commands.describe(
        target="Member to give the role to",
        role="Role to give",
        duration="How long, e.g. 2h, 7d",
        reason="Why",
    )
    @permissions.mod_only()
    @permissions.guild_only()
    async def temprole(
        self,
        ctx: commands.Context,
        target: discord.Member,
        role: discord.Role,
        duration: str,
        *,
        reason: str = "No reason given",
    ) -> None:
        """Give a role that is removed automatically after a duration."""
        delta = parse_duration(duration)
        if delta is None:
            raise DeezeeError(f"`{duration}` is not a duration. Try `2h` or `7d`.")

        await permissions.enforce_hierarchy(
            ctx, target, action="temprole", bot_permission="manage_roles"
        )
        self._require_usable(ctx.guild, role)
        await self._check_invoker_outranks(ctx, role)

        expires = int(time.time() + delta.total_seconds())
        try:
            await target.add_roles(role, reason=f"{ctx.author}: {reason}")
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["manage_roles"]) from None

        # Cancel any existing timer for the same pair, so re-running the command
        # extends the role rather than leaving two removals queued.
        await self.bot.scheduler.cancel_matching(
            ctx.guild.id, "temprole_remove", user_id=target.id, role_id=role.id
        )
        await self.bot.scheduler.schedule(
            ctx.guild.id,
            "temprole_remove",
            expires,
            {"user_id": target.id, "role_id": role.id, "reason": reason},
        )

        await self._respond(
            ctx,
            self._ok(
                f"{target.mention} has {role.mention} for **{format_duration(delta)}** "
                f"(until <t:{expires}:{TS_LONG_DATE_TIME}>)."
            ),
        )

    async def _scheduled_temprole(self, guild_id: int, payload: dict[str, Any]) -> None:
        """Remove a temporary role when its timer comes due."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        role = guild.get_role(int(payload.get("role_id", 0)))
        if role is None:
            return  # Role deleted while the timer ran; nothing left to remove.
        member = await self.bot.get_or_fetch_member(guild, int(payload.get("user_id", 0)))
        if member is None or role not in member.roles:
            return
        try:
            await member.remove_roles(role, reason="Temporary role expired")
        except discord.HTTPException as exc:
            log.warning("Could not remove expired temprole in %s: %s", guild_id, exc)

    # =======================================================================
    # Role creation and editing
    # =======================================================================

    @commands.hybrid_command(name="createrole", with_app_command=False, aliases=["addrole", "newrole"])
    @app_commands.describe(
        name="Name of the new role",
        colour="Hex like #ff0000, a colour name, or random",
        hoist="Display the role separately in the member list",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def createrole(
        self,
        ctx: commands.Context,
        name: str,
        colour: Optional[str] = None,
        hoist: bool = False,
    ) -> None:
        """Create a role, optionally coloured and hoisted."""
        if not ctx.guild.me.guild_permissions.manage_roles:
            raise commands.BotMissingPermissions(["manage_roles"])
        if len(ctx.guild.roles) >= 250:
            raise DeezeeError("This server is at Discord's 250-role limit.")

        chosen = discord.Colour.default()
        if colour is not None:
            parsed = parse_colour(colour)
            if parsed is None:
                raise DeezeeError(
                    f"`{colour}` is not a colour. Use hex (`#ff0000`), a name "
                    f"({', '.join(sorted(NAMED_COLOURS)[:6])}...), or `random`."
                )
            chosen = parsed

        try:
            role = await ctx.guild.create_role(
                name=name[:100],
                colour=chosen,
                hoist=hoist,
                reason=f"{ctx.author} via ?createrole",
            )
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["manage_roles"]) from None

        await self._respond(
            ctx,
            self._ok(
                f"Created {role.mention} (`{role.id}`), colour `{chosen}`, "
                f"{'hoisted' if hoist else 'not hoisted'}.\n"
                "It sits at the bottom of the role list -- move it in Server "
                "Settings if it needs to be higher."
            ),
        )

    @commands.hybrid_command(name="deleterole", with_app_command=False, aliases=["delrole", "removerole"])
    @app_commands.describe(role="Role to delete")
    @permissions.admin_only()
    @permissions.guild_only()
    async def deleterole(self, ctx: commands.Context, role: discord.Role) -> None:
        """Delete a role. Asks for confirmation first."""
        self._require_usable(ctx.guild, role)
        await self._check_invoker_outranks(ctx, role)
        await self._defer(ctx)

        holders = (await self._count_members_by_role(ctx.guild)).get(role.id, 0)

        proceed = await confirm(
            ctx,
            title=f"Delete {role.name}?",
            description=(
                f"{role.mention} will be **deleted permanently**. "
                f"**{holders}** member(s) currently hold it and will lose it.\n"
                "Deleting a role cannot be undone -- the role ID is gone and any "
                "channel overwrites referencing it disappear with it."
            ),
            confirm_label="Delete the role",
            fields={"Holders": str(holders), "Position": str(role.position)},
        )
        if not proceed:
            return

        name = role.name
        try:
            await role.delete(reason=f"{ctx.author} via ?deleterole")
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["manage_roles"]) from None

        await self._respond(ctx, self._ok(f"Deleted the role **{name}**."))

    @commands.hybrid_command(name="rolecolor", with_app_command=False, aliases=["rolecolour"])
    @app_commands.describe(role="Role to recolour", colour="Hex, a colour name, or random")
    @permissions.admin_only()
    @permissions.guild_only()
    async def rolecolor(
        self, ctx: commands.Context, role: discord.Role, colour: str
    ) -> None:
        """Change a role's colour."""
        self._require_usable(ctx.guild, role)
        await self._check_invoker_outranks(ctx, role)

        parsed = parse_colour(colour)
        if parsed is None:
            raise DeezeeError(
                f"`{colour}` is not a colour. Use hex (`#5865f2`), a name, or `random`."
            )

        try:
            await role.edit(colour=parsed, reason=f"{ctx.author} via ?rolecolor")
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["manage_roles"]) from None

        embed = self._ok(f"{role.mention} is now `{parsed}`.")
        embed.colour = parsed.value or COLOUR_SUCCESS
        await self._respond(ctx, embed)

    @commands.hybrid_command(name="mentionable", with_app_command=False)
    @app_commands.describe(role="Role to change", state="on or off. Blank toggles")
    @permissions.admin_only()
    @permissions.guild_only()
    async def mentionable(
        self, ctx: commands.Context, role: discord.Role, state: Optional[str] = None
    ) -> None:
        """Toggle whether a role can be mentioned by anyone."""
        self._require_usable(ctx.guild, role)
        await self._check_invoker_outranks(ctx, role)

        if state is None:
            wanted = not role.mentionable
        elif state.lower() in {"on", "yes", "true", "enable"}:
            wanted = True
        elif state.lower() in {"off", "no", "false", "disable"}:
            wanted = False
        else:
            raise DeezeeError(f"`{state}` is not `on` or `off`.")

        if wanted == role.mentionable:
            await self._respond(
                ctx,
                discord.Embed(
                    description=f"{role.mention} is already "
                    f"{'mentionable' if wanted else 'not mentionable'}.",
                    colour=COLOUR_DEFAULT,
                ),
            )
            return

        try:
            await role.edit(mentionable=wanted, reason=f"{ctx.author} via ?mentionable")
        except discord.Forbidden:
            raise commands.BotMissingPermissions(["manage_roles"]) from None

        await self._respond(
            ctx,
            self._ok(
                f"{role.mention} is now "
                f"**{'mentionable by anyone' if wanted else 'not mentionable'}**."
            ),
        )

    # =======================================================================
    # Information
    # =======================================================================

    @commands.hybrid_command(name="roleinfo")
    @app_commands.describe(role="Role to inspect")
    @commands.cooldown(1, 15, commands.BucketType.guild)
    @permissions.guild_only()
    async def roleinfo(self, ctx: commands.Context, role: discord.Role) -> None:
        """Show a role's ID, colour, permissions, member count and position."""
        await self._defer(ctx)
        holders = (await self._count_members_by_role(ctx.guild)).get(role.id, 0)

        granted = [
            name.replace("_", " ").title()
            for name, value in role.permissions
            if value
        ]

        embed = discord.Embed(
            title=role.name,
            colour=role.colour.value or COLOUR_DEFAULT,
            description=f"{role.mention}\n`{role.id}`",
        )
        embed.add_field(name="Members", value=str(holders), inline=True)
        embed.add_field(
            name="Position", value=f"{role.position} of {len(ctx.guild.roles) - 1}",
            inline=True,
        )
        embed.add_field(name="Colour", value=str(role.colour), inline=True)
        embed.add_field(name="Hoisted", value="Yes" if role.hoist else "No", inline=True)
        embed.add_field(
            name="Mentionable", value="Yes" if role.mentionable else "No", inline=True
        )
        embed.add_field(
            name="Managed",
            value="Yes -- by Discord or an integration" if role.managed else "No",
            inline=True,
        )
        embed.add_field(
            name="Created",
            value=f"<t:{int(role.created_at.timestamp())}:{TS_LONG_DATE_TIME}>",
            inline=False,
        )
        embed.add_field(
            name=f"Permissions ({len(granted)})",
            value=", ".join(granted)[:1024] if granted else "None",
            inline=False,
        )

        usable, why = permissions.can_manage_role(ctx.guild, role)
        if not usable:
            embed.add_field(name="I cannot assign this role", value=why, inline=False)

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="roles", aliases=["rolelist"])
    @app_commands.describe(role="Optional: list the members holding this one role")
    @commands.cooldown(1, 20, commands.BucketType.guild)
    @permissions.guild_only()
    async def roles_cmd(
        self, ctx: commands.Context, role: Optional[discord.Role] = None
    ) -> None:
        """List every role with member counts, or the members holding one role."""
        await self._defer(ctx)

        if role is not None:
            holders = [
                f"{member} `{member.id}`"
                async for member in ctx.guild.fetch_members(limit=None)
                if role in member.roles
            ]
            pages = paginate_lines(
                sorted(holders),
                title=f"Members with {role.name}",
                per_page=15,
                colour=role.colour.value or COLOUR_DEFAULT,
                description=f"{len(holders)} member(s).",
                empty_message="Nobody has that role.",
            )
            await Paginator(pages, ctx.author).start(ctx)
            return

        counts = await self._count_members_by_role(ctx.guild)
        lines = [
            f"{r.mention} — **{counts.get(r.id, 0)}** member(s) `{r.id}`"
            for r in sorted(ctx.guild.roles, key=lambda x: x.position, reverse=True)
            if not r.is_default()
        ]
        pages = paginate_lines(
            lines,
            title=f"Roles in {ctx.guild.name}",
            per_page=12,
            description=f"{len(lines)} role(s), highest first. "
            "Counts are read from the live member list.",
            empty_message="This server has no roles besides @everyone.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    # =======================================================================
    # Reaction roles
    # =======================================================================

    async def load_menu(self, menu_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Return one menu and its options, both as plain dicts."""
        row = await self.bot.db.fetchone(
            "SELECT * FROM reaction_role_menus WHERE id = ?", (menu_id,)
        )
        if row is None:
            raise DeezeeError(f"Menu `{menu_id}` no longer exists.")
        options = await self.bot.db.fetchall(
            "SELECT * FROM reaction_role_options WHERE menu_id = ? "
            "ORDER BY position, id",
            (menu_id,),
        )
        return dict(row), [dict(option) for option in options]

    async def update_menu(self, menu_id: int, **fields: Any) -> None:
        """Write menu settings. Column names come from this module, never a user."""
        allowed = {"title", "description", "colour", "style", "mode", "max_roles",
                   "channel_id", "message_id"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        await self.bot.db.execute(
            f"UPDATE reaction_role_menus SET {assignments} WHERE id = ?",
            (*updates.values(), menu_id),
        )

    async def set_menu_roles(self, menu_id: int, roles: list[discord.Role]) -> None:
        """Replace a menu's role list in one transaction."""
        async with self.bot.db.transaction() as conn:
            await conn.execute(
                "DELETE FROM reaction_role_options WHERE menu_id = ?", (menu_id,)
            )
            for position, role in enumerate(roles):
                await conn.execute(
                    "INSERT INTO reaction_role_options "
                    "(menu_id, role_id, label, position) VALUES (?, ?, ?, ?)",
                    (menu_id, role.id, role.name[:80], position),
                )

    def _build_menu_view(
        self, guild: discord.Guild, menu: dict[str, Any], options: list[dict[str, Any]]
    ) -> discord.ui.View | None:
        """Build the components for a published menu, or ``None`` for reactions."""
        if menu["style"] == "reaction":
            return None

        view = discord.ui.View(timeout=None)

        if menu["style"] == "select":
            select_options: list[discord.SelectOption] = []
            for option in options[:MAX_SELECT_OPTIONS]:
                role = guild.get_role(option["role_id"])
                if role is None:
                    continue
                select_options.append(
                    discord.SelectOption(
                        label=(option["label"] or role.name)[:100],
                        value=str(option["id"]),
                        description=(option["description"] or "")[:100] or None,
                        emoji=option["emoji"] or None,
                    )
                )
            if not select_options:
                return None
            # ``unique`` means one at a time, so the dropdown enforces it directly
            # rather than letting someone pick five and then rejecting four.
            max_values = 1 if menu["mode"] == "unique" else len(select_options)
            view.add_item(
                MenuRoleSelect(
                    menu["id"],
                    placeholder=menu["title"][:100] or "Pick your roles",
                    options=select_options,
                    max_values=max_values,
                )
            )
            return view

        for index, option in enumerate(options[:MAX_MENU_BUTTONS]):
            role = guild.get_role(option["role_id"])
            if role is None:
                continue
            view.add_item(
                MenuRoleButton(
                    menu["id"],
                    option["id"],
                    label=option["label"] or role.name,
                    emoji=option["emoji"],
                    row=index // 5,
                )
            )
        return view if view.children else None

    def _menu_embed(
        self, guild: discord.Guild, menu: dict[str, Any], options: list[dict[str, Any]]
    ) -> discord.Embed:
        """The public embed for a published menu."""
        embed = discord.Embed(
            title=menu["title"],
            description=menu["description"] or None,
            colour=menu["colour"] or COLOUR_DEFAULT,
        )
        if menu["style"] == "reaction":
            lines = []
            for option in options:
                role = guild.get_role(option["role_id"])
                if role is None:
                    continue
                lines.append(f"{option['emoji'] or '•'} — {role.mention}")
            if lines:
                embed.add_field(name="React to choose", value="\n".join(lines),
                                inline=False)

        note = {
            "unique": "You may hold one role from this menu at a time.",
            "verify": "Roles here can be taken but not given back.",
            "drop": "This menu only removes roles.",
            "limit": f"You may hold up to {menu['max_roles']} roles from this menu.",
        }.get(menu["mode"])
        if note:
            embed.set_footer(text=note)
        return embed

    async def publish_menu(self, guild: discord.Guild, menu_id: int) -> discord.Message:
        """Post or repost a menu into its channel."""
        menu, options = await self.load_menu(menu_id)
        if not options:
            raise DeezeeError("Add at least one role before publishing.")

        channel = guild.get_channel(menu["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            raise DeezeeError("That menu's channel no longer exists.")

        perms = channel.permissions_for(guild.me)
        if not perms.send_messages or not perms.embed_links:
            raise DeezeeError(
                f"I need Send Messages and Embed Links in {channel.mention}."
            )
        if menu["style"] == "reaction" and not perms.add_reactions:
            raise DeezeeError(f"I need Add Reactions in {channel.mention}.")

        embed = self._menu_embed(guild, menu, options)
        view = self._build_menu_view(guild, menu, options)

        # A republish edits the existing message where possible: members who
        # already picked keep their roles and the message keeps its link.
        message: discord.Message | None = None
        if menu["message_id"]:
            try:
                message = await channel.fetch_message(menu["message_id"])
                await message.edit(embed=embed, view=view)
            except (discord.NotFound, discord.Forbidden):
                message = None

        if message is None:
            message = await channel.send(embed=embed, view=view)
            await self.update_menu(menu_id, message_id=message.id)

        if menu["style"] == "reaction":
            for option in options:
                if not option["emoji"]:
                    continue
                try:
                    await message.add_reaction(option["emoji"])
                except discord.HTTPException:
                    log.debug("Could not add reaction %s", option["emoji"])

        return message

    @commands.hybrid_group(
        name="reactionrole", aliases=["rr", "rolemenu"], fallback="list"
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def reactionrole(self, ctx: commands.Context) -> None:
        """Build and manage self-assign role menus."""
        rows = await self.bot.db.fetchall(
            "SELECT m.*, (SELECT COUNT(*) FROM reaction_role_options o "
            "WHERE o.menu_id = m.id) AS roles FROM reaction_role_menus m "
            "WHERE m.guild_id = ? ORDER BY m.id",
            (ctx.guild.id,),
        )
        lines = []
        for row in rows:
            channel = ctx.guild.get_channel(row["channel_id"])
            where = channel.mention if channel else "*deleted channel*"
            state = "published" if row["message_id"] else "**draft**"
            lines.append(
                f"**#{row['id']}** {row['title']}\n"
                f"{where} • {row['style']}/{row['mode']} • "
                f"{row['roles']} role(s) • {state}"
            )
        pages = paginate_lines(
            lines,
            title="Role menus",
            per_page=6,
            description="`?rr edit <id>` to change one, `?rr create` to make one.",
            empty_message="No role menus yet. Make one with `?rr create`.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @reactionrole.command(name="create")
    @app_commands.describe(channel="Where the menu will be posted", title="Menu heading")
    @permissions.admin_only()
    @permissions.guild_only()
    async def rr_create(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        *,
        title: str = "Self-assignable roles",
    ) -> None:
        """Start a new role menu and open the builder."""
        target = channel or ctx.channel
        cursor = await self.bot.db.execute(
            "INSERT INTO reaction_role_menus "
            "(guild_id, channel_id, title, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (ctx.guild.id, target.id, title[:256], ctx.author.id, int(time.time())),
        )
        menu, options = await self.load_menu(int(cursor.lastrowid or 0))
        await open_builder(ctx, self, menu, options)

    @reactionrole.command(name="edit")
    @app_commands.describe(menu_id="Menu number from ?rr list")
    @permissions.admin_only()
    @permissions.guild_only()
    async def rr_edit(self, ctx: commands.Context, menu_id: int) -> None:
        """Reopen the builder for an existing menu."""
        menu, options = await self.load_menu(menu_id)
        if menu["guild_id"] != ctx.guild.id:
            raise DeezeeError(f"Menu `{menu_id}` does not belong to this server.")
        await open_builder(ctx, self, menu, options)

    @reactionrole.command(name="add")
    @app_commands.describe(
        menu_id="Menu number", role="Role to add", emoji="Optional emoji for the entry"
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def rr_add(
        self,
        ctx: commands.Context,
        menu_id: int,
        role: discord.Role,
        emoji: Optional[str] = None,
    ) -> None:
        """Add one role to a menu."""
        menu, options = await self.load_menu(menu_id)
        if menu["guild_id"] != ctx.guild.id:
            raise DeezeeError(f"Menu `{menu_id}` does not belong to this server.")
        self._require_usable(ctx.guild, role)
        await self._check_invoker_outranks(ctx, role)
        if menu["style"] == "reaction" and not emoji:
            raise DeezeeError("A reaction menu needs an emoji for each role.")
        if any(option["role_id"] == role.id for option in options):
            raise DeezeeError(f"{role.mention} is already in menu `{menu_id}`.")

        await self.bot.db.execute(
            "INSERT INTO reaction_role_options (menu_id, role_id, emoji, label, position) "
            "VALUES (?, ?, ?, ?, ?)",
            (menu_id, role.id, emoji, role.name[:80], len(options)),
        )
        await self._respond(
            ctx,
            self._ok(
                f"Added {role.mention} to menu `{menu_id}`. "
                "Run `?rr edit` and press Publish to update the posted message."
            ),
        )

    @reactionrole.command(name="remove")
    @app_commands.describe(menu_id="Menu number", role="Role to remove from the menu")
    @permissions.admin_only()
    @permissions.guild_only()
    async def rr_remove(
        self, ctx: commands.Context, menu_id: int, role: discord.Role
    ) -> None:
        """Remove one role from a menu."""
        menu, _ = await self.load_menu(menu_id)
        if menu["guild_id"] != ctx.guild.id:
            raise DeezeeError(f"Menu `{menu_id}` does not belong to this server.")

        cursor = await self.bot.db.execute(
            "DELETE FROM reaction_role_options WHERE menu_id = ? AND role_id = ?",
            (menu_id, role.id),
        )
        if not cursor.rowcount:
            raise DeezeeError(f"{role.mention} is not in menu `{menu_id}`.")
        await self._respond(
            ctx, self._ok(f"Removed {role.mention} from menu `{menu_id}`.")
        )

    @reactionrole.command(name="mode")
    @app_commands.describe(
        menu_id="Menu number",
        mode="normal, unique, verify, drop or limit",
        maximum="Only for limit mode: how many roles",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def rr_mode(
        self, ctx: commands.Context, menu_id: int, mode: str, maximum: int = 0
    ) -> None:
        """Set a menu's behaviour: normal, unique, verify, drop or limit."""
        menu, _ = await self.load_menu(menu_id)
        if menu["guild_id"] != ctx.guild.id:
            raise DeezeeError(f"Menu `{menu_id}` does not belong to this server.")

        chosen = mode.lower()
        if chosen not in MODES:
            raise DeezeeError(
                "Mode must be one of: " + ", ".join(f"`{m}`" for m in MODES)
            )
        if chosen == "limit" and maximum < 1:
            raise DeezeeError("`limit` mode needs a number, e.g. `?rr mode 3 limit 2`.")

        await self.update_menu(menu_id, mode=chosen, max_roles=max(0, maximum))
        await self._respond(
            ctx,
            self._ok(
                f"Menu `{menu_id}` is now **{chosen}** — {MODES[chosen]}."
                + (f" Limit: **{maximum}**." if chosen == "limit" else "")
                + "\nRepublish the menu if its style is `select`, so the dropdown "
                "matches."
            ),
        )

    @reactionrole.command(name="delete")
    @app_commands.describe(menu_id="Menu number to delete")
    @permissions.admin_only()
    @permissions.guild_only()
    async def rr_delete(self, ctx: commands.Context, menu_id: int) -> None:
        """Delete a menu and, if it was published, its message."""
        menu, options = await self.load_menu(menu_id)
        if menu["guild_id"] != ctx.guild.id:
            raise DeezeeError(f"Menu `{menu_id}` does not belong to this server.")

        proceed = await confirm(
            ctx,
            title=f"Delete menu #{menu_id}?",
            description=(
                f"**{menu['title']}** and its {len(options)} role entr(ies) will be "
                "deleted, and the posted message removed if it still exists.\n"
                "Roles members already picked up are **not** taken away."
            ),
            confirm_label="Delete the menu",
        )
        if not proceed:
            return

        if menu["message_id"]:
            channel = ctx.guild.get_channel(menu["channel_id"])
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(menu["message_id"])
                    await message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        await self.bot.db.execute(
            "DELETE FROM reaction_role_menus WHERE id = ?", (menu_id,)
        )
        await self._respond(ctx, self._ok(f"Deleted menu `{menu_id}`."))

    # --- Menu interaction --------------------------------------------------

    async def handle_menu_choice(
        self,
        interaction: discord.Interaction,
        menu_id: int,
        option_ids: list[int],
        *,
        exclusive: bool = False,
    ) -> None:
        """Apply a member's choice from a published menu.

        Args:
            interaction: The button press or select submission.
            menu_id: Which menu.
            option_ids: The options they chose.
            exclusive: True for a dropdown, where the chosen set is the whole
                answer and anything unpicked should come off. False for a
                button, which only ever speaks about itself.
        """
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return

        try:
            menu, options = await self.load_menu(menu_id)
        except DeezeeError:
            await interaction.response.send_message(
                "That menu has been deleted.", ephemeral=True
            )
            return

        menu_roles: dict[int, discord.Role] = {}
        for option in options:
            role = guild.get_role(option["role_id"])
            if role is not None:
                menu_roles[option["id"]] = role

        chosen = [menu_roles[oid] for oid in option_ids if oid in menu_roles]
        held_from_menu = [r for r in menu_roles.values() if r in member.roles]

        to_add: list[discord.Role] = []
        to_remove: list[discord.Role] = []
        mode = menu["mode"]

        if exclusive:
            wanted = {role.id for role in chosen}
            to_add = [r for r in chosen if r not in member.roles]
            to_remove = [r for r in held_from_menu if r.id not in wanted]
        else:
            for role in chosen:
                if role in member.roles:
                    to_remove.append(role)
                else:
                    to_add.append(role)

        if mode == "verify":
            to_remove = []
        elif mode == "drop":
            to_add = []
        elif mode == "unique" and to_add:
            keep = to_add[-1:]
            to_add = keep
            to_remove = [r for r in held_from_menu if r not in keep]
        elif mode == "limit" and menu["max_roles"]:
            after = (set(held_from_menu) | set(to_add)) - set(to_remove)
            if len(after) > menu["max_roles"]:
                await interaction.response.send_message(
                    f"You may hold at most **{menu['max_roles']}** role(s) from this "
                    "menu. Remove one first.",
                    ephemeral=True,
                )
                return

        refused: list[str] = []
        for role in list(to_add) + list(to_remove):
            usable, why = permissions.can_manage_role(guild, role)
            if not usable:
                refused.append(f"{role.name}: {why}")
                if role in to_add:
                    to_add.remove(role)
                if role in to_remove:
                    to_remove.remove(role)

        if not to_add and not to_remove:
            message = "Nothing changed."
            if refused:
                message = "I could not change any of those roles:\n" + "\n".join(
                    f"• {reason}" for reason in refused[:3]
                )
            await interaction.response.send_message(message, ephemeral=True)
            return

        reason = f"Role menu #{menu_id}"
        try:
            if to_add:
                await member.add_roles(*to_add, reason=reason)
            if to_remove:
                await member.remove_roles(*to_remove, reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I do not have permission to change your roles. "
                "My role may be below them.",
                ephemeral=True,
            )
            return

        parts = []
        if to_add:
            parts.append("**Added:** " + ", ".join(r.mention for r in to_add))
        if to_remove:
            parts.append("**Removed:** " + ", ".join(r.mention for r in to_remove))
        if refused:
            parts.append("**Skipped:** " + "; ".join(refused[:2]))

        await interaction.response.send_message(
            embed=discord.Embed(
                description="\n".join(parts), colour=COLOUR_SUCCESS
            ),
            ephemeral=True,
        )

    @commands.Cog.listener("on_raw_reaction_add")
    async def reaction_role_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Grant a role when someone reacts to a reaction-style menu."""
        await self._handle_reaction(payload, adding=True)

    @commands.Cog.listener("on_raw_reaction_remove")
    async def reaction_role_remove(self, payload: discord.RawReactionActionEvent) -> None:
        """Take the role back when the reaction is removed."""
        await self._handle_reaction(payload, adding=False)

    async def _handle_reaction(
        self, payload: discord.RawReactionActionEvent, *, adding: bool
    ) -> None:
        """Shared body for both reaction events.

        Raw events are used rather than the cached ones because a menu posted
        weeks ago is not in the message cache, and the cached event would simply
        never fire for it.
        """
        if payload.guild_id is None or payload.user_id == self.bot.user.id:
            return

        row = await self.bot.db.fetchone(
            "SELECT * FROM reaction_role_menus WHERE message_id = ? AND style = 'reaction'",
            (payload.message_id,),
        )
        if row is None:
            return

        emoji = str(payload.emoji)
        option = await self.bot.db.fetchone(
            "SELECT * FROM reaction_role_options WHERE menu_id = ? AND emoji = ?",
            (row["id"], emoji),
        )
        if option is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        role = guild.get_role(option["role_id"])
        if role is None:
            return

        member = payload.member or await self.bot.get_or_fetch_member(
            guild, payload.user_id
        )
        if member is None or member.bot:
            return

        mode = row["mode"]
        if adding and mode == "drop":
            adding = False
        elif not adding and mode == "verify":
            return

        usable, _ = permissions.can_manage_role(guild, role)
        if not usable:
            return

        try:
            if adding:
                if role not in member.roles:
                    await member.add_roles(role, reason=f"Role menu #{row['id']}")
                if mode == "unique":
                    others = await self.bot.db.fetchall(
                        "SELECT role_id FROM reaction_role_options "
                        "WHERE menu_id = ? AND role_id != ?",
                        (row["id"], role.id),
                    )
                    drop = [
                        guild.get_role(other["role_id"])
                        for other in others
                        if guild.get_role(other["role_id"]) in member.roles
                    ]
                    if drop:
                        await member.remove_roles(*drop, reason="Unique role menu")
            elif role in member.roles:
                await member.remove_roles(role, reason=f"Role menu #{row['id']}")
        except discord.HTTPException as exc:
            log.debug("Reaction role change failed: %s", exc)

    # =======================================================================
    # Autoroles
    # =======================================================================

    @commands.hybrid_group(name="autorole", with_app_command=False, fallback="list")
    @permissions.admin_only()
    @permissions.guild_only()
    async def autorole(self, ctx: commands.Context) -> None:
        """Roles granted automatically when someone joins."""
        rows = await self.bot.db.fetchall(
            "SELECT * FROM autoroles WHERE guild_id = ?", (ctx.guild.id,)
        )
        lines = []
        for row in rows:
            role = ctx.guild.get_role(row["role_id"])
            name = role.mention if role else f"*deleted role {row['role_id']}*"
            who = "bots" if row["for_bots"] else "humans"
            delay = (
                f" after {format_duration(row['delay_seconds'])}"
                if row["delay_seconds"]
                else " immediately"
            )
            lines.append(f"{name} → {who}{delay}")

        pages = paginate_lines(
            lines,
            title="Autoroles",
            per_page=12,
            description="`?autorole add <role> [--bots] [--delay 60s]`",
            empty_message="No autoroles configured.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @autorole.command(name="add")
    @app_commands.describe(
        role="Role to grant on join",
        bots="Grant to bots instead of humans",
        delay="Wait this long before granting, e.g. 60s",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def autorole_add(
        self,
        ctx: commands.Context,
        role: discord.Role,
        bots: bool = False,
        delay: Optional[str] = None,
    ) -> None:
        """Grant a role automatically on join, optionally after a delay."""
        self._require_usable(ctx.guild, role)
        await self._check_invoker_outranks(ctx, role)

        seconds = 0
        if delay:
            parsed = parse_duration(delay)
            if parsed is None:
                raise DeezeeError(f"`{delay}` is not a duration. Try `60s` or `5m`.")
            seconds = int(parsed.total_seconds())
            if seconds > 86400:
                raise DeezeeError("A join delay above 24 hours is not useful. Use 24h or less.")

        await self.bot.db.execute(
            "INSERT INTO autoroles (guild_id, role_id, for_bots, delay_seconds, "
            "added_by, created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, role_id) DO UPDATE SET "
            "for_bots = excluded.for_bots, delay_seconds = excluded.delay_seconds",
            (ctx.guild.id, role.id, int(bots), seconds, ctx.author.id, int(time.time())),
        )

        note = (
            f" after **{format_duration(seconds)}** -- which blunts join-and-spam raids"
            if seconds
            else " immediately on join"
        )
        await self._respond(
            ctx,
            self._ok(
                f"{role.mention} will be granted to "
                f"**{'bots' if bots else 'humans'}**{note}."
            ),
        )

    @autorole.command(name="remove")
    @app_commands.describe(role="Role to stop granting")
    @permissions.admin_only()
    @permissions.guild_only()
    async def autorole_remove(self, ctx: commands.Context, role: discord.Role) -> None:
        """Stop granting a role on join."""
        cursor = await self.bot.db.execute(
            "DELETE FROM autoroles WHERE guild_id = ? AND role_id = ?",
            (ctx.guild.id, role.id),
        )
        if not cursor.rowcount:
            raise DeezeeError(f"{role.mention} is not an autorole.")
        await self._respond(ctx, self._ok(f"{role.mention} is no longer granted on join."))

    @commands.Cog.listener("on_member_join")
    async def grant_autoroles(self, member: discord.Member) -> None:
        """Apply autoroles, immediately or on a timer."""
        rows = await self.bot.db.fetchall(
            "SELECT role_id, delay_seconds FROM autoroles "
            "WHERE guild_id = ? AND for_bots = ?",
            (member.guild.id, int(member.bot)),
        )
        if not rows:
            return

        immediate: list[discord.Role] = []
        for row in rows:
            role = member.guild.get_role(row["role_id"])
            if role is None:
                continue
            usable, _ = permissions.can_manage_role(member.guild, role)
            if not usable:
                continue
            if row["delay_seconds"]:
                await self.bot.scheduler.schedule(
                    member.guild.id,
                    "autorole_grant",
                    int(time.time()) + row["delay_seconds"],
                    {"user_id": member.id, "role_id": role.id},
                )
            else:
                immediate.append(role)

        if immediate:
            try:
                await member.add_roles(*immediate, reason="Autorole")
            except discord.HTTPException as exc:
                log.warning("Autorole grant failed in %s: %s", member.guild.id, exc)

    async def _scheduled_autorole(self, guild_id: int, payload: dict[str, Any]) -> None:
        """Grant a delayed autorole once its wait is over."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        role = guild.get_role(int(payload.get("role_id", 0)))
        if role is None:
            return
        member = await self.bot.get_or_fetch_member(guild, int(payload.get("user_id", 0)))
        if member is None or role in member.roles:
            return
        # Re-check that the autorole still exists: it may have been removed
        # during the delay, and granting it then would be wrong.
        still_set = await self.bot.db.fetchval(
            "SELECT 1 FROM autoroles WHERE guild_id = ? AND role_id = ?",
            (guild_id, role.id),
        )
        if not still_set:
            return
        try:
            await member.add_roles(role, reason="Delayed autorole")
        except discord.HTTPException as exc:
            log.warning("Delayed autorole failed in %s: %s", guild_id, exc)

    # =======================================================================
    # Sticky roles
    # =======================================================================

    @commands.hybrid_command(name="stickyroles", with_app_command=False)
    @app_commands.describe(
        action="on, off, blacklist, unblacklist or list",
        role="Role, for blacklist and unblacklist",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def stickyroles(
        self, ctx: commands.Context, action: str = "list",
        role: Optional[discord.Role] = None,
    ) -> None:
        """Restore a member's previous roles when they rejoin."""
        choice = action.lower()
        config = await self.bot.guild_config.get(ctx.guild.id)

        if choice in {"on", "enable"}:
            await self.bot.guild_config.set(ctx.guild.id, "sticky_roles_enabled", 1)
            await self._respond(
                ctx,
                self._ok(
                    "Sticky roles are **on**. Members who rejoin get their roles back.\n"
                    "Blacklist any role that must not come back -- the mute role above "
                    "all -- with `?stickyroles blacklist <role>`."
                ),
            )
            return

        if choice in {"off", "disable"}:
            await self.bot.guild_config.set(ctx.guild.id, "sticky_roles_enabled", 0)
            await self._respond(ctx, self._ok("Sticky roles are **off**."))
            return

        if choice in {"blacklist", "block"}:
            if role is None:
                raise DeezeeError("Name the role to blacklist.")
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO sticky_role_blacklist (guild_id, role_id) "
                "VALUES (?, ?)",
                (ctx.guild.id, role.id),
            )
            await self._respond(
                ctx, self._ok(f"{role.mention} will never be restored on rejoin.")
            )
            return

        if choice in {"unblacklist", "unblock", "allow"}:
            if role is None:
                raise DeezeeError("Name the role to un-blacklist.")
            await self.bot.db.execute(
                "DELETE FROM sticky_role_blacklist WHERE guild_id = ? AND role_id = ?",
                (ctx.guild.id, role.id),
            )
            await self._respond(ctx, self._ok(f"{role.mention} can be restored again."))
            return

        if choice != "list":
            raise DeezeeError(
                "Use `on`, `off`, `blacklist <role>`, `unblacklist <role>` or `list`."
            )

        rows = await self.bot.db.fetchall(
            "SELECT role_id FROM sticky_role_blacklist WHERE guild_id = ?",
            (ctx.guild.id,),
        )
        stored = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM sticky_roles WHERE guild_id = ?", (ctx.guild.id,)
        )
        blocked = [
            (ctx.guild.get_role(row["role_id"]) or None) for row in rows
        ]
        embed = discord.Embed(
            title="Sticky roles",
            colour=COLOUR_SUCCESS if config["sticky_roles_enabled"] else COLOUR_DEFAULT,
        )
        embed.add_field(
            name="Status",
            value=f"{EMOJI_ON} On" if config["sticky_roles_enabled"] else f"{EMOJI_OFF} Off",
            inline=True,
        )
        embed.add_field(name="Members remembered", value=str(stored or 0), inline=True)
        embed.add_field(
            name="Never restored",
            value="\n".join(r.mention for r in blocked if r) or "Nothing blacklisted",
            inline=False,
        )
        await ctx.send(embed=embed)

    @commands.Cog.listener("on_member_remove")
    async def remember_roles(self, member: discord.Member) -> None:
        """Write down what a leaving member held, if sticky roles are on."""
        if not await self.bot.guild_config.flag(member.guild.id, "sticky_roles_enabled"):
            return
        keep = [
            role.id
            for role in member.roles
            if not role.is_default() and not role.managed
        ]
        if not keep:
            return
        await self.bot.db.execute(
            "INSERT INTO sticky_roles (guild_id, user_id, roles, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "roles = excluded.roles, updated_at = excluded.updated_at",
            (member.guild.id, member.id, json.dumps(keep), int(time.time())),
        )

    @commands.Cog.listener("on_member_join")
    async def restore_roles(self, member: discord.Member) -> None:
        """Give a returning member their roles back."""
        if not await self.bot.guild_config.flag(member.guild.id, "sticky_roles_enabled"):
            return

        row = await self.bot.db.fetchone(
            "SELECT roles FROM sticky_roles WHERE guild_id = ? AND user_id = ?",
            (member.guild.id, member.id),
        )
        if row is None:
            return

        try:
            stored = json.loads(row["roles"])
        except (ValueError, TypeError):
            stored = []

        blacklist = {
            r["role_id"]
            for r in await self.bot.db.fetchall(
                "SELECT role_id FROM sticky_role_blacklist WHERE guild_id = ?",
                (member.guild.id,),
            )
        }

        restore: list[discord.Role] = []
        for role_id in stored:
            if role_id in blacklist:
                continue
            role = member.guild.get_role(role_id)
            if role is None:
                continue
            usable, _ = permissions.can_manage_role(member.guild, role)
            if usable:
                restore.append(role)

        if restore:
            try:
                await member.add_roles(*restore, reason="Sticky roles")
            except discord.HTTPException as exc:
                log.warning("Sticky role restore failed: %s", exc)

        await self.bot.db.execute(
            "DELETE FROM sticky_roles WHERE guild_id = ? AND user_id = ?",
            (member.guild.id, member.id),
        )

    # =======================================================================
    # Self-roles
    # =======================================================================

    async def apply_self_roles(
        self, member: discord.Member, available: set[int], wanted: set[int]
    ) -> tuple[list[discord.Role], list[discord.Role], list[str]]:
        """Bring a member's self-roles in line with what they picked.

        Returns:
            ``(added, removed, refused)``.
        """
        added: list[discord.Role] = []
        removed: list[discord.Role] = []
        refused: list[str] = []

        for role_id in available:
            role = member.guild.get_role(role_id)
            if role is None:
                continue
            has = role in member.roles
            want = role_id in wanted
            if has == want:
                continue
            usable, why = permissions.can_manage_role(member.guild, role)
            if not usable:
                refused.append(f"{role.name}: {why}")
                continue
            (added if want else removed).append(role)

        try:
            if added:
                await member.add_roles(*added, reason="Self-role")
            if removed:
                await member.remove_roles(*removed, reason="Self-role")
        except discord.Forbidden:
            return [], [], refused + ["I lack the Manage Roles permission."]

        return added, removed, refused

    @commands.hybrid_command(name="selfrole", aliases=["iam"])
    @app_commands.describe(role="Role to join or leave. Blank opens the picker")
    @commands.cooldown(3, 10, commands.BucketType.member)
    @permissions.guild_only()
    async def selfrole(
        self, ctx: commands.Context, *, role: Optional[discord.Role] = None
    ) -> None:
        """Join or leave a role marked self-assignable.

        With no argument this opens a picker showing everything available.
        """
        rows = await self.bot.db.fetchall(
            "SELECT * FROM self_roles WHERE guild_id = ?", (ctx.guild.id,)
        )
        if not rows:
            raise NotConfigured("self-assignable roles", "Roles")

        if role is None:
            await open_self_roles(ctx, self, [dict(row) for row in rows])
            return

        if not any(row["role_id"] == role.id for row in rows):
            raise DeezeeError(
                f"{role.mention} is not self-assignable. "
                "Run `?selfrole` with no argument to see what is."
            )

        added, removed, refused = await self.apply_self_roles(
            ctx.author, {role.id}, set() if role in ctx.author.roles else {role.id}
        )
        if refused:
            raise DeezeeError(refused[0])

        if added:
            await ctx.send(embed=self._ok(f"You now have {role.mention}."))
        elif removed:
            await ctx.send(embed=self._ok(f"Removed {role.mention}."))
        else:
            await ctx.send(
                embed=discord.Embed(
                    description="Nothing changed.", colour=COLOUR_DEFAULT
                )
            )

    @commands.hybrid_group(name="selfroles", with_app_command=False, aliases=["ranks"], fallback="list")
    @permissions.guild_only()
    async def selfroles(self, ctx: commands.Context) -> None:
        """List which roles members may assign themselves."""
        rows = await self.bot.db.fetchall(
            "SELECT * FROM self_roles WHERE guild_id = ? ORDER BY created_at",
            (ctx.guild.id,),
        )
        lines = []
        for row in rows:
            role = ctx.guild.get_role(row["role_id"])
            if role is None:
                lines.append(f"*deleted role {row['role_id']}* — remove with `?selfroles remove`")
                continue
            note = f" — {row['description']}" if row["description"] else ""
            lines.append(f"{role.mention}{note}")

        pages = paginate_lines(
            lines,
            title="Self-assignable roles",
            per_page=15,
            description="Take one with `?selfrole <name>`, or `?selfrole` for a picker.",
            empty_message="No self-assignable roles yet.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @selfroles.command(name="add")
    @app_commands.describe(role="Role members may take", description="Shown in the picker")
    @permissions.admin_only()
    @permissions.guild_only()
    async def selfroles_add(
        self, ctx: commands.Context, role: discord.Role, *, description: str = ""
    ) -> None:
        """Mark a role as self-assignable."""
        self._require_usable(ctx.guild, role)
        await self._check_invoker_outranks(ctx, role)

        dangerous = [
            name.replace("_", " ")
            for name in ("administrator", "manage_guild", "manage_roles", "ban_members",
                         "kick_members", "manage_channels", "mention_everyone")
            if getattr(role.permissions, name)
        ]
        if dangerous:
            proceed = await confirm(
                ctx,
                title=f"{role.name} carries staff permissions",
                description=(
                    f"{role.mention} grants: **{', '.join(dangerous)}**.\n"
                    "Making it self-assignable means **any member can give it to "
                    "themselves at any time**. That is almost never what you want."
                ),
                confirm_label="I understand, make it self-assignable",
            )
            if not proceed:
                return

        await self.bot.db.execute(
            "INSERT INTO self_roles (guild_id, role_id, description, added_by, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id, role_id) DO UPDATE SET "
            "description = excluded.description",
            (ctx.guild.id, role.id, description[:100], ctx.author.id, int(time.time())),
        )
        await self._respond(
            ctx, self._ok(f"Members may now take {role.mention} with `?selfrole`.")
        )

    @selfroles.command(name="remove")
    @app_commands.describe(role="Role members may no longer take")
    @permissions.admin_only()
    @permissions.guild_only()
    async def selfroles_remove(self, ctx: commands.Context, role: discord.Role) -> None:
        """Stop a role being self-assignable."""
        cursor = await self.bot.db.execute(
            "DELETE FROM self_roles WHERE guild_id = ? AND role_id = ?",
            (ctx.guild.id, role.id),
        )
        if not cursor.rowcount:
            raise DeezeeError(f"{role.mention} was not self-assignable.")
        await self._respond(
            ctx,
            self._ok(
                f"{role.mention} is no longer self-assignable. "
                "Members who already have it keep it."
            ),
        )


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point."""
    await bot.add_cog(Roles(bot))
