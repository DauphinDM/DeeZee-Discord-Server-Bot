"""The configuration surface: ``?config`` and everything it reaches.

Covers 15 of the 24 commands in the command sheet's "Server Config" section;
the nine ``?import`` rows are in ``cogs/importer.py``.

This is the file that makes "no web dashboard" true rather than aspirational.
``?config`` opens a panel with one button per category, each of which opens the
panel that category's own cog owns. Nothing here re-implements another cog's
settings screen.

The gating check registered here runs before every command in the bot:
``?module``, ``?command`` and ``?permissions`` are enforced in one place rather
than by each cog remembering to ask.
"""

from __future__ import annotations

import io
import json
import logging
import time
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import permissions
from core.constants import (
    COLOUR_DEFAULT,
    COLOUR_INFO,
    COLOUR_SUCCESS,
    COLOUR_WARNING,
    EMOJI_OFF,
    EMOJI_ON,
    EMOJI_SUCCESS,
)
from core.errors import DeezeeError
from ui.paginator import Paginator, paginate_lines
from ui.panels.root import open_root
from ui.panels.welcome import open_greeting
from ui.views import confirm

log = logging.getLogger(__name__)

#: Commands that can never be disabled or overridden.
#:
#: Every one of them is a way back out. Disabling ``?command`` with ``?command``
#: would leave the server with no route to undo it short of editing SQLite by
#: hand, which on a panel host means downloading the database.
PROTECTED_COMMANDS = frozenset({
    "config", "command", "module", "permissions", "prefix", "help", "diagnose",
})

#: Modules that can never be disabled, for the same reason.
PROTECTED_MODULES = frozenset({"ServerConfig", "Utility"})

#: Config keys never included in an export. There are none holding secrets today
#: -- tokens live in ``.env`` and never touch the database -- but an export is a
#: file people paste into public channels, so the list exists to be added to.
EXPORT_EXCLUDE = frozenset({"guild_id", "created_at", "updated_at", "case_counter"})

#: Settings groups understood by ``?configreset``.
RESET_SECTIONS: dict[str, tuple[str, ...]] = {
    "moderation": ("mute_role_id", "dm_on_punish", "delete_invocation"),
    "logging": (
        "mod_log_channel_id", "message_log_channel_id", "member_log_channel_id",
        "server_log_channel_id", "voice_log_channel_id",
        "log_messages_channel_id", "log_members_channel_id", "log_roles_channel_id",
        "log_channels_channel_id", "log_voice_channel_id", "log_server_channel_id",
        "log_invites_channel_id", "log_moderation_channel_id",
        "log_automod_channel_id",
    ),
    "antiraid": (
        "raid_mode", "raid_join_threshold", "raid_join_window", "raid_action",
        "raid_alert_channel_id", "raid_auto_off", "verification_mode",
        "verified_role_id", "unverified_role_id", "verification_channel_id",
        "captcha_type", "verification_timeout", "verification_attempts",
        "min_account_age", "min_age_action", "quarantine_role_id",
        "alt_flag_threshold", "alt_action_threshold", "alt_action",
    ),
    "leveling": (
        "xp_enabled", "xp_min", "xp_max", "xp_cooldown", "xp_multiplier",
        "level_up_mode", "level_up_channel_id", "level_up_message",
        "level_role_stack", "voice_xp_enabled", "voice_xp_rate",
    ),
    "welcome": (
        "welcome_channel_id", "welcome_message", "welcome_embed", "welcome_dm",
        "welcome_dm_message", "welcome_delete_after", "goodbye_channel_id",
        "goodbye_message", "goodbye_embed", "goodbye_delete_after",
    ),
    "starboard": (
        "starboard_channel_id", "starboard_emoji", "starboard_threshold",
        "starboard_selfstar", "starboard_nsfw",
    ),
}


