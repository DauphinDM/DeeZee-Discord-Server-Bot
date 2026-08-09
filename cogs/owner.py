"""Owner tools: reload, sync, shutdown, bot-wide blacklist, diagnostics.

Not part of the 188-command sheet. These are the operator controls the design
document lists in the file tree, and they exist because a bot on a panel host
with no shell access needs a way to reload a cog and re-sync the command tree
without a redeploy.

Everything here is **prefix-only and owner-gated**. Prefix-only because Discord
caps a guild at 100 slash commands and operator tools are the last thing that
should spend one. Owner-gated because every command here can change how the bot
behaves for everybody.

There is no ``eval`` and there will not be one. A bot that can execute arbitrary
Python from a chat message is one leaked account away from being a shell on the
host -- see DESIGN.md decision 4.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import discord
from discord.ext import commands

from core import permissions
from core.constants import (
    COLOUR_DEFAULT,
    COLOUR_ERROR,
    COLOUR_INFO,
    COLOUR_SUCCESS,
    EMOJI_SUCCESS,
    TS_RELATIVE,
)
from core.errors import DeezeeError
from services.timeparse import format_duration
from ui.paginator import Paginator, paginate_lines
from ui.views import confirm

log = logging.getLogger(__name__)

#: Tables worth reporting a row count for. Anything growth-bounded, so a number
#: creeping up is visible before it becomes a storage problem.
COUNTED_TABLES = (
    "mod_cases", "warnings", "notes", "scheduled_actions", "join_log",
    "levels", "economy", "tags", "autoresponses", "polls", "reminders",
    "starboard_messages", "scan_cache", "import_capture",
)


class Owner(commands.Cog):
    """Operator controls. Prefix-only, owner-gated, no arbitrary execution."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @staticmethod
    def _ok(description: str) -> discord.Embed:
        return discord.Embed(
            description=f"{EMOJI_SUCCESS}  {description}", colour=COLOUR_SUCCESS
        )

    async def _describe(self, user_id: int, guild: discord.Guild | None) -> str:
        """One line for one owner: who they are, and whether they are here.

        Falls back to the bare ID rather than dropping the entry. An owner the
        bot cannot resolve -- a deleted account, an API hiccup -- still holds
        that authority, and a list that quietly omits them is worse than one
        that says it could not look them up.
        """
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                return f"`{user_id}` — *could not look this account up*"

        here = ""
        if guild is not None:
            here = "" if guild.get_member(user_id) else "  *(not in this server)*"
        return f"{user.mention} — {user} (`{user_id}`){here}"

    @commands.command(name="owners", aliases=["botowners"])
    @permissions.admin_only()
    async def owners(self, ctx: commands.Context) -> None:
        """Show who owns this bot, in both tiers.

        Admin-gated rather than public. Knowing who to impersonate is the first
        step of most social engineering, and a moderator knowing is enough.
        """
        root_ids = sorted(self.bot.config.root_owner_ids)
        owner_ids = sorted(self.bot.config.owner_ids)

        embed = discord.Embed(
            title="Bot owners",
            description=(
                "Two tiers. **Root** is absolute: nothing at runtime can demote, "
                "blacklist, ignore, deny or moderate a root owner — not another "
                "owner, not the server owner, not an Administrator. Attempts are "
                "refused and recorded in `permission_audit`."
            ),
            colour=COLOUR_INFO,
        )

        for label, ids, note in (
            ("Root", root_ids, "absolute authority"),
            ("Owner", owner_ids, "full bot power, but cannot touch a root owner "
                                 "or edit either list"),
        ):
            lines = [await self._describe(user_id, ctx.guild) for user_id in ids]
            embed.add_field(
                name=f"{label} — {note}",
                value="\n".join(lines) if lines else "*none configured*",
                inline=False,
            )

        yours = []
        if ctx.author.id in root_ids:
            yours.append("a **root owner**")
        elif ctx.author.id in owner_ids:
            yours.append("a **bot owner**")
        if yours:
            embed.add_field(name="You", value=f"You are {yours[0]}.", inline=False)

        embed.set_footer(
            text="Neither list can be changed from inside Discord. "
            "Editing .env and restarting is the only way."
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="reload")
    @permissions.owner_only()
    async def reload(self, ctx: commands.Context, extension: str = "all") -> None:
        """Reload one cog, or every cog.

        A cog that fails to reload is reported by name with its error; the ones
        that succeeded stay reloaded. Half a reload is better than a bot that
        refuses to start.
        """
        targets = (
            [f"cogs.{path.stem}" for path in
             sorted((self.bot.config.project_root / "cogs").glob("*.py"))
             if not path.name.startswith("_")]
            if extension.lower() in {"all", "*"}
            else [f"cogs.{extension.lower().removeprefix('cogs.')}"]
        )

        succeeded: list[str] = []
        failed: list[tuple[str, str]] = []
        for module in targets:
            try:
                await self.bot.reload_extension(module)
            except commands.ExtensionNotLoaded:
                try:
                    await self.bot.load_extension(module)
                except Exception as exc:
                    failed.append((module, f"{type(exc).__name__}: {exc}"))
                    continue
                succeeded.append(module)
            except Exception as exc:
                failed.append((module, f"{type(exc).__name__}: {exc}"))
            else:
                succeeded.append(module)

        embed = discord.Embed(
            title="Reload",
            colour=COLOUR_SUCCESS if not failed else COLOUR_ERROR,
            description=f"**{len(succeeded)}** reloaded, **{len(failed)}** failed.",
        )
        if succeeded:
            embed.add_field(
                name="Reloaded",
                value=", ".join(m.removeprefix("cogs.") for m in succeeded)[:1024],
                inline=False,
            )
        for module, why in failed[:5]:
            embed.add_field(name=module, value=f"```{why[:900]}```", inline=False)
        if failed:
            embed.set_footer(
                text="The tree is unchanged until you run ?sync -- reloading a cog "
                "does not re-register its slash commands."
            )
        await ctx.send(embed=embed)

    @commands.command(name="sync")
    @permissions.owner_only()
    async def sync(self, ctx: commands.Context, scope: str = "guild") -> None:
        """Re-register the application-command tree.

        ``guild`` syncs to every guild in ``GUILD_IDS`` and applies immediately.
        ``global`` syncs globally and can take up to an hour to appear, which is
        why it is not the default.
        """
        choice = scope.lower()
        if choice not in {"guild", "global"}:
            raise DeezeeError("Scope must be `guild` or `global`.")

        if ctx.interaction is None:
            await ctx.typing()

        if choice == "global":
            proceed = await confirm(
                ctx,
                title="Sync globally?",
                description=(
                    "A global sync can take up to an hour to propagate and applies "
                    "to every server the bot is in. A guild sync is immediate and "
                    "is almost always what you want."
                ),
                confirm_label="Sync globally",
                danger=False,
            )
            if not proceed:
                return
            synced = await self.bot.tree.sync()
            await ctx.send(
                embed=self._ok(
                    f"Synced **{len(synced)}** command(s) globally. "
                    "Allow up to an hour."
                )
            )
            return

        results: list[str] = []
        for guild_id in sorted(self.bot.config.allowed_guild_ids):
            guild = discord.Object(id=guild_id)
            try:
                self.bot.tree.copy_global_to(guild=guild)
                synced = await self.bot.tree.sync(guild=guild)
            except discord.HTTPException as exc:
                results.append(f"`{guild_id}` — failed: {exc}")
            else:
                results.append(f"`{guild_id}` — {len(synced)} command(s)")

        await ctx.send(
            embed=discord.Embed(
                title="Command tree synced",
                description="\n".join(results),
                colour=COLOUR_SUCCESS,
            ).set_footer(
                text="Discord allows 100 top-level slash commands per guild. "
                "Prefix commands are not capped."
            )
        )

    @commands.command(name="shutdown", aliases=["die"])
    @permissions.root_only()
    async def shutdown(self, ctx: commands.Context) -> None:
        """Stop the bot cleanly.

        Root-only. On a panel host the supervisor will normally restart the
        process, so this is a restart button as much as a stop button.
        """
        pending = await self.bot.scheduler.pending_count()
        proceed = await confirm(
            ctx,
            title="Shut down?",
            description=(
                f"The scheduler has **{pending}** pending action(s). They are in "
                "SQLite and will be picked up on the next boot, so nothing is lost."
                "\n\nOn Wispbyte the panel restarts the process automatically."
            ),
            confirm_label="Shut down",
        )
        if not proceed:
            return

        await ctx.send(embed=self._ok("Shutting down. See you shortly."))
        log.info("Shutdown requested by %s (%s)", ctx.author, ctx.author.id)
        await self.bot.close()

    @commands.command(name="botblacklist")
    @permissions.root_only()
    async def botblacklist(
        self, ctx: commands.Context, action: str = "list", user_id: int = 0
    ) -> None:
        """Ignore a user across every guild.

        Root-only, and a root owner can never be added: the write is refused
        here, which is the same rule the hierarchy guard enforces everywhere
        else.
        """
        choice = action.lower()

        if choice == "list":
            rows = await self.bot.db.fetchall(
                "SELECT entity_id, reason, created_at FROM blacklist "
                "WHERE guild_id = 0 AND entity_type = 'user' ORDER BY created_at DESC"
            )
            lines = [
                f"<@{row['entity_id']}> `{row['entity_id']}` — "
                f"{row['reason'] or 'no reason'} "
                f"(<t:{row['created_at']}:{TS_RELATIVE}>)"
                for row in rows
            ]
            pages = paginate_lines(
                lines,
                title="Bot-wide blacklist",
                per_page=15,
                description="These users are ignored in every guild.",
                empty_message="Nobody is blacklisted bot-wide.",
            )
            await Paginator(pages, ctx.author).start(ctx)
            return

        if not user_id:
            raise DeezeeError("Give a user ID.")
        if self.bot.config.is_root_owner(user_id):
            raise DeezeeError(
                "A root owner cannot be blacklisted. That is the whole point of the "
                "tier."
            )

        if choice == "add":
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO blacklist (guild_id, entity_id, entity_type, "
                "reason, added_by, created_at) VALUES (0, ?, 'user', ?, ?, ?)",
                (user_id, f"bot-wide by {ctx.author}", ctx.author.id, int(time.time())),
            )
            self.bot.permissions.clear()
            await ctx.send(
                embed=self._ok(f"<@{user_id}> is ignored in every guild.")
            )
            return

        if choice in {"remove", "delete"}:
            cursor = await self.bot.db.execute(
                "DELETE FROM blacklist WHERE guild_id = 0 AND entity_id = ? "
                "AND entity_type = 'user'",
                (user_id,),
            )
            self.bot.permissions.clear()
            if not cursor.rowcount:
                raise DeezeeError(f"`{user_id}` was not blacklisted bot-wide.")
            await ctx.send(embed=self._ok(f"<@{user_id}> is no longer blacklisted."))
            return

        raise DeezeeError("Use `add <id>`, `remove <id>` or `list`.")

    @commands.command(name="guilds")
    @permissions.owner_only()
    async def guilds(self, ctx: commands.Context) -> None:
        """List the guilds the bot is in, and whether each is on the allowlist."""
        allowed = self.bot.config.allowed_guild_ids
        lines = [
            f"**{guild.name}** `{guild.id}` — {guild.member_count or '?'} member(s)"
            + ("" if guild.id in allowed else "  ⚠ **not on the allowlist**")
            for guild in sorted(self.bot.guilds, key=lambda g: g.name)
        ]
        listed = [
            f"`{guild_id}` — not joined"
            for guild_id in sorted(allowed)
            if self.bot.get_guild(guild_id) is None
        ]
        pages = paginate_lines(
            lines + listed,
            title="Guilds",
            per_page=15,
            colour=COLOUR_INFO,
            description=f"GUILD_IDS holds {len(allowed)} entry(s). Anything not on "
            "it is left automatically on join.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @commands.command(name="dbstats")
    @permissions.owner_only()
    async def dbstats(self, ctx: commands.Context) -> None:
        """Row counts and file size, against the 1 GiB storage budget."""
        try:
            size = self.bot.db.path.stat().st_size
        except OSError:
            size = 0

        try:
            wal = self.bot.db.path.with_name(self.bot.db.path.name + "-wal").stat().st_size
        except OSError:
            wal = 0

        lines = []
        for table in COUNTED_TABLES:
            try:
                count = await self.bot.db.fetchval(f"SELECT COUNT(*) FROM {table}")
            except Exception:
                continue  # A table from a migration this build does not have.
            lines.append(f"`{table}` — {int(count or 0):,}")

        embed = discord.Embed(
            title="Database",
            description="\n".join(lines),
            colour=COLOUR_INFO if size < 500 * 1024 * 1024 else COLOUR_ERROR,
        )
        embed.add_field(
            name="File", value=f"{size / 1024 / 1024:.1f} MiB", inline=True
        )
        embed.add_field(name="WAL", value=f"{wal / 1024 / 1024:.1f} MiB", inline=True)
        embed.add_field(
            name="Budget",
            value=f"{(size + wal) / 1024 / 1024 / 1024 * 100:.1f}% of 1 GiB",
            inline=True,
        )
        embed.add_field(
            name="Uptime", value=format_duration(int(self.bot.uptime)), inline=True
        )
        embed.add_field(
            name="Scheduler",
            value=f"{await self.bot.scheduler.pending_count()} pending",
            inline=True,
        )
        embed.set_footer(
            text="VACUUM runs on boot once the file passes 200 MiB."
        )
        await ctx.send(embed=embed)

    @commands.command(name="commandcount")
    @permissions.owner_only()
    async def commandcount(self, ctx: commands.Context) -> None:
        """How many commands exist, and how many of them are slash commands.

        Worth having as a command rather than a note: the slash figure is capped
        at 100 by Discord, and this is how you find out you are near it.
        """
        prefix_total = len(list(self.bot.walk_commands()))
        slash_top = len(self.bot.tree.get_commands())

        lines = []
        for name, cog in sorted(self.bot.cogs.items()):
            walkable = len(list(cog.walk_commands()))
            slash = sum(
                1
                for command in cog.walk_commands()
                if command.parent is None
                and getattr(command, "with_app_command", False)
            )
            lines.append(f"**{name}** — {walkable} command(s), {slash} on slash")

        embed = discord.Embed(
            title="Command inventory",
            description="\n".join(lines),
            colour=COLOUR_DEFAULT,
        )
        embed.add_field(
            name="Totals",
            value=f"**{prefix_total}** commands, all available with the prefix.\n"
            f"**{slash_top}** of 100 top-level slash commands used.",
            inline=False,
        )
        embed.set_footer(
            text="Discord caps a guild at 100 top-level application commands. "
            "Prefix commands are not capped."
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point."""
    await bot.add_cog(Owner(bot))