class ServerConfig(commands.Cog):
    """Prefix, greetings, permission roles, module gating, backup and restore."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        #: Cached gating state per guild: disabled commands, disabled modules,
        #: and per-command overrides. Read before every command, so it is cached
        #: and dropped on write.
        self._gates: dict[int, dict[str, Any]] = {}

    async def cog_load(self) -> None:
        self.bot.add_check(self.gate_check)

    async def cog_unload(self) -> None:
        self.bot.remove_check(self.gate_check)

    async def open_config_panel(self, interaction: discord.Interaction) -> None:
        """Open the welcome panel from the root config panel."""
        from ui.panels.welcome import GreetingPanel

        config = await self.bot.guild_config.get(interaction.guild.id)
        view = GreetingPanel(self, interaction.user, config, "welcome")
        await interaction.response.send_message(
            embed=view.embed(interaction.guild), view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    @staticmethod
    def _ok(description: str) -> discord.Embed:
        return discord.Embed(
            description=f"{EMOJI_SUCCESS}  {description}", colour=COLOUR_SUCCESS
        )

    # =======================================================================
    # Gating
    # =======================================================================

    async def gates(self, guild_id: int) -> dict[str, Any]:
        """Disabled commands, disabled modules and overrides for one guild."""
        cached = self._gates.get(guild_id)
        if cached is not None:
            return cached

        disabled_commands = {
            row["command"]
            for row in await self.bot.db.fetchall(
                "SELECT command FROM disabled_commands WHERE guild_id = ?", (guild_id,)
            )
        }
        disabled_modules = {
            row["module"]
            for row in await self.bot.db.fetchall(
                "SELECT module FROM disabled_modules WHERE guild_id = ?", (guild_id,)
            )
        }
        overrides: dict[str, list[tuple[int, str, int]]] = {}
        for row in await self.bot.db.fetchall(
            "SELECT command, entity_id, entity_type, allow FROM command_overrides "
            "WHERE guild_id = ?",
            (guild_id,),
        ):
            overrides.setdefault(row["command"], []).append(
                (row["entity_id"], row["entity_type"], row["allow"])
            )

        state = {
            "commands": disabled_commands,
            "modules": disabled_modules,
            "overrides": overrides,
        }
        self._gates[guild_id] = state
        return state

    def invalidate(self, guild_id: int) -> None:
        self._gates.pop(guild_id, None)

    async def gate_check(self, ctx: commands.Context) -> bool:
        """Enforce ``?module``, ``?command`` and ``?permissions`` for every command.

        Registered bot-wide so no cog has to remember it. Owners bypass the whole
        thing: they are the people who have to fix a lockout.
        """
        if ctx.guild is None or ctx.command is None:
            return True
        if self.bot.config.is_owner(ctx.author.id):
            return True

        name = ctx.command.qualified_name
        root = name.split(" ", 1)[0]
        if root in PROTECTED_COMMANDS:
            return True

        state = await self.gates(ctx.guild.id)

        module = ctx.command.cog_name
        if module and module in state["modules"]:
            raise DeezeeError(
                f"The **{module}** module is disabled in this server. "
                "An administrator can re-enable it with `?module enable "
                f"{module}`."
            )

        if name in state["commands"] or root in state["commands"]:
            raise DeezeeError(
                f"`{name}` is disabled in this server "
                f"(`?command enable {root}` to undo)."
            )

        rules = state["overrides"].get(name) or state["overrides"].get(root)
        if not rules:
            return True

        author_roles = (
            {role.id for role in ctx.author.roles}
            if isinstance(ctx.author, discord.Member)
            else set()
        )

        allowed = False
        for entity_id, entity_type, allow in rules:
            matches = (
                (entity_type == "user" and entity_id == ctx.author.id)
                or (entity_type == "role" and entity_id in author_roles)
                or (entity_type == "channel" and entity_id == ctx.channel.id)
            )
            if not matches:
                continue
            if not allow:
                # A deny always wins. Two contradictory rules read the
                # restrictive way, which is the only safe reading.
                raise DeezeeError(
                    f"`{name}` has been denied for you here by a command override "
                    "(`?permissions show " + root + "`)."
                )
            allowed = True

        # An allow rule that matched nobody means the command is restricted to
        # whoever it does match.
        if any(allow for _, _, allow in rules) and not allowed:
            raise DeezeeError(
                f"`{name}` is restricted here. Ask an administrator, or check "
                f"`?permissions show {root}`."
            )
        return True

    # =======================================================================
    # ?config, ?prefix
    # =======================================================================

    @commands.hybrid_command(name="config", aliases=["settings", "setup"])
    @permissions.admin_only()
    @permissions.guild_only()
    async def config(self, ctx: commands.Context) -> None:
        """Open the master configuration panel.

        Every setting in the bot is reachable from here. There is no web
        dashboard, and there is not going to be one.
        """
        await open_root(ctx)

    @commands.hybrid_command(name="prefix")
    @app_commands.describe(new_prefix="New prefix, 1-5 characters. Blank shows it")
    @permissions.guild_only()
    async def prefix(
        self, ctx: commands.Context, new_prefix: Optional[str] = None
    ) -> None:
        """Show or change the command prefix for this server."""
        current = await self.bot.guild_config.prefix(
            ctx.guild.id, self.bot.config.default_prefix
        )

        if new_prefix is None:
            await ctx.send(
                embed=discord.Embed(
                    title="Prefix",
                    description=f"The prefix here is `{current}`.\n"
                    f"Mentioning me ({self.bot.user.mention}) always works too, so "
                    "a bad prefix can never lock you out.",
                    colour=COLOUR_INFO,
                )
            )
            return

        if not await permissions.has_tier(
            self.bot, ctx.author, ctx.guild, permissions.Tier.ADMIN
        ):
            raise commands.MissingPermissions(["manage_guild"])

        candidate = new_prefix.strip()
        if not 1 <= len(candidate) <= 5:
            raise DeezeeError("A prefix must be between 1 and 5 characters.")
        if candidate.startswith(("<@", "@")):
            raise DeezeeError("A prefix cannot start with a mention.")

        await self.bot.guild_config.set(ctx.guild.id, "prefix", candidate)
        await ctx.send(
            embed=self._ok(
                f"The prefix is now `{candidate}` (was `{current}`).\n"
                f"Try `{candidate}help`."
            )
        )

    # =======================================================================
    # Greetings
    # =======================================================================

    @commands.hybrid_command(name="welcome", with_app_command=False, aliases=["greet", "joinmessage"])
    @permissions.admin_only()
    @permissions.guild_only()
    async def welcome(self, ctx: commands.Context) -> None:
        """Configure the join message: channel, text, embed, DM copy, auto-delete."""
        await open_greeting(ctx, self, "welcome")

    @commands.hybrid_command(name="goodbye", with_app_command=False, aliases=["farewell", "leavemessage"])
    @permissions.admin_only()
    @permissions.guild_only()
    async def goodbye(self, ctx: commands.Context) -> None:
        """Configure the leave message: channel, text, embed, auto-delete."""
        await open_greeting(ctx, self, "goodbye")

    def render(self, member: discord.Member, template: str, count: int) -> str:
        """Substitute the greeting variables.

        A fixed list of replacements, not a scripting language: see DESIGN.md
        decision 4. Nothing in a template can execute.
        """
        return (
            template.replace("{mention}", member.mention)
            .replace("{user}", member.display_name)
            .replace("{tag}", str(member))
            .replace("{id}", str(member.id))
            .replace("{server}", member.guild.name)
            .replace("{count}", str(count))
        )

    async def send_greeting(
        self, member: discord.Member, kind: str, *, test: bool = False
    ) -> None:
        """Post a welcome or goodbye message for one member."""
        config = await self.bot.guild_config.get(member.guild.id)
        channel_id = config[f"{kind}_channel_id"]
        if not channel_id:
            raise DeezeeError(f"No {kind} channel is set.")

        channel = member.guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            raise DeezeeError(f"The {kind} channel no longer exists.")

        count = member.guild.member_count or 0
        text = self.render(member, str(config[f"{kind}_message"]), count)
        delete_after = int(config[f"{kind}_delete_after"]) or None

        if config[f"{kind}_embed"]:
            embed = discord.Embed(
                description=text,
                colour=COLOUR_SUCCESS if kind == "welcome" else COLOUR_DEFAULT,
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            if test:
                embed.set_footer(text="Test preview -- nobody actually joined or left")
            await channel.send(
                embed=embed,
                delete_after=delete_after,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        else:
            if test:
                text = f"*(test preview)* {text}"
            await channel.send(
                text[:2000],
                delete_after=delete_after,
                allowed_mentions=discord.AllowedMentions(users=True),
            )

        if kind == "welcome" and config["welcome_dm"] and config["welcome_dm_message"]:
            try:
                await member.send(
                    self.render(member, str(config["welcome_dm_message"]), count)[:2000]
                )
            except discord.HTTPException:
                # Closed DMs are the normal case, not an error worth surfacing.
                log.debug("Could not DM the welcome copy to %s", member.id)

    @commands.Cog.listener("on_member_join")
    async def greet_join(self, member: discord.Member) -> None:
        try:
            await self.send_greeting(member, "welcome")
        except DeezeeError:
            return  # Not configured. Nothing to report.
        except discord.HTTPException as exc:
            log.warning("Welcome message failed in %s: %s", member.guild.id, exc)

    @commands.Cog.listener("on_member_remove")
    async def greet_leave(self, member: discord.Member) -> None:
        try:
            await self.send_greeting(member, "goodbye")
        except DeezeeError:
            return
        except discord.HTTPException as exc:
            log.warning("Goodbye message failed in %s: %s", member.guild.id, exc)

    # =======================================================================
    # Permission roles
    # =======================================================================

    async def _role_list_command(
        self,
        ctx: commands.Context,
        action: str,
        role: discord.Role | None,
        kind: str,
    ) -> None:
        """Shared body for ?modroles, ?adminroles and ?protectedroles."""
        choice = action.lower()

        if choice == "list":
            if kind == "protected":
                ids = await self.bot.permissions.protected_roles(ctx.guild.id)
                title = "Protected roles"
                blurb = "These roles can never be targeted by moderation or automod."
            elif kind == "admin":
                ids = await self.bot.permissions.admin_roles(ctx.guild.id)
                title = "Admin roles"
                blurb = "Holders get the Administrator tier for Deezee commands."
            else:
                ids = await self.bot.permissions.mod_roles(ctx.guild.id)
                title = "Moderator roles"
                blurb = (
                    "Holders get the Moderator tier without needing Discord-level "
                    "permissions."
                )

            lines = [
                (ctx.guild.get_role(role_id).mention
                 if ctx.guild.get_role(role_id)
                 else f"*deleted role* `{role_id}`")
                for role_id in sorted(ids)
            ]
            pages = paginate_lines(
                lines, title=title, per_page=20, description=blurb,
                empty_message="None set.",
            )
            await Paginator(pages, ctx.author).start(ctx)
            return

        if role is None:
            raise DeezeeError("Name the role.")

        if choice == "add":
            if kind == "protected":
                added = await self.bot.permissions.add_protected_role(
                    ctx.guild.id, role.id, ctx.author.id
                )
                message = (
                    f"{role.mention} is protected. Nobody can be banned, kicked, "
                    "muted or automodded while they hold it."
                )
            else:
                added = await self.bot.permissions.add_permission_role(
                    ctx.guild.id, role.id, kind, ctx.author.id
                )
                message = f"{role.mention} now has the **{kind}** tier."
            if not added:
                raise DeezeeError(f"{role.mention} already had that.")
            await ctx.send(embed=self._ok(message))
            return

        if choice in {"remove", "delete"}:
            if kind == "protected":
                removed = await self.bot.permissions.remove_protected_role(
                    ctx.guild.id, role.id
                )
            else:
                removed = await self.bot.permissions.remove_permission_role(
                    ctx.guild.id, role.id, kind
                )
            if not removed:
                raise DeezeeError(f"{role.mention} did not have that.")
            await ctx.send(embed=self._ok(f"Removed {role.mention}."))
            return

        raise DeezeeError("Use `add`, `remove` or `list`.")

    @commands.hybrid_command(name="modroles", with_app_command=False)
    @app_commands.describe(action="add, remove or list", role="The role")
    @permissions.admin_only()
    @permissions.guild_only()
    async def modroles(
        self, ctx: commands.Context, action: str = "list",
        role: Optional[discord.Role] = None,
    ) -> None:
        """Define which roles count as moderators for permission checks."""
        await self._role_list_command(ctx, action, role, "mod")

    @commands.hybrid_command(name="adminroles", with_app_command=False)
    @app_commands.describe(action="add, remove or list", role="The role")
    @permissions.admin_only()
    @permissions.guild_only()
    async def adminroles(
        self, ctx: commands.Context, action: str = "list",
        role: Optional[discord.Role] = None,
    ) -> None:
        """Define which roles count as administrators for permission checks."""
        await self._role_list_command(ctx, action, role, "admin")

    @commands.hybrid_command(name="protectedroles", with_app_command=False, aliases=["immune"])
    @app_commands.describe(action="add, remove or list", role="The role")
    @permissions.admin_only()
    @permissions.guild_only()
    async def protectedroles(
        self, ctx: commands.Context, action: str = "list",
        role: Optional[discord.Role] = None,
    ) -> None:
        """Roles that can never be targeted by moderation or automod."""
        await self._role_list_command(ctx, action, role, "protected")

    # =======================================================================
    # Command and module gating
    # =======================================================================

    def _resolve_command(self, name: str) -> commands.Command:
        found = self.bot.get_command(name.strip().lstrip("?/"))
        if found is None:
            raise DeezeeError(f"There is no command called `{name}`.")
        return found

    @commands.hybrid_command(name="command", with_app_command=False)
    @app_commands.describe(
        action="enable, disable or list", command="Command name"
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def command_gate(
        self, ctx: commands.Context, action: str = "list",
        command: Optional[str] = None,
    ) -> None:
        """Enable or disable an individual command in this server."""
        choice = action.lower()
        state = await self.gates(ctx.guild.id)

        if choice == "list":
            lines = sorted(f"`{name}`" for name in state["commands"])
            pages = paginate_lines(
                lines,
                title="Disabled commands",
                per_page=25,
                description="`?command enable <name>` to switch one back on.\n"
                "These can never be disabled: "
                + ", ".join(f"`{c}`" for c in sorted(PROTECTED_COMMANDS)),
                empty_message="Every command is enabled.",
            )
            await Paginator(pages, ctx.author).start(ctx)
            return

        if command is None:
            raise DeezeeError("Name the command.")
        target = self._resolve_command(command)
        name = target.qualified_name

        if choice == "disable":
            if name.split(" ", 1)[0] in PROTECTED_COMMANDS:
                raise DeezeeError(
                    f"`{name}` cannot be disabled -- it is one of the ways back "
                    "out of a misconfiguration."
                )
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO disabled_commands "
                "(guild_id, command, disabled_by, created_at) VALUES (?, ?, ?, ?)",
                (ctx.guild.id, name, ctx.author.id, int(time.time())),
            )
            self.invalidate(ctx.guild.id)
            await ctx.send(embed=self._ok(f"`{name}` is disabled in this server."))
            return

        if choice == "enable":
            cursor = await self.bot.db.execute(
                "DELETE FROM disabled_commands WHERE guild_id = ? AND command = ?",
                (ctx.guild.id, name),
            )
            self.invalidate(ctx.guild.id)
            if not cursor.rowcount:
                raise DeezeeError(f"`{name}` was not disabled.")
            await ctx.send(embed=self._ok(f"`{name}` is enabled again."))
            return

        raise DeezeeError("Use `enable`, `disable` or `list`.")

    @commands.hybrid_command(name="module", with_app_command=False, aliases=["modules", "cog"])
    @app_commands.describe(action="enable, disable or list", module="Module name")
    @permissions.admin_only()
    @permissions.guild_only()
    async def module(
        self, ctx: commands.Context, action: str = "list",
        module: Optional[str] = None,
    ) -> None:
        """Enable or disable a whole feature module."""
        choice = action.lower()
        state = await self.gates(ctx.guild.id)

        if choice == "list":
            lines = []
            for name, cog in sorted(self.bot.cogs.items()):
                off = name in state["modules"]
                count = len(list(cog.walk_commands()))
                locked = "  *(cannot be disabled)*" if name in PROTECTED_MODULES else ""
                lines.append(
                    f"{EMOJI_OFF if off else EMOJI_ON} **{name}** — "
                    f"{count} command(s){locked}"
                )
            pages = paginate_lines(
                lines,
                title="Modules",
                per_page=15,
                description="`?module disable <name>` turns a whole category off.",
            )
            await Paginator(pages, ctx.author).start(ctx)
            return

        if module is None:
            raise DeezeeError("Name the module.")

        match = discord.utils.find(
            lambda n: n.lower() == module.strip().lower(), self.bot.cogs
        )
        if match is None:
            raise DeezeeError(
                f"There is no module called `{module}`. "
                f"Known: {', '.join(sorted(self.bot.cogs))}."
            )

        if choice == "disable":
            if match in PROTECTED_MODULES:
                raise DeezeeError(
                    f"**{match}** cannot be disabled -- it holds the commands that "
                    "would switch it back on."
                )
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO disabled_modules "
                "(guild_id, module, disabled_by, created_at) VALUES (?, ?, ?, ?)",
                (ctx.guild.id, match, ctx.author.id, int(time.time())),
            )
            self.invalidate(ctx.guild.id)
            await ctx.send(
                embed=self._ok(
                    f"**{match}** is disabled. Its listeners still run -- only its "
                    "commands are refused."
                )
            )
            return

        if choice == "enable":
            cursor = await self.bot.db.execute(
                "DELETE FROM disabled_modules WHERE guild_id = ? AND module = ?",
                (ctx.guild.id, match),
            )
            self.invalidate(ctx.guild.id)
            if not cursor.rowcount:
                raise DeezeeError(f"**{match}** was not disabled.")
            await ctx.send(embed=self._ok(f"**{match}** is enabled again."))
            return

        raise DeezeeError("Use `enable`, `disable` or `list`.")

    @commands.hybrid_command(name="permissions", with_app_command=False, aliases=["perms"])
    @app_commands.describe(
        action="allow, deny, reset or show",
        command="Which command",
        target="Role, channel or member -- mention, ID or name",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def permissions_cmd(
        self,
        ctx: commands.Context,
        action: str,
        command: str,
        *,
        target: Optional[str] = None,
    ) -> None:
        """Per-command overrides: allow or deny a command for a role, channel or member."""
        choice = action.lower()
        resolved = self._resolve_command(command)
        name = resolved.qualified_name

        if choice == "show":
            state = await self.gates(ctx.guild.id)
            rules = state["overrides"].get(name, [])
            lines = []
            for entity_id, entity_type, allow in rules:
                mention = {
                    "role": f"<@&{entity_id}>",
                    "channel": f"<#{entity_id}>",
                    "user": f"<@{entity_id}>",
                }[entity_type]
                lines.append(
                    f"{EMOJI_ON if allow else EMOJI_OFF} "
                    f"{'allow' if allow else 'deny'} — {mention}"
                )
            embed = discord.Embed(
                title=f"Overrides for {name}",
                description="\n".join(lines) or "No overrides. Normal permissions apply.",
                colour=COLOUR_INFO,
            )
            embed.set_footer(
                text="A deny always beats an allow. If any allow exists, everyone "
                "not matched by one is refused."
            )
            await ctx.send(embed=embed)
            return

        if name.split(" ", 1)[0] in PROTECTED_COMMANDS:
            raise DeezeeError(f"`{name}` cannot be overridden.")

        if choice == "reset":
            cursor = await self.bot.db.execute(
                "DELETE FROM command_overrides WHERE guild_id = ? AND command = ?",
                (ctx.guild.id, name),
            )
            self.invalidate(ctx.guild.id)
            await ctx.send(
                embed=self._ok(
                    f"Cleared **{cursor.rowcount}** override(s) for `{name}`."
                )
            )
            return

        if choice not in {"allow", "deny"}:
            raise DeezeeError("Use `allow`, `deny`, `reset` or `show`.")
        if target is None:
            raise DeezeeError("Name the role, channel or member.")

        entity, kind = await self._resolve_entity(ctx, target)
        await self.bot.db.execute(
            "INSERT INTO command_overrides (guild_id, command, entity_id, "
            "entity_type, allow, created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, command, entity_id, entity_type) "
            "DO UPDATE SET allow = excluded.allow",
            (
                ctx.guild.id, name, entity.id, kind,
                int(choice == "allow"), int(time.time()),
            ),
        )
        self.invalidate(ctx.guild.id)
        await ctx.send(
            embed=self._ok(
                f"`{name}` is now **{choice}ed** for {entity.mention}."
                + (
                    "\nBecause an allow exists, everyone not matched by one is now "
                    "refused this command."
                    if choice == "allow"
                    else ""
                )
            )
        )

    @staticmethod
    async def _resolve_entity(ctx: commands.Context, query: str) -> tuple[Any, str]:
        """Turn a mention, ID or name into a role, channel or member."""
        for converter, kind in (
            (commands.RoleConverter(), "role"),
            (commands.TextChannelConverter(), "channel"),
            (commands.MemberConverter(), "user"),
        ):
            try:
                return await converter.convert(ctx, query), kind
            except commands.BadArgument:
                continue
        raise DeezeeError(f"`{query}` is not a role, channel or member here.")

    @commands.hybrid_command(name="ignore", with_app_command=False)
    @app_commands.describe(
        action="add, remove or list",
        target="Channel, role or member the bot should ignore",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def ignore(
        self, ctx: commands.Context, action: str = "list",
        *, target: Optional[str] = None,
    ) -> None:
        """Stop the bot responding to commands in a channel, from a role, or from a member.

        This is a command-level ignore. Logging, automod and leveling are
        unaffected -- each has its own exclusion list.
        """
        choice = action.lower()

        if choice == "list":
            entries = await self.bot.permissions.blacklist(ctx.guild.id)
            lines = []
            for entity_id, kind in sorted(entries, key=lambda e: e[1]):
                mention = {
                    "role": f"<@&{entity_id}>",
                    "channel": f"<#{entity_id}>",
                    "user": f"<@{entity_id}>",
                }[kind]
                lines.append(f"{mention} — {kind}")
            pages = paginate_lines(
                lines,
                title="Ignored by commands",
                per_page=20,
                description="Bot owners are never ignored.",
                empty_message="Nothing is ignored.",
            )
            await Paginator(pages, ctx.author).start(ctx)
            return

        if target is None:
            raise DeezeeError("Name the channel, role or member.")
        entity, kind = await self._resolve_entity(ctx, target)

        if kind == "user" and self.bot.config.is_root_owner(entity.id):
            raise DeezeeError(
                f"{entity.mention} is a root owner and can never be ignored."
            )

        if choice == "add":
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO blacklist (guild_id, entity_id, entity_type, "
                "reason, added_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ctx.guild.id, entity.id, kind, f"?ignore by {ctx.author}",
                    ctx.author.id, int(time.time()),
                ),
            )
            self.bot.permissions.invalidate(ctx.guild.id)
            await ctx.send(
                embed=self._ok(f"I will not respond to commands from {entity.mention}.")
            )
            return

        if choice in {"remove", "delete"}:
            cursor = await self.bot.db.execute(
                "DELETE FROM blacklist WHERE guild_id = ? AND entity_id = ? "
                "AND entity_type = ?",
                (ctx.guild.id, entity.id, kind),
            )
            self.bot.permissions.invalidate(ctx.guild.id)
            if not cursor.rowcount:
                raise DeezeeError(f"{entity.mention} was not ignored.")
            await ctx.send(embed=self._ok(f"{entity.mention} is no longer ignored."))
            return

        raise DeezeeError("Use `add`, `remove` or `list`.")

    # =======================================================================
    # Backup and restore
    # =======================================================================

    async def export_payload(self, guild: discord.Guild) -> dict[str, Any]:
        """Build the export document for one guild."""
        config = dict(await self.bot.guild_config.get(guild.id))
        settings = {
            key: value for key, value in config.items() if key not in EXPORT_EXCLUDE
        }

        async def rows(sql: str) -> list[dict[str, Any]]:
            return [dict(row) for row in await self.bot.db.fetchall(sql, (guild.id,))]

        return {
            "format": 1,
            "exported_at": int(time.time()),
            "guild_id": guild.id,
            "guild_name": guild.name,
            "settings": settings,
            "permission_roles": await rows(
                "SELECT role_id, tier FROM permission_roles WHERE guild_id = ?"
            ),
            "protected_roles": await rows(
                "SELECT role_id FROM protected_roles WHERE guild_id = ?"
            ),
            "autoroles": await rows(
                "SELECT role_id, for_bots, delay_seconds FROM autoroles WHERE guild_id = ?"
            ),
            "self_roles": await rows(
                "SELECT role_id, description FROM self_roles WHERE guild_id = ?"
            ),
            "level_roles": await rows(
                "SELECT level, role_id FROM level_roles WHERE guild_id = ?"
            ),
            "xp_blacklist": await rows(
                "SELECT entity_id, entity_type FROM xp_blacklist WHERE guild_id = ?"
            ),
            "log_disabled_events": await rows(
                "SELECT event FROM log_disabled_events WHERE guild_id = ?"
            ),
            "log_ignores": await rows(
                "SELECT entity_id, entity_type FROM log_ignores WHERE guild_id = ?"
            ),
            "disabled_commands": await rows(
                "SELECT command FROM disabled_commands WHERE guild_id = ?"
            ),
            "disabled_modules": await rows(
                "SELECT module FROM disabled_modules WHERE guild_id = ?"
            ),
            "command_overrides": await rows(
                "SELECT command, entity_id, entity_type, allow FROM command_overrides "
                "WHERE guild_id = ?"
            ),
        }

    @commands.hybrid_command(name="configexport", with_app_command=False, aliases=["backup"])
    @permissions.admin_only()
    @permissions.guild_only()
    async def configexport(self, ctx: commands.Context) -> None:
        """Export this server's configuration as a JSON attachment.

        Moderation history is **not** included: cases, warnings and notes are the
        one thing worth keeping and the one thing nobody should be able to
        rewrite by editing a text file.
        """
        payload = await self.export_payload(ctx.guild)
        raw = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")

        await ctx.send(
            embed=discord.Embed(
                title="Configuration export",
                description=(
                    f"**{len(payload['settings'])}** settings plus role, logging and "
                    "gating tables.\n\nNo secrets are included -- the token lives in "
                    "`.env` and never touches the database. Moderation history is "
                    "excluded on purpose.\n\nRestore it with `?configimport` and this "
                    "file attached."
                ),
                colour=COLOUR_INFO,
            ),
            file=discord.File(
                io.BytesIO(raw), filename=f"deezee-config-{ctx.guild.id}.json"
            ),
        )

    @commands.hybrid_command(name="configimport", with_app_command=False, aliases=["restore"])
    @app_commands.describe(attachment="A file produced by ?configexport")
    @permissions.admin_only()
    @permissions.guild_only()
    async def configimport(
        self, ctx: commands.Context, attachment: Optional[discord.Attachment] = None
    ) -> None:
        """Restore configuration from a previously exported JSON file."""
        if attachment is None and ctx.message is not None and ctx.message.attachments:
            attachment = ctx.message.attachments[0]
        if attachment is None:
            raise DeezeeError("Attach the JSON file produced by `?configexport`.")
        if attachment.size > 2 * 1024 * 1024:
            raise DeezeeError("That file is larger than 2 MiB.")

        try:
            payload = json.loads((await attachment.read()).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeezeeError(f"That file is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict) or "settings" not in payload:
            raise DeezeeError("That file is not a Deezee configuration export.")

        current = await self.bot.guild_config.get(ctx.guild.id)
        writable = self.bot.guild_config.settings
        incoming = {
            key: value
            for key, value in payload["settings"].items()
            if key in writable
        }
        changes = {
            key: value for key, value in incoming.items() if current.get(key) != value
        }
        unknown = sorted(set(payload["settings"]) - set(writable))

        if not changes:
            await ctx.send(
                embed=discord.Embed(
                    description="That export matches the current configuration "
                    "exactly. Nothing to do.",
                    colour=COLOUR_DEFAULT,
                )
            )
            return

        preview = "\n".join(
            f"`{key}`: `{current.get(key)}` → `{value}`"
            for key, value in list(changes.items())[:15]
        )
        proceed = await confirm(
            ctx,
            title=f"Apply {len(changes)} setting change(s)?",
            description=(
                f"From an export of **{payload.get('guild_name', 'unknown')}** taken "
                f"<t:{payload.get('exported_at', int(time.time()))}:R>.\n\n{preview}"
                + (f"\n\n…and {len(changes) - 15} more." if len(changes) > 15 else "")
                + (
                    f"\n\n**{len(unknown)} unknown key(s) will be skipped**: "
                    f"{', '.join(unknown[:5])}"
                    if unknown
                    else ""
                )
                + "\n\nThe current values are snapshotted first, so `?import undo "
                "config` can put them back for 7 days."
            ),
            confirm_label=f"Apply {len(changes)}",
        )
        if not proceed:
            return

        await self.snapshot(ctx.guild.id, "config", {k: current.get(k) for k in changes},
                            ctx.author.id)
        await self.bot.guild_config.set_many(ctx.guild.id, changes)
        self.invalidate(ctx.guild.id)

        await ctx.send(
            embed=self._ok(
                f"Applied **{len(changes)}** setting(s)."
                + (f" Skipped {len(unknown)} unknown key(s)." if unknown else "")
            )
        )

    async def snapshot(
        self, guild_id: int, section: str, payload: dict[str, Any], author_id: int
    ) -> int:
        """Record the previous values of a section, for ``?import undo``."""
        cursor = await self.bot.db.execute(
            "INSERT INTO config_snapshots (guild_id, section, payload, created_by, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, section, json.dumps(payload), author_id, int(time.time())),
        )
        # Seven-day retention, pruned opportunistically rather than on a timer:
        # the only thing that creates snapshots is the only thing that needs to
        # clean them up.
        await self.bot.db.execute(
            "DELETE FROM config_snapshots WHERE created_at < ?",
            (int(time.time()) - 7 * 86400,),
        )
        return int(cursor.lastrowid or 0)

    @commands.hybrid_command(name="configreset", with_app_command=False)
    @app_commands.describe(section="A module name, or 'all'")
    @permissions.admin_only()
    @permissions.guild_only()
    async def configreset(self, ctx: commands.Context, section: str) -> None:
        """Reset one module's settings, or every setting, to defaults."""
        choice = section.strip().lower()

        if choice == "all":
            proceed = await confirm(
                ctx,
                title="Reset every setting in this server?",
                description=(
                    "**Every** configured value returns to its default: log "
                    "channels, automod, anti-raid, leveling, welcome messages, "
                    "prefix, starboard.\n\n"
                    "Moderation history, warnings, notes, XP and tags are **not** "
                    "touched.\n\nThis cannot be undone from inside Discord. Run "
                    "`?configexport` first if you are not certain."
                ),
                confirm_label="Reset everything",
            )
            if not proceed:
                return
            second = await confirm(
                ctx,
                title="Confirm again",
                description="This is the destructive one. Press it only if you "
                "meant the first press.",
                confirm_label="Yes, reset everything",
            )
            if not second:
                return

            await self.bot.guild_config.reset_all(ctx.guild.id)
            self.invalidate(ctx.guild.id)
            await ctx.send(embed=self._ok("Every setting is back to its default."))
            return

        if choice not in RESET_SECTIONS:
            raise DeezeeError(
                "Section must be `all` or one of: "
                + ", ".join(f"`{name}`" for name in sorted(RESET_SECTIONS))
            )

        keys = [k for k in RESET_SECTIONS[choice] if k in self.bot.guild_config.settings]
        current = await self.bot.guild_config.get(ctx.guild.id)

        proceed = await confirm(
            ctx,
            title=f"Reset the {choice} settings?",
            description=f"**{len(keys)}** setting(s) return to their defaults.",
            confirm_label=f"Reset {choice}",
        )
        if not proceed:
            return

        await self.snapshot(
            ctx.guild.id, choice, {k: current.get(k) for k in keys}, ctx.author.id
        )
        for key in keys:
            await self.bot.guild_config.reset(ctx.guild.id, key)
        self.invalidate(ctx.guild.id)

        await ctx.send(
            embed=self._ok(f"Reset **{len(keys)}** {choice} setting(s) to defaults.")
        )

    # =======================================================================
    # ?diagnose
    # =======================================================================

    @commands.hybrid_command(name="diagnose")
    @app_commands.describe(target="A command or module name")
    @permissions.admin_only()
    @permissions.guild_only()
    async def diagnose(self, ctx: commands.Context, *, target: str) -> None:
        """Explain why a command or module is not working here.

        Checks, in the order they would actually stop the command: module gate,
        command gate, overrides, the bot's own Discord permissions, and whatever
        that feature needs configured.
        """
        query = target.strip().lstrip("?/")
        state = await self.gates(ctx.guild.id)
        findings: list[str] = []

        module_match = discord.utils.find(
            lambda n: n.lower() == query.lower(), self.bot.cogs
        )
        command_match = self.bot.get_command(query)

        if module_match is None and command_match is None:
            raise DeezeeError(
                f"`{query}` is neither a command nor a module. "
                f"Modules: {', '.join(sorted(self.bot.cogs))}."
            )

        subject = module_match or command_match.qualified_name
        module = module_match or (command_match.cog_name if command_match else None)

        if module and module in state["modules"]:
            findings.append(
                f"{EMOJI_OFF} The **{module}** module is disabled "
                f"(`?module enable {module}`)."
            )
        else:
            findings.append(f"{EMOJI_ON} The module is enabled.")

        if command_match is not None:
            name = command_match.qualified_name
            if name in state["commands"]:
                findings.append(
                    f"{EMOJI_OFF} `{name}` is disabled (`?command enable {name}`)."
                )
            else:
                findings.append(f"{EMOJI_ON} The command is enabled.")

            rules = state["overrides"].get(name, [])
            if rules:
                findings.append(
                    f"{EMOJI_OFF} `{name}` has **{len(rules)}** override(s) "
                    f"(`?permissions show {name}`)."
                )
            else:
                findings.append(f"{EMOJI_ON} No command overrides.")

        blacklist = await self.bot.permissions.blacklist(ctx.guild.id)
        if (ctx.channel.id, "channel") in blacklist:
            findings.append(
                f"{EMOJI_OFF} This channel is on the `?ignore` list, so I answer "
                "nothing here."
            )

        me = ctx.guild.me
        needed = {
            "Moderation": ("ban_members", "kick_members", "moderate_members",
                           "manage_messages", "manage_channels"),
            "Automod": ("manage_messages", "moderate_members"),
            "AntiRaid": ("manage_roles", "kick_members", "ban_members"),
            "Roles": ("manage_roles",),
            "Logging": ("view_audit_log",),
            "Leveling": ("manage_roles",),
            "Utility": ("manage_messages", "manage_expressions"),
        }.get(module or "", ())
        missing = [
            name.replace("_", " ").title()
            for name in needed
            if not getattr(me.guild_permissions, name)
        ]
        if missing:
            findings.append(
                f"{EMOJI_OFF} I am missing: **{', '.join(missing)}**. "
                "Grant them in Server Settings → Roles."
            )
        elif needed:
            findings.append(f"{EMOJI_ON} I have every Discord permission this needs.")

        config = await self.bot.guild_config.get(ctx.guild.id)
        requirements = {
            "Logging": ("log_messages_channel_id", "a logging channel (`?logging`)"),
            "Leveling": ("xp_enabled", "XP switched on (`?levelconfig`)"),
            "Economy": ("economy_enabled", "the economy switched on (`?ecoadmin`)"),
            "AntiRaid": ("quarantine_role_id", "a quarantine role (`?config` → Anti-raid)"),
        }.get(module or "")
        if requirements:
            key, description = requirements
            if key in config and not config[key]:
                findings.append(f"{EMOJI_OFF} This module needs {description}.")
            elif key in config:
                findings.append(f"{EMOJI_ON} Required configuration is present.")

        role_note = ""
        if me.top_role.position < len(ctx.guild.roles) - 1:
            above = [
                r.name for r in ctx.guild.roles
                if r > me.top_role and not r.is_default()
            ]
            if above:
                role_note = (
                    f"\n\nMy highest role is **{me.top_role.name}**, below "
                    f"{len(above)} other role(s). I cannot act on anyone whose "
                    "highest role is above mine."
                )

        embed = discord.Embed(
            title=f"Diagnosis: {subject}",
            description="\n".join(findings) + role_note,
            colour=COLOUR_WARNING if any(EMOJI_OFF in f for f in findings)
            else COLOUR_SUCCESS,
        )
        embed.set_footer(text="Checked in the order these would actually stop it.")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point."""
    await bot.add_cog(ServerConfig(bot))
