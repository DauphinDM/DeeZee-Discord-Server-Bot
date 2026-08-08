"""Information, tools and the things that make a server usable day to day.

Covers the 27 commands in the command sheet's "Utility" section.

Three of them carry state that has to survive a restart -- polls, suggestions
and reminders -- and each does it the same way: the row lives in SQLite, the
message keeps a fixed ``custom_id``, and the handler is re-attached on boot by
pattern rather than by registering one view per object.
"""

from __future__ import annotations

import io
import json
import logging
import platform
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core import permissions
from core.constants import (
    COLOUR_DEFAULT,
    COLOUR_ERROR,
    COLOUR_INFO,
    COLOUR_MUTED,
    COLOUR_SUCCESS,
    COLOUR_WARNING,
    DISCORD_EPOCH_MS,
    EMOJI_SUCCESS,
    MAX_SELECT_OPTIONS,
    TS_LONG_DATE_TIME,
    TS_RELATIVE,
)
from core.errors import DeezeeError, NotConfigured
from services.timeparse import format_duration, parse_duration
from ui.paginator import Paginator, paginate_lines, paginate_text
from ui.panels.utility import EmbedBuilder, HelpView
from ui.views import BaseView

log = logging.getLogger(__name__)

#: Options a poll may carry. Ten is Discord's practical button ceiling for two
#: rows, and a poll with more than ten choices is a survey.
MAX_POLL_OPTIONS = 10

#: Number emoji used as poll button faces.
POLL_FACES = ("1️⃣", "2️⃣", "3️⃣", "4️⃣",
              "5️⃣", "6️⃣", "7️⃣", "8️⃣",
              "9️⃣", "\N{KEYCAP TEN}")

#: Highlight notifications are suppressed if the member has spoken in the
#: channel this recently -- they are already reading it.
HIGHLIGHT_PRESENCE_WINDOW = 300

#: Cap on a highlight word list, per user.
MAX_HIGHLIGHTS = 25

#: Bytes accepted for an emoji upload. Discord's own limit is 256 KiB.
MAX_EMOJI_BYTES = 256 * 1024

_TIME_ONLY = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_DATE_TIME = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$"
)
_CUSTOM_EMOJI = re.compile(r"<(a)?:(\w+):(\d+)>")


def _memory_mib() -> float | None:
    """Resident memory of this process, or ``None`` if it cannot be read.

    Two implementations because the bot is developed on Windows and deployed on
    a Linux panel, and neither method works on the other platform.
    """
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass

    if platform.system() == "Windows":
        try:
            import ctypes
            import ctypes.wintypes

            class _Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.wintypes.DWORD),
                    ("PageFaultCount", ctypes.wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            handle_fn = ctypes.windll.kernel32.GetCurrentProcess
            handle_fn.restype = ctypes.wintypes.HANDLE
            info_fn = ctypes.windll.psapi.GetProcessMemoryInfo
            # Explicit argtypes are load-bearing on 64-bit: without them the
            # HANDLE is truncated to 32 bits and every field comes back zero.
            info_fn.argtypes = [
                ctypes.wintypes.HANDLE,
                ctypes.POINTER(_Counters),
                ctypes.wintypes.DWORD,
            ]
            counters = _Counters()
            counters.cb = ctypes.sizeof(_Counters)
            info_fn(handle_fn(), ctypes.byref(counters), counters.cb)
            return counters.WorkingSetSize / 1024 / 1024
        except Exception:  # pragma: no cover - diagnostics must never raise
            return None

    return None


# ---------------------------------------------------------------------------
# Persistent components
# ---------------------------------------------------------------------------


class PollButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"poll:(?P<poll>[0-9]+):(?P<option>[0-9]+)",
):
    """One vote button. Survives restarts by pattern, not by registration."""

    def __init__(self, poll_id: int, index: int, label: str) -> None:
        super().__init__(
            discord.ui.Button(
                label=label[:80],
                emoji=POLL_FACES[index] if index < len(POLL_FACES) else None,
                style=discord.ButtonStyle.secondary,
                custom_id=f"poll:{poll_id}:{index}",
            ),
            # Row belongs to the wrapper: DynamicItem takes its own ``row`` and
            # ignores the inner item's.
            row=index // 5,
        )
        self.poll_id = poll_id
        self.index = index

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str],
    ) -> PollButton:
        return cls(int(match["poll"]), int(match["option"]), item.label or "Option")

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Utility")
        if cog is None:
            await interaction.response.send_message(
                "Polls are unavailable right now.", ephemeral=True
            )
            return
        await cog.record_vote(interaction, self.poll_id, self.index)


class SuggestionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"sug:(?P<id>[0-9]+):(?P<dir>up|down)",
):
    """Up or down on a suggestion."""

    def __init__(self, suggestion_id: int, direction: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="Upvote" if direction == "up" else "Downvote",
                emoji="\N{THUMBS UP SIGN}" if direction == "up"
                else "\N{THUMBS DOWN SIGN}",
                style=discord.ButtonStyle.success
                if direction == "up"
                else discord.ButtonStyle.danger,
                custom_id=f"sug:{suggestion_id}:{direction}",
            )
        )
        self.suggestion_id = suggestion_id
        self.direction = direction

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str],
    ) -> SuggestionButton:
        return cls(int(match["id"]), match["dir"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Utility")
        if cog is None:
            await interaction.response.send_message(
                "Suggestions are unavailable right now.", ephemeral=True
            )
            return
        await cog.record_suggestion_vote(
            interaction, self.suggestion_id, 1 if self.direction == "up" else -1
        )


class ReminderCancelView(BaseView):
    """A dropdown of the invoker's pending reminders, for cancelling them."""

    def __init__(self, cog: Any, author: discord.abc.User, rows: list[dict[str, Any]]) -> None:
        super().__init__(author)
        self.cog = cog
        options = [
            discord.SelectOption(
                label=f"#{row['id']} — {row['text'][:60]}",
                value=str(row["id"]),
                description=f"Fires in {format_duration(row['remind_at'] - int(time.time()))}",
            )
            for row in rows[:MAX_SELECT_OPTIONS]
        ]
        select = discord.ui.Select(
            placeholder="Cancel a reminder", options=options, max_values=len(options)
        )
        select.callback = self._cancel
        self.add_item(select)
        self._select = select

    async def _cancel(self, interaction: discord.Interaction) -> None:
        cancelled = await self.cog.cancel_reminders(
            interaction.user.id, [int(v) for v in self._select.values]
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"{EMOJI_SUCCESS}  Cancelled **{cancelled}** reminder(s).",
                colour=COLOUR_SUCCESS,
            ),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------


class Utility(commands.Cog):
    """Server info, member info, polls, reminders, starboard, help."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        #: Last time each member spoke in each channel, for highlight suppression.
        #: Bounded by activity and cleared on restart; never persisted.
        self._last_seen: dict[tuple[int, int], float] = {}
        #: Every ``(user_id, word)`` pair, cached. The highlight listener runs on
        #: every message in the server, and a query per message is exactly the
        #: kind of thing the CPU budget has no room for. ``None`` means unloaded.
        self._highlight_cache: list[tuple[int, str]] | None = None

    async def cog_load(self) -> None:
        self.bot.scheduler.register("reminder", self._fire_reminder)
        self.bot.scheduler.register("poll_end", self._end_poll)

    async def cog_unload(self) -> None:
        for action in ("reminder", "poll_end"):
            self.bot.scheduler.unregister(action)

    def register_persistent_views(self, bot: Any) -> None:
        """Re-attach poll and suggestion buttons posted before the restart."""
        bot.add_dynamic_items(PollButton, SuggestionButton)

    @staticmethod
    def _ok(description: str) -> discord.Embed:
        return discord.Embed(
            description=f"{EMOJI_SUCCESS}  {description}", colour=COLOUR_SUCCESS
        )

    @staticmethod
    async def _defer(ctx: commands.Context) -> None:
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            await ctx.defer()

    # =======================================================================
    # Information
    # =======================================================================

    @commands.hybrid_command(name="serverinfo", aliases=["guildinfo", "si"])
    @permissions.guild_only()
    async def serverinfo(self, ctx: commands.Context) -> None:
        """Show creation date, owner, counts, boost tier, features and icon."""
        guild = ctx.guild
        created = int(guild.created_at.timestamp())

        embed = discord.Embed(
            title=guild.name,
            description=guild.description or None,
            colour=COLOUR_INFO,
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.add_field(name="ID", value=f"`{guild.id}`", inline=True)
        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
        embed.add_field(
            name="Created",
            value=f"<t:{created}:{TS_LONG_DATE_TIME}>\n(<t:{created}:{TS_RELATIVE}>)",
            inline=True,
        )
        embed.add_field(
            name="Members", value=f"{guild.member_count or '?'}", inline=True
        )
        embed.add_field(
            name="Channels",
            value=f"{len(guild.text_channels)} text, {len(guild.voice_channels)} voice, "
            f"{len(guild.categories)} categories",
            inline=True,
        )
        embed.add_field(name="Roles", value=str(len(guild.roles) - 1), inline=True)
        embed.add_field(
            name="Boosts",
            value=f"Tier {guild.premium_tier} — {guild.premium_subscription_count or 0} boost(s)",
            inline=True,
        )
        embed.add_field(
            name="Verification", value=str(guild.verification_level).title(), inline=True
        )
        embed.add_field(
            name="Emoji / stickers",
            value=f"{len(guild.emojis)} / {len(guild.stickers)}",
            inline=True,
        )
        if guild.features:
            embed.add_field(
                name="Features",
                value=", ".join(f.replace("_", " ").title() for f in guild.features)[:1024],
                inline=False,
            )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="userinfo", aliases=["whois", "ui"])
    @app_commands.describe(target="Whose info to show. Defaults to you")
    @permissions.guild_only()
    async def userinfo(
        self, ctx: commands.Context, target: Optional[discord.Member] = None
    ) -> None:
        """Show join date, account age, roles, permissions and moderation summary."""
        member = target or ctx.author
        created = int(member.created_at.timestamp())
        joined = int(member.joined_at.timestamp()) if member.joined_at else None

        embed = discord.Embed(
            title=str(member),
            colour=member.colour.value or COLOUR_INFO,
            description=member.mention,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
        embed.add_field(name="Bot", value="Yes" if member.bot else "No", inline=True)
        embed.add_field(
            name="Status", value=str(member.status).title(), inline=True
        )
        embed.add_field(
            name="Account created",
            value=f"<t:{created}:{TS_LONG_DATE_TIME}>\n(<t:{created}:{TS_RELATIVE}>)",
            inline=True,
        )
        if joined:
            embed.add_field(
                name="Joined server",
                value=f"<t:{joined}:{TS_LONG_DATE_TIME}>\n(<t:{joined}:{TS_RELATIVE}>)",
                inline=True,
            )
        if member.premium_since:
            embed.add_field(
                name="Boosting since",
                value=f"<t:{int(member.premium_since.timestamp())}:{TS_RELATIVE}>",
                inline=True,
            )

        roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
        embed.add_field(
            name=f"Roles ({len(roles)})",
            value=", ".join(roles)[:1024] if roles else "None",
            inline=False,
        )

        key = [
            name.replace("_", " ").title()
            for name in ("administrator", "manage_guild", "manage_roles",
                         "manage_channels", "ban_members", "kick_members",
                         "manage_messages", "moderate_members", "mention_everyone")
            if getattr(member.guild_permissions, name)
        ]
        embed.add_field(
            name="Key permissions", value=", ".join(key) or "None", inline=False
        )

        tier = await permissions.tier_of(self.bot, member, ctx.guild)
        embed.add_field(name="Deezee tier", value=tier.label, inline=True)

        if await permissions.has_tier(
            self.bot, ctx.author, ctx.guild, permissions.Tier.MOD
        ):
            warnings = await self.bot.db.fetchval(
                "SELECT COUNT(*) FROM warnings WHERE guild_id = ? AND user_id = ? "
                "AND active = 1",
                (ctx.guild.id, member.id),
            )
            cases = await self.bot.db.fetchval(
                "SELECT COUNT(*) FROM mod_cases WHERE guild_id = ? AND target_id = ?",
                (ctx.guild.id, member.id),
            )
            embed.add_field(
                name="Moderation",
                value=f"**{warnings or 0}** active warning(s) • **{cases or 0}** case(s)\n"
                f"`?warnings {member.id}` • `?modlogs {member.id}`",
                inline=False,
            )
        if member.timed_out_until:
            embed.add_field(
                name="Timed out until",
                value=f"<t:{int(member.timed_out_until.timestamp())}:{TS_RELATIVE}>",
                inline=False,
            )

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="avatar", aliases=["av", "pfp"])
    @app_commands.describe(target="Whose avatar. Defaults to you")
    @permissions.guild_only()
    async def avatar(
        self, ctx: commands.Context, target: Optional[discord.Member] = None
    ) -> None:
        """Show an avatar, with server-specific avatar and format links."""
        member = target or ctx.author
        asset = member.display_avatar

        links = [
            f"[png]({asset.replace(format='png', size=1024).url})",
            f"[jpg]({asset.replace(format='jpg', size=1024).url})",
            f"[webp]({asset.replace(format='webp', size=1024).url})",
        ]
        if asset.is_animated():
            links.append(f"[gif]({asset.replace(format='gif', size=1024).url})")

        embed = discord.Embed(
            title=f"{member.display_name}'s avatar",
            description=" • ".join(links),
            colour=member.colour.value or COLOUR_INFO,
        )
        embed.set_image(url=asset.replace(size=1024).url)
        if member.guild_avatar is not None:
            embed.add_field(
                name="Note",
                value="This is their **server** avatar. "
                f"[Global one]({member.avatar.url if member.avatar else ''})",
                inline=False,
            )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="banner", with_app_command=False)
    @app_commands.describe(target="Whose banner. Blank shows the server banner")
    @permissions.guild_only()
    async def banner(
        self, ctx: commands.Context, target: Optional[discord.Member] = None
    ) -> None:
        """Show a member's or the server's banner."""
        if target is None:
            if ctx.guild.banner is None:
                raise DeezeeError("This server has no banner.")
            embed = discord.Embed(title=f"{ctx.guild.name} banner", colour=COLOUR_INFO)
            embed.set_image(url=ctx.guild.banner.replace(size=1024).url)
            await ctx.send(embed=embed)
            return

        # A member object never carries the banner; only a fetched user does.
        user = await self.bot.fetch_user(target.id)
        if user.banner is None:
            raise DeezeeError(
                f"{target.display_name} has no banner. "
                "Banners are a Nitro feature, so most members will not have one."
            )
        embed = discord.Embed(title=f"{user}'s banner", colour=COLOUR_INFO)
        embed.set_image(url=user.banner.replace(size=1024).url)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="channelinfo", with_app_command=False)
    @app_commands.describe(channel="Which channel. Defaults to here")
    @permissions.guild_only()
    async def channelinfo(
        self, ctx: commands.Context, channel: Optional[discord.abc.GuildChannel] = None
    ) -> None:
        """Show a channel's ID, type, topic, slowmode, category and creation date."""
        target = channel or ctx.channel
        created = int(target.created_at.timestamp())

        embed = discord.Embed(
            title=f"#{target.name}",
            description=getattr(target, "topic", None) or None,
            colour=COLOUR_INFO,
        )
        embed.add_field(name="ID", value=f"`{target.id}`", inline=True)
        embed.add_field(name="Type", value=str(target.type), inline=True)
        embed.add_field(
            name="Category",
            value=target.category.name if target.category else "None",
            inline=True,
        )
        embed.add_field(
            name="Created",
            value=f"<t:{created}:{TS_LONG_DATE_TIME}>\n(<t:{created}:{TS_RELATIVE}>)",
            inline=True,
        )
        embed.add_field(name="Position", value=str(target.position), inline=True)
        if hasattr(target, "slowmode_delay"):
            embed.add_field(
                name="Slowmode",
                value=format_duration(target.slowmode_delay)
                if target.slowmode_delay
                else "Off",
                inline=True,
            )
        if hasattr(target, "nsfw"):
            embed.add_field(
                name="Age-restricted", value="Yes" if target.nsfw else "No", inline=True
            )
        if isinstance(target, discord.VoiceChannel):
            embed.add_field(name="Bitrate", value=f"{target.bitrate // 1000} kbps",
                            inline=True)
            embed.add_field(
                name="User limit", value=str(target.user_limit or "unlimited"),
                inline=True,
            )
        embed.add_field(
            name="Permission overwrites", value=str(len(target.overwrites)), inline=True
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="membercount", aliases=["mc"])
    @permissions.guild_only()
    async def membercount(self, ctx: commands.Context) -> None:
        """Show member, human, bot and online counts."""
        guild = ctx.guild
        total = guild.member_count or 0

        # Human/bot and online counts come from the cache, which is not the whole
        # member list -- startup chunking is off. Say so rather than presenting a
        # partial count as the truth.
        cached = guild.members
        bots = sum(1 for m in cached if m.bot)
        online = sum(1 for m in cached if m.status is not discord.Status.offline)

        embed = discord.Embed(title=f"{guild.name} — members", colour=COLOUR_INFO)
        embed.add_field(name="Total", value=f"**{total}**", inline=True)
        embed.add_field(
            name="Cached", value=f"{len(cached)} ({len(cached) - bots} human, {bots} bots)",
            inline=True,
        )
        embed.add_field(name="Online (of cached)", value=str(online), inline=True)
        embed.set_footer(
            text="Total is exact. The breakdown counts members I have seen since "
            "starting -- I do not download the whole member list, to stay inside "
            "the memory budget."
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="ping")
    async def ping(self, ctx: commands.Context) -> None:
        """Show gateway and API round-trip latency, plus database query time."""
        gateway = self.bot.latency * 1000

        started = time.perf_counter()
        await self.bot.db.fetchval("SELECT 1")
        db_ms = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        message = await ctx.send(
            embed=discord.Embed(description="Measuring…", colour=COLOUR_MUTED)
        )
        api_ms = (time.perf_counter() - started) * 1000

        embed = discord.Embed(title="Pong", colour=COLOUR_SUCCESS)
        embed.add_field(name="Gateway", value=f"{gateway:.0f} ms", inline=True)
        embed.add_field(name="API round trip", value=f"{api_ms:.0f} ms", inline=True)
        embed.add_field(name="Database", value=f"{db_ms:.2f} ms", inline=True)

        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            await ctx.send(embed=embed)

    @commands.hybrid_command(name="botinfo", aliases=["about", "stats", "uptime"])
    async def botinfo(self, ctx: commands.Context) -> None:
        """Show uptime, versions, memory use and command counts."""
        commands_total = len(list(self.bot.walk_commands()))
        memory = _memory_mib()

        cases = await self.bot.db.fetchval("SELECT COUNT(*) FROM mod_cases") or 0
        db_bytes = 0
        try:
            db_bytes = self.bot.db.path.stat().st_size
        except OSError:
            pass

        embed = discord.Embed(
            title="Deezee Server Bot",
            description="One bot replacing Dyno, Carl-bot, Lawliet, Sapphire and "
            "Double Counter. Every setting is configured inside Discord -- there "
            "is no web dashboard.",
            colour=COLOUR_INFO,
        )
        if self.bot.user and self.bot.user.avatar:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)

        embed.add_field(
            name="Uptime", value=format_duration(int(self.bot.uptime)), inline=True
        )
        embed.add_field(
            name="Started",
            value=f"<t:{int(self.bot.started_at)}:{TS_RELATIVE}>", inline=True,
        )
        embed.add_field(name="Guilds", value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(name="Commands", value=str(commands_total), inline=True)
        embed.add_field(name="Cogs", value=str(len(self.bot.cogs)), inline=True)
        embed.add_field(
            name="Cached members", value=str(sum(len(g.members) for g in self.bot.guilds)),
            inline=True,
        )
        embed.add_field(
            name="Memory",
            value=f"{memory:.0f} MiB of 512 budget" if memory else "unavailable",
            inline=True,
        )
        embed.add_field(
            name="Database", value=f"{db_bytes / 1024 / 1024:.1f} MiB • {cases} case(s)",
            inline=True,
        )
        embed.add_field(
            name="Versions",
            value=f"discord.py {discord.__version__}\nPython {platform.python_version()}",
            inline=True,
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="help", aliases=["h", "commands"])
    @app_commands.describe(command="Optional: a specific command to explain")
    async def help_command(
        self, ctx: commands.Context, *, command: Optional[str] = None
    ) -> None:
        """Interactive help: category menu, per-command detail, search."""
        prefix = (
            await self.bot.guild_config.prefix(
                ctx.guild.id, self.bot.config.default_prefix
            )
            if ctx.guild
            else self.bot.config.default_prefix
        )

        if command:
            found = self.bot.get_command(command.strip().lstrip(prefix))
            if found is None:
                raise DeezeeError(
                    f"There is no command called `{command}`. "
                    f"Run `{prefix}help` and press Search."
                )
            await ctx.send(embed=self._command_embed(found, prefix))
            return

        categories = await self._visible_categories(ctx)
        view = HelpView(ctx.author, categories, prefix)
        view.message = await ctx.send(embed=view.embed(), view=view)

    async def _visible_categories(
        self, ctx: commands.Context
    ) -> dict[str, list[commands.Command]]:
        """Group runnable commands by cog.

        Every command is check-tested against the invoker. A help page that lists
        commands answering "you may not use that" is worse than no help page.
        """
        categories: dict[str, list[commands.Command]] = {}
        for command in sorted(
            self.bot.walk_commands(), key=lambda c: c.qualified_name
        ):
            if command.hidden:
                continue
            try:
                if not await command.can_run(ctx):
                    continue
            except commands.CommandError:
                continue
            name = command.cog_name or "Other"
            categories.setdefault(name, []).append(command)
        return categories

    @staticmethod
    def _command_embed(command: commands.Command, prefix: str) -> discord.Embed:
        """Detail page for one command."""
        embed = discord.Embed(
            title=f"{prefix}{command.qualified_name}",
            description=(command.help or command.short_doc or "No description.").strip(),
            colour=COLOUR_DEFAULT,
        )
        embed.add_field(
            name="Usage",
            value=f"`{prefix}{command.qualified_name} {command.signature}`".strip(),
            inline=False,
        )
        if command.aliases:
            embed.add_field(
                name="Aliases",
                value=", ".join(f"`{a}`" for a in command.aliases),
                inline=True,
            )
        if command.cog_name:
            embed.add_field(name="Category", value=command.cog_name, inline=True)
        if isinstance(command, commands.Group):
            subs = sorted(c.name for c in command.commands)
            if subs:
                embed.add_field(
                    name="Subcommands",
                    value=", ".join(f"`{s}`" for s in subs)[:1024],
                    inline=False,
                )
        embed.set_footer(text="Angle brackets are required, square brackets optional.")
        return embed

    # =======================================================================
    # Emoji
    # =======================================================================

    @commands.hybrid_command(name="emojis", with_app_command=False, aliases=["emotes"])
    @app_commands.describe(search="Optional: filter by name")
    @permissions.guild_only()
    async def emojis(
        self, ctx: commands.Context, *, search: Optional[str] = None
    ) -> None:
        """List the server's custom emoji with IDs."""
        found = ctx.guild.emojis
        if search:
            needle = search.lower()
            found = [e for e in found if needle in e.name.lower()]

        lines = [
            f"{emoji} `:{emoji.name}:` — `{emoji.id}`"
            + ("  *(animated)*" if emoji.animated else "")
            for emoji in sorted(found, key=lambda e: e.name.lower())
        ]
        pages = paginate_lines(
            lines,
            title=f"Emoji in {ctx.guild.name}",
            per_page=15,
            description=f"{len(lines)} shown of {len(ctx.guild.emojis)} total.",
            empty_message="No custom emoji match.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @commands.hybrid_command(name="addemoji", with_app_command=False, aliases=["steal", "addemote"])
    @app_commands.describe(
        name="Name for the new emoji",
        source="An image URL, or an emoji from another server. Or attach a file",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def addemoji(
        self, ctx: commands.Context, name: str, *, source: Optional[str] = None
    ) -> None:
        """Add an emoji from an image, a URL, or by copying one from a message."""
        if not ctx.guild.me.guild_permissions.manage_expressions:
            raise commands.BotMissingPermissions(["manage_expressions"])

        clean = re.sub(r"[^A-Za-z0-9_]", "", name)[:32]
        if len(clean) < 2:
            raise DeezeeError(
                "An emoji name must be at least 2 characters and may only contain "
                "letters, numbers and underscores."
            )

        await self._defer(ctx)
        url: str | None = None

        if source:
            match = _CUSTOM_EMOJI.search(source)
            if match:
                extension = "gif" if match.group(1) else "png"
                url = f"https://cdn.discordapp.com/emojis/{match.group(3)}.{extension}"
            elif source.lower().startswith("https://"):
                url = source.strip()
            else:
                raise DeezeeError(
                    "Give an `https://` image URL, paste a custom emoji, or attach "
                    "an image file."
                )

        data: bytes
        if url is None:
            attachments = ctx.message.attachments if ctx.message else []
            if not attachments:
                raise DeezeeError(
                    "Attach an image, paste a custom emoji, or give an image URL."
                )
            if attachments[0].size > MAX_EMOJI_BYTES:
                raise DeezeeError(
                    f"That file is {attachments[0].size // 1024} KiB. "
                    f"Discord's limit is {MAX_EMOJI_BYTES // 1024} KiB."
                )
            data = await attachments[0].read()
        else:
            data = await self._download(url, MAX_EMOJI_BYTES)

        try:
            emoji = await ctx.guild.create_custom_emoji(
                name=clean, image=data, reason=f"{ctx.author} via ?addemoji"
            )
        except discord.HTTPException as exc:
            raise DeezeeError(
                f"Discord refused that emoji: {exc.text or exc}. "
                "The most common causes are a full emoji slot list and a file "
                "above 256 KiB."
            ) from exc

        await ctx.send(embed=self._ok(f"Added {emoji} as `:{emoji.name}:`."))

    @commands.hybrid_command(name="delemoji", with_app_command=False, aliases=["delemote"])
    @app_commands.describe(emoji="The emoji, its name, or its ID")
    @permissions.admin_only()
    @permissions.guild_only()
    async def delemoji(self, ctx: commands.Context, *, emoji: str) -> None:
        """Delete a custom emoji."""
        if not ctx.guild.me.guild_permissions.manage_expressions:
            raise commands.BotMissingPermissions(["manage_expressions"])

        match = _CUSTOM_EMOJI.search(emoji)
        needle = match.group(3) if match else emoji.strip().strip(":").lower()

        target = discord.utils.find(
            lambda e: str(e.id) == needle or e.name.lower() == needle, ctx.guild.emojis
        )
        if target is None:
            raise DeezeeError(f"No emoji in this server matches `{emoji}`.")

        name = target.name
        await target.delete(reason=f"{ctx.author} via ?delemoji")
        await ctx.send(embed=self._ok(f"Deleted `:{name}:`."))

    @staticmethod
    async def _download(url: str, limit: int) -> bytes:
        """Fetch a URL, refusing anything above ``limit`` bytes.

        The size is checked against the header *and* against what actually
        arrives: a Content-Length can lie, and streaming an unbounded body into
        memory is how a 512 MiB budget disappears.
        """
        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise DeezeeError(
                            f"That URL answered HTTP {response.status}."
                        )
                    declared = response.content_length
                    if declared and declared > limit:
                        raise DeezeeError(
                            f"That file is {declared // 1024} KiB; the limit is "
                            f"{limit // 1024} KiB."
                        )
                    data = await response.content.read(limit + 1)
        except aiohttp.ClientError as exc:
            raise DeezeeError(f"Could not fetch that URL: {exc}") from exc

        if len(data) > limit:
            raise DeezeeError(f"That file is above the {limit // 1024} KiB limit.")
        return data

    # =======================================================================
    # Posting
    # =======================================================================

    @commands.hybrid_group(name="embed", with_app_command=False, fallback="create")
    @app_commands.describe(channel="Where to post. Defaults to here")
    @permissions.mod_only()
    @permissions.guild_only()
    async def embed_group(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Open the embed builder and post the result."""
        target = channel or ctx.channel
        perms = target.permissions_for(ctx.guild.me)
        if not perms.send_messages or not perms.embed_links:
            raise DeezeeError(f"I need Send Messages and Embed Links in {target.mention}.")

        view = EmbedBuilder(self, ctx.author, target)
        view.message = await ctx.send(
            content=view.status(), embed=view.embed, view=view
        )

    @embed_group.command(name="edit")
    @app_commands.describe(message="Link or ID of one of my messages")
    @permissions.mod_only()
    @permissions.guild_only()
    async def embed_edit(self, ctx: commands.Context, message: str) -> None:
        """Reopen the builder on an embed I already posted."""
        target = await self._resolve_message(ctx, message)
        if target.author.id != self.bot.user.id:
            raise DeezeeError("I can only edit my own messages.")
        if not target.embeds:
            raise DeezeeError("That message has no embed.")

        view = EmbedBuilder(
            self, ctx.author, target.channel, embed=target.embeds[0], target=target
        )
        view.message = await ctx.send(
            content=view.status(), embed=view.embed, view=view
        )

    @embed_group.command(name="source")
    @app_commands.describe(message="Link or ID of a message with an embed")
    @permissions.mod_only()
    @permissions.guild_only()
    async def embed_source(self, ctx: commands.Context, message: str) -> None:
        """Show an embed's JSON, for copying."""
        target = await self._resolve_message(ctx, message)
        if not target.embeds:
            raise DeezeeError("That message has no embed.")

        payload = json.dumps(target.embeds[0].to_dict(), indent=2, ensure_ascii=False)
        if len(payload) > 3800:
            await ctx.send(
                content="The embed source is too long to show inline.",
                file=discord.File(
                    io.BytesIO(payload.encode("utf-8")), filename="embed.json"
                ),
            )
            return
        pages = paginate_text(payload, title="Embed source", code_block="json")
        await Paginator(pages, ctx.author).start(ctx)

    @embed_group.command(name="json")
    @app_commands.describe(
        channel="Where to post", payload="A Discord embed object as JSON"
    )
    @permissions.mod_only()
    @permissions.guild_only()
    async def embed_json(
        self, ctx: commands.Context, channel: discord.TextChannel, *, payload: str
    ) -> None:
        """Post an embed from raw JSON."""
        text = payload.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DeezeeError(f"That is not valid JSON: {exc.msg} at position {exc.pos}.")
        if not isinstance(data, dict):
            raise DeezeeError("The JSON must be a single embed object.")

        try:
            embed = discord.Embed.from_dict(data)
            posted = await channel.send(embed=embed)
        except discord.HTTPException as exc:
            raise DeezeeError(f"Discord refused that embed: {exc.text or exc}") from exc

        await ctx.send(embed=self._ok(f"Posted: {posted.jump_url}"))

    async def _resolve_message(
        self, ctx: commands.Context, reference: str
    ) -> discord.Message:
        """Turn a message link or ID into a message."""
        try:
            return await commands.MessageConverter().convert(ctx, reference.strip())
        except commands.BadArgument as exc:
            raise DeezeeError(
                f"I could not find that message. Paste its link, or its ID if it is "
                f"in this channel. ({exc})"
            ) from exc

    @commands.hybrid_command(name="say", aliases=["echo"])
    @app_commands.describe(channel="Where to post", message="What to say")
    @permissions.mod_only()
    @permissions.guild_only()
    async def say(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        *,
        message: str,
    ) -> None:
        """Post a plain message as the bot.

        Mentions are stripped unless the invoker has Mention Everyone, so this
        cannot be used to borrow the bot's permissions for a mass ping.
        """
        target = channel or ctx.channel
        perms = target.permissions_for(ctx.guild.me)
        if not perms.send_messages:
            raise DeezeeError(f"I cannot post in {target.mention}.")

        may_ping = ctx.author.guild_permissions.mention_everyone
        allowed = discord.AllowedMentions(
            everyone=may_ping, roles=may_ping, users=True
        )

        posted = await target.send(message[:2000], allowed_mentions=allowed)
        if target.id != ctx.channel.id:
            await ctx.send(embed=self._ok(f"Posted: {posted.jump_url}"), ephemeral=True)
        elif ctx.interaction is not None:
            await ctx.send(embed=self._ok("Posted."), ephemeral=True)
        else:
            try:
                await ctx.message.delete()
            except (discord.Forbidden, discord.NotFound):
                pass

    @commands.hybrid_command(name="announce", with_app_command=False)
    @app_commands.describe(
        channel="Where to announce",
        message="The announcement",
        mention="Role to ping. Must be the configured announcement role",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def announce(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        mention: Optional[discord.Role] = None,
        *,
        message: str,
    ) -> None:
        """Post an announcement, optionally pinging the configured role."""
        perms = channel.permissions_for(ctx.guild.me)
        if not perms.send_messages or not perms.embed_links:
            raise DeezeeError(f"I need Send Messages and Embed Links in {channel.mention}.")

        content = None
        allowed = discord.AllowedMentions.none()

        if mention is not None:
            configured = await self.bot.guild_config.value(
                ctx.guild.id, "announcement_role_id"
            )
            may = ctx.author.guild_permissions.mention_everyone or (
                configured and int(configured) == mention.id
            )
            if not may:
                raise DeezeeError(
                    f"{mention.mention} is not the configured announcement role and "
                    "you do not have Mention Everyone. Set the announcement role in "
                    "`?config` → Server."
                )
            content = mention.mention
            allowed = discord.AllowedMentions(roles=[mention])

        embed = discord.Embed(
            title="Announcement", description=message[:4000], colour=COLOUR_WARNING
        )
        embed.set_footer(text=f"Posted by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        embed.timestamp = discord.utils.utcnow()

        posted = await channel.send(content=content, embed=embed, allowed_mentions=allowed)
        await ctx.send(embed=self._ok(f"Announced: {posted.jump_url}"))

    # =======================================================================
    # Polls
    # =======================================================================

    @commands.hybrid_command(name="poll")
    @app_commands.describe(
        question="What is being asked",
        options="Choices separated by | -- e.g. Yes | No | Maybe",
        duration="Optional: close automatically after this, e.g. 1h",
        multi="Allow voting for more than one option",
    )
    @permissions.mod_only()
    @permissions.guild_only()
    async def poll(
        self,
        ctx: commands.Context,
        question: str,
        options: str,
        duration: Optional[str] = None,
        multi: bool = False,
    ) -> None:
        """Create a button poll with live vote counts.

        Votes are stored per voter, so double-voting is impossible and the poll
        survives a restart with its counts intact.
        """
        labels = [part.strip() for part in options.split("|") if part.strip()]
        if len(labels) < 2:
            raise DeezeeError(
                "A poll needs at least two options, separated by `|`. "
                "Example: `Yes | No | Maybe`."
            )
        if len(labels) > MAX_POLL_OPTIONS:
            raise DeezeeError(f"A poll holds at most {MAX_POLL_OPTIONS} options.")

        ends_at: int | None = None
        if duration:
            delta = parse_duration(duration)
            if delta is None:
                raise DeezeeError(f"`{duration}` is not a duration. Try `1h` or `2d`.")
            ends_at = int(time.time() + delta.total_seconds())

        cursor = await self.bot.db.execute(
            "INSERT INTO polls (guild_id, channel_id, question, options, multi, "
            "ends_at, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ctx.guild.id, ctx.channel.id, question[:400], json.dumps(labels),
                int(multi), ends_at, ctx.author.id, int(time.time()),
            ),
        )
        poll_id = int(cursor.lastrowid or 0)

        view = discord.ui.View(timeout=None)
        for index, label in enumerate(labels):
            view.add_item(PollButton(poll_id, index, label))

        message = await ctx.channel.send(
            embed=await self.poll_embed(poll_id), view=view
        )
        await self.bot.db.execute(
            "UPDATE polls SET message_id = ? WHERE id = ?", (message.id, poll_id)
        )

        if ends_at:
            await self.bot.scheduler.schedule(
                ctx.guild.id, "poll_end", ends_at, {"poll_id": poll_id}
            )

        if ctx.interaction is not None:
            await ctx.send(embed=self._ok(f"Poll posted: {message.jump_url}"),
                           ephemeral=True)

    async def poll_embed(self, poll_id: int) -> discord.Embed:
        """Render a poll with its current counts."""
        row = await self.bot.db.fetchone("SELECT * FROM polls WHERE id = ?", (poll_id,))
        if row is None:
            return discord.Embed(description="This poll no longer exists.",
                                 colour=COLOUR_ERROR)

        labels = json.loads(row["options"])
        counts = {index: 0 for index in range(len(labels))}
        for vote in await self.bot.db.fetchall(
            "SELECT option_index, COUNT(*) AS total FROM poll_votes "
            "WHERE poll_id = ? GROUP BY option_index",
            (poll_id,),
        ):
            counts[vote["option_index"]] = vote["total"]

        voters = int(
            await self.bot.db.fetchval(
                "SELECT COUNT(DISTINCT user_id) FROM poll_votes WHERE poll_id = ?",
                (poll_id,),
            )
            or 0
        )
        total = sum(counts.values()) or 1

        lines = []
        for index, label in enumerate(labels):
            count = counts[index]
            share = count / total
            filled = int(20 * share)
            face = POLL_FACES[index] if index < len(POLL_FACES) else "•"
            lines.append(
                f"{face} **{label}**\n"
                f"`{'█' * filled}{'░' * (20 - filled)}` {count} ({share * 100:.0f}%)"
            )

        embed = discord.Embed(
            title=row["question"],
            description="\n".join(lines),
            colour=COLOUR_MUTED if row["closed"] else COLOUR_INFO,
        )
        embed.set_footer(
            text=f"Poll #{poll_id} • {voters} voter(s)"
            + (" • multiple choice" if row["multi"] else "")
            + (" • CLOSED" if row["closed"] else "")
        )
        if row["ends_at"] and not row["closed"]:
            embed.add_field(
                name="Closes",
                value=f"<t:{row['ends_at']}:{TS_RELATIVE}>",
                inline=False,
            )
        return embed

    async def record_vote(
        self, interaction: discord.Interaction, poll_id: int, index: int
    ) -> None:
        """Register or withdraw one vote, then redraw the poll."""
        row = await self.bot.db.fetchone("SELECT * FROM polls WHERE id = ?", (poll_id,))
        if row is None:
            await interaction.response.send_message(
                "That poll no longer exists.", ephemeral=True
            )
            return
        if row["closed"]:
            await interaction.response.send_message(
                "That poll is closed.", ephemeral=True
            )
            return

        user_id = interaction.user.id
        existing = await self.bot.db.fetchone(
            "SELECT 1 FROM poll_votes WHERE poll_id = ? AND user_id = ? "
            "AND option_index = ?",
            (poll_id, user_id, index),
        )

        if existing:
            await self.bot.db.execute(
                "DELETE FROM poll_votes WHERE poll_id = ? AND user_id = ? "
                "AND option_index = ?",
                (poll_id, user_id, index),
            )
            note = "Vote withdrawn."
        else:
            if not row["multi"]:
                # Single-choice: replacing is what a voter means by pressing a
                # different button, not adding a second vote.
                await self.bot.db.execute(
                    "DELETE FROM poll_votes WHERE poll_id = ? AND user_id = ?",
                    (poll_id, user_id),
                )
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO poll_votes (poll_id, user_id, option_index) "
                "VALUES (?, ?, ?)",
                (poll_id, user_id, index),
            )
            note = "Vote recorded."

        await interaction.response.send_message(note, ephemeral=True)
        try:
            await interaction.message.edit(embed=await self.poll_embed(poll_id))
        except discord.HTTPException:
            pass

    async def _end_poll(self, guild_id: int, payload: dict[str, Any]) -> None:
        """Close a poll when its timer comes due."""
        poll_id = int(payload.get("poll_id", 0))
        row = await self.bot.db.fetchone("SELECT * FROM polls WHERE id = ?", (poll_id,))
        if row is None or row["closed"]:
            return

        await self.bot.db.execute("UPDATE polls SET closed = 1 WHERE id = ?", (poll_id,))

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(row["channel_id"])
        if not isinstance(channel, discord.TextChannel) or not row["message_id"]:
            return
        try:
            message = await channel.fetch_message(row["message_id"])
            await message.edit(embed=await self.poll_embed(poll_id), view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    # =======================================================================
    # Reminders
    # =======================================================================

    @commands.hybrid_command(name="remind", aliases=["remindme", "timer"])
    @app_commands.describe(duration="When, e.g. 30m, 2h, 3d", text="What to remind you of")
    async def remind(
        self, ctx: commands.Context, duration: str, *, text: str
    ) -> None:
        """Set a personal reminder."""
        delta = parse_duration(duration)
        if delta is None:
            raise DeezeeError(f"`{duration}` is not a duration. Try `30m` or `2h`.")
        if delta > timedelta(days=365):
            raise DeezeeError("A reminder more than a year out is almost never meant.")

        pending = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM reminders WHERE user_id = ? AND completed_at IS NULL "
            "AND cancelled = 0",
            (ctx.author.id,),
        )
        if (pending or 0) >= 25:
            raise DeezeeError(
                "You already have 25 pending reminders. Cancel one with `?reminders`."
            )

        remind_at = int(time.time() + delta.total_seconds())
        jump = ctx.message.jump_url if ctx.message else ""

        cursor = await self.bot.db.execute(
            "INSERT INTO reminders (user_id, guild_id, channel_id, jump_url, text, "
            "remind_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ctx.author.id,
                ctx.guild.id if ctx.guild else None,
                ctx.channel.id,
                jump,
                text[:1000],
                remind_at,
                int(time.time()),
            ),
        )
        reminder_id = int(cursor.lastrowid or 0)

        await self.bot.scheduler.schedule(
            ctx.guild.id if ctx.guild else 0,
            "reminder",
            remind_at,
            {"reminder_id": reminder_id},
        )

        await ctx.send(
            embed=self._ok(
                f"Reminder **#{reminder_id}** set for "
                f"<t:{remind_at}:{TS_LONG_DATE_TIME}> "
                f"(<t:{remind_at}:{TS_RELATIVE}>).\nCancel it with `?reminders`."
            )
        )

    @commands.hybrid_command(name="reminders")
    async def reminders(self, ctx: commands.Context) -> None:
        """List and cancel your pending reminders."""
        rows = await self.bot.db.fetchall(
            "SELECT * FROM reminders WHERE user_id = ? AND completed_at IS NULL "
            "AND cancelled = 0 ORDER BY remind_at",
            (ctx.author.id,),
        )
        if not rows:
            await ctx.send(
                embed=discord.Embed(
                    description="You have no pending reminders.", colour=COLOUR_DEFAULT
                )
            )
            return

        lines = [
            f"**#{row['id']}** <t:{row['remind_at']}:{TS_RELATIVE}> — {row['text'][:120]}"
            for row in rows
        ]
        embed = discord.Embed(
            title=f"Your reminders ({len(rows)})",
            description="\n".join(lines[:20]),
            colour=COLOUR_INFO,
        )
        view = ReminderCancelView(self, ctx.author, [dict(r) for r in rows])
        view.message = await ctx.send(embed=embed, view=view, ephemeral=True)

    async def cancel_reminders(self, user_id: int, ids: list[int]) -> int:
        """Cancel reminders and their scheduler rows. Returns how many."""
        if not ids:
            return 0
        placeholders = ", ".join("?" * len(ids))
        cursor = await self.bot.db.execute(
            f"UPDATE reminders SET cancelled = 1, completed_at = ? "
            f"WHERE user_id = ? AND id IN ({placeholders}) AND completed_at IS NULL",
            (int(time.time()), user_id, *ids),
        )
        for reminder_id in ids:
            # Scheduler rows are keyed by guild, and a reminder can be set in a
            # DM, so every guild the bot is in is swept for the payload.
            for guild in self.bot.guilds:
                await self.bot.scheduler.cancel_matching(
                    guild.id, "reminder", reminder_id=reminder_id
                )
            await self.bot.scheduler.cancel_matching(
                0, "reminder", reminder_id=reminder_id
            )
        return cursor.rowcount

    async def _fire_reminder(self, guild_id: int, payload: dict[str, Any]) -> None:
        """Deliver a reminder, preferring the channel and falling back to a DM."""
        reminder_id = int(payload.get("reminder_id", 0))
        row = await self.bot.db.fetchone(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        )
        if row is None or row["cancelled"] or row["completed_at"]:
            return

        user = self.bot.get_user(row["user_id"])
        if user is None:
            try:
                user = await self.bot.fetch_user(row["user_id"])
            except discord.HTTPException:
                user = None

        embed = discord.Embed(
            title="Reminder",
            description=row["text"],
            colour=COLOUR_INFO,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(
            text=f"Set {format_duration(int(time.time()) - row['created_at'])} ago"
        )
        if row["jump_url"]:
            embed.add_field(
                name="Where you set it",
                value=f"[Jump]({row['jump_url']})",
                inline=False,
            )

        delivered = False
        channel = self.bot.get_channel(row["channel_id"]) if row["channel_id"] else None
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(content=f"<@{row['user_id']}>", embed=embed)
                delivered = True
            except discord.HTTPException:
                delivered = False

        if not delivered and user is not None:
            try:
                await user.send(embed=embed)
                delivered = True
            except discord.HTTPException:
                log.info("Could not deliver reminder %s to %s", reminder_id,
                         row["user_id"])

        await self.bot.db.execute(
            "UPDATE reminders SET completed_at = ? WHERE id = ?",
            (int(time.time()), reminder_id),
        )

    # =======================================================================
    # Highlights and AFK
    # =======================================================================

    @commands.hybrid_group(name="highlight", aliases=["hl"], fallback="list")
    async def highlight(self, ctx: commands.Context) -> None:
        """Words that DM you when someone says them."""
        words = await self.bot.db.fetchall(
            "SELECT word FROM highlights WHERE user_id = ? ORDER BY word",
            (ctx.author.id,),
        )
        blocks = await self.bot.db.fetchall(
            "SELECT entity_id, entity_type FROM highlight_blocks WHERE user_id = ?",
            (ctx.author.id,),
        )

        embed = discord.Embed(
            title="Your highlights",
            description=", ".join(f"`{row['word']}`" for row in words)
            or "None yet. `?highlight add <word>`.",
            colour=COLOUR_INFO,
        )
        if blocks:
            embed.add_field(
                name="Blocked",
                value=", ".join(
                    f"<#{r['entity_id']}>" if r["entity_type"] == "channel"
                    else f"<@{r['entity_id']}>"
                    for r in blocks
                )[:1024],
                inline=False,
            )
        embed.set_footer(
            text="Highlights follow you across every server I am in. You are never "
            "notified about your own messages, or in a channel you have just been "
            "talking in."
        )
        await ctx.send(embed=embed, ephemeral=True)

    @highlight.command(name="add")
    @app_commands.describe(word="Word or phrase to watch for")
    async def highlight_add(self, ctx: commands.Context, *, word: str) -> None:
        """Watch for a word."""
        clean = word.strip().lower()
        if len(clean) < 3:
            raise DeezeeError(
                "A highlight must be at least 3 characters -- shorter than that "
                "matches half of every conversation."
            )
        if len(clean) > 60:
            raise DeezeeError("A highlight must be 60 characters or fewer.")

        total = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM highlights WHERE user_id = ?", (ctx.author.id,)
        )
        if (total or 0) >= MAX_HIGHLIGHTS:
            raise DeezeeError(f"You already have {MAX_HIGHLIGHTS} highlights.")

        await self.bot.db.execute(
            "INSERT OR IGNORE INTO highlights (user_id, word, created_at) "
            "VALUES (?, ?, ?)",
            (ctx.author.id, clean, int(time.time())),
        )
        self.invalidate_highlights()
        await ctx.send(
            embed=self._ok(f"You will be DMed when someone says `{clean}`."),
            ephemeral=True,
        )

    @highlight.command(name="remove")
    @app_commands.describe(word="Word to stop watching")
    async def highlight_remove(self, ctx: commands.Context, *, word: str) -> None:
        """Stop watching for a word."""
        cursor = await self.bot.db.execute(
            "DELETE FROM highlights WHERE user_id = ? AND word = ?",
            (ctx.author.id, word.strip().lower()),
        )
        self.invalidate_highlights()
        if not cursor.rowcount:
            raise DeezeeError(f"`{word}` is not one of your highlights.")
        await ctx.send(embed=self._ok(f"Removed `{word}`."), ephemeral=True)

    @highlight.command(name="block")
    @app_commands.describe(target="A channel or member you never want to hear from")
    async def highlight_block(self, ctx: commands.Context, *, target: str) -> None:
        """Never be highlighted by a channel or a member."""
        entity, kind = await self._resolve_channel_or_user(ctx, target)
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO highlight_blocks (user_id, entity_id, entity_type) "
            "VALUES (?, ?, ?)",
            (ctx.author.id, entity.id, kind),
        )
        await ctx.send(
            embed=self._ok(f"{entity.mention} will never trigger your highlights."),
            ephemeral=True,
        )

    @highlight.command(name="unblock")
    @app_commands.describe(target="A channel or member to hear from again")
    async def highlight_unblock(self, ctx: commands.Context, *, target: str) -> None:
        """Undo a block."""
        entity, kind = await self._resolve_channel_or_user(ctx, target)
        cursor = await self.bot.db.execute(
            "DELETE FROM highlight_blocks WHERE user_id = ? AND entity_id = ? "
            "AND entity_type = ?",
            (ctx.author.id, entity.id, kind),
        )
        if not cursor.rowcount:
            raise DeezeeError(f"{entity.mention} was not blocked.")
        await ctx.send(embed=self._ok(f"{entity.mention} unblocked."), ephemeral=True)

    @highlight.command(name="clear")
    async def highlight_clear(self, ctx: commands.Context) -> None:
        """Remove every highlight you have."""
        cursor = await self.bot.db.execute(
            "DELETE FROM highlights WHERE user_id = ?", (ctx.author.id,)
        )
        self.invalidate_highlights()
        await ctx.send(
            embed=self._ok(f"Cleared **{cursor.rowcount}** highlight(s)."),
            ephemeral=True,
        )

    async def highlight_pairs(self) -> list[tuple[int, str]]:
        """Every watched word, cached across messages."""
        if self._highlight_cache is None:
            rows = await self.bot.db.fetchall("SELECT user_id, word FROM highlights")
            self._highlight_cache = [(row["user_id"], row["word"]) for row in rows]
        return self._highlight_cache

    def invalidate_highlights(self) -> None:
        """Drop the cache after a write, so the next message sees the change."""
        self._highlight_cache = None

    @staticmethod
    async def _resolve_channel_or_user(
        ctx: commands.Context, query: str
    ) -> tuple[Any, str]:
        """Turn a mention, ID or name into a channel or a member."""
        try:
            return await commands.TextChannelConverter().convert(ctx, query), "channel"
        except commands.BadArgument:
            pass
        try:
            return await commands.MemberConverter().convert(ctx, query), "user"
        except commands.BadArgument:
            raise DeezeeError(f"`{query}` is not a channel or member here.") from None

    @commands.Cog.listener("on_message")
    async def watch_highlights(self, message: discord.Message) -> None:
        """DM anyone whose highlight word appears, subject to their blocks."""
        if message.guild is None or message.author.bot or not message.content:
            return

        self._last_seen[(message.channel.id, message.author.id)] = time.monotonic()

        watchers = await self.highlight_pairs()
        if not watchers:
            return

        lowered = message.content.lower()
        notified: set[int] = set()
        for user_id, word in watchers:
            if user_id in notified or user_id == message.author.id:
                continue
            if word not in lowered:
                continue

            member = message.guild.get_member(user_id)
            if member is None:
                continue
            if not message.channel.permissions_for(member).read_messages:
                continue

            last = self._last_seen.get((message.channel.id, user_id))
            if last is not None and time.monotonic() - last < HIGHLIGHT_PRESENCE_WINDOW:
                continue  # They are reading the channel already.

            blocked = await self.bot.db.fetchone(
                "SELECT 1 FROM highlight_blocks WHERE user_id = ? AND entity_id IN (?, ?)",
                (user_id, message.channel.id, message.author.id),
            )
            if blocked:
                continue

            notified.add(user_id)
            embed = discord.Embed(
                title=f"Highlight: {word}",
                description=message.content[:1000],
                colour=COLOUR_INFO,
                timestamp=message.created_at,
            )
            embed.set_author(
                name=str(message.author), icon_url=message.author.display_avatar.url
            )
            embed.add_field(
                name="Where",
                value=f"{message.channel.mention} in {message.guild.name}\n"
                f"[Jump]({message.jump_url})",
                inline=False,
            )
            embed.set_footer(text="Stop this with ?highlight remove, or ?highlight block")
            try:
                await member.send(embed=embed)
            except discord.HTTPException:
                pass  # Closed DMs are not an error worth reporting to anyone.

    @commands.hybrid_command(name="afk")
    @app_commands.describe(message="What to show people who mention you")
    @permissions.guild_only()
    async def afk(self, ctx: commands.Context, *, message: str = "AFK") -> None:
        """Set an AFK status shown when someone mentions you."""
        await self.bot.db.execute(
            "INSERT INTO afk (guild_id, user_id, message, since) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "message = excluded.message, since = excluded.since",
            (ctx.guild.id, ctx.author.id, message[:200], int(time.time())),
        )
        await ctx.send(
            embed=self._ok(
                f"You are AFK: {message[:200]}\nIt clears on your next message."
            )
        )

    @commands.Cog.listener("on_message")
    async def handle_afk(self, message: discord.Message) -> None:
        """Clear the author's AFK, and answer mentions of AFK members."""
        if message.guild is None or message.author.bot:
            return

        own = await self.bot.db.fetchone(
            "SELECT since FROM afk WHERE guild_id = ? AND user_id = ?",
            (message.guild.id, message.author.id),
        )
        if own is not None:
            await self.bot.db.execute(
                "DELETE FROM afk WHERE guild_id = ? AND user_id = ?",
                (message.guild.id, message.author.id),
            )
            away = format_duration(int(time.time()) - own["since"])
            try:
                await message.channel.send(
                    embed=discord.Embed(
                        description=f"Welcome back {message.author.mention} — "
                        f"you were away for **{away}**.",
                        colour=COLOUR_SUCCESS,
                    ),
                    delete_after=15,
                )
            except discord.HTTPException:
                pass

        if not message.mentions:
            return

        notes = []
        for user in message.mentions[:5]:
            row = await self.bot.db.fetchone(
                "SELECT message, since FROM afk WHERE guild_id = ? AND user_id = ?",
                (message.guild.id, user.id),
            )
            if row is None:
                continue
            notes.append(
                f"**{user.display_name}** is AFK: {row['message']} "
                f"— <t:{row['since']}:{TS_RELATIVE}>"
            )

        if notes:
            try:
                await message.channel.send(
                    embed=discord.Embed(
                        description="\n".join(notes), colour=COLOUR_MUTED
                    ),
                    delete_after=30,
                )
            except discord.HTTPException:
                pass

    # =======================================================================
    # Starboard
    # =======================================================================

    @commands.hybrid_group(name="starboard", with_app_command=False, aliases=["star"], fallback="status")
    @permissions.guild_only()
    async def starboard(self, ctx: commands.Context) -> None:
        """Show the starboard configuration."""
        config = await self.bot.guild_config.get(ctx.guild.id)
        channel_id = config["starboard_channel_id"]
        channel = ctx.guild.get_channel(int(channel_id)) if channel_id else None

        blocked = await self.bot.db.fetchall(
            "SELECT channel_id FROM starboard_blacklist WHERE guild_id = ?",
            (ctx.guild.id,),
        )
        posted = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM starboard_messages WHERE guild_id = ? "
            "AND star_message_id IS NOT NULL",
            (ctx.guild.id,),
        )

        embed = discord.Embed(
            title="Starboard",
            colour=COLOUR_SUCCESS if channel else COLOUR_DEFAULT,
            description=(
                f"Posting to {channel.mention}."
                if channel
                else "**Off.** Set a channel with `?starboard channel #stars`."
            ),
        )
        embed.add_field(name="Emoji", value=config["starboard_emoji"], inline=True)
        embed.add_field(
            name="Threshold", value=f"{config['starboard_threshold']} reaction(s)",
            inline=True,
        )
        embed.add_field(
            name="Self-stars",
            value="Counted" if config["starboard_selfstar"] else "Not counted",
            inline=True,
        )
        embed.add_field(
            name="Age-restricted channels",
            value="Allowed" if config["starboard_nsfw"] else "Never starred",
            inline=True,
        )
        embed.add_field(name="Messages on the board", value=str(posted or 0), inline=True)
        embed.add_field(
            name="Blacklisted channels",
            value=", ".join(f"<#{r['channel_id']}>" for r in blocked) or "None",
            inline=False,
        )
        await ctx.send(embed=embed)

    @starboard.command(name="channel")
    @app_commands.describe(channel="Where starred messages are posted")
    @permissions.admin_only()
    @permissions.guild_only()
    async def starboard_channel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Set or clear the starboard channel."""
        if channel is None:
            await self.bot.guild_config.set(ctx.guild.id, "starboard_channel_id", None)
            await ctx.send(embed=self._ok("Starboard turned off."))
            return

        perms = channel.permissions_for(ctx.guild.me)
        if not perms.send_messages or not perms.embed_links:
            raise DeezeeError(f"I need Send Messages and Embed Links in {channel.mention}.")

        await self.bot.guild_config.set(
            ctx.guild.id, "starboard_channel_id", channel.id
        )
        await ctx.send(embed=self._ok(f"Starred messages will post to {channel.mention}."))

    @starboard.command(name="emoji")
    @app_commands.describe(emoji="The reaction that counts as a star")
    @permissions.admin_only()
    @permissions.guild_only()
    async def starboard_emoji(self, ctx: commands.Context, emoji: str) -> None:
        """Set which reaction counts as a star."""
        clean = emoji.strip()
        if len(clean) > 64:
            raise DeezeeError("That does not look like a single emoji.")
        await self.bot.guild_config.set(ctx.guild.id, "starboard_emoji", clean)
        await ctx.send(embed=self._ok(f"{clean} now counts as a star."))

    @starboard.command(name="threshold")
    @app_commands.describe(count="How many reactions are needed")
    @permissions.admin_only()
    @permissions.guild_only()
    async def starboard_threshold(self, ctx: commands.Context, count: int) -> None:
        """Set how many stars a message needs."""
        if not 1 <= count <= 100:
            raise DeezeeError("The threshold must be between 1 and 100.")
        await self.bot.guild_config.set(ctx.guild.id, "starboard_threshold", count)
        await ctx.send(embed=self._ok(f"Messages need **{count}** star(s)."))

    @starboard.command(name="selfstar")
    @app_commands.describe(state="on or off")
    @permissions.admin_only()
    @permissions.guild_only()
    async def starboard_selfstar(self, ctx: commands.Context, state: str) -> None:
        """Choose whether an author's own star counts."""
        on = state.lower() in {"on", "yes", "true", "enable"}
        await self.bot.guild_config.set(ctx.guild.id, "starboard_selfstar", int(on))
        await ctx.send(
            embed=self._ok(
                f"Self-stars are now **{'counted' if on else 'not counted'}**."
            )
        )

    @starboard.command(name="blacklist")
    @app_commands.describe(channel="Channel whose messages can never be starred")
    @permissions.admin_only()
    @permissions.guild_only()
    async def starboard_blacklist(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Toggle a channel's exclusion from the starboard."""
        existing = await self.bot.db.fetchone(
            "SELECT 1 FROM starboard_blacklist WHERE guild_id = ? AND channel_id = ?",
            (ctx.guild.id, channel.id),
        )
        if existing:
            await self.bot.db.execute(
                "DELETE FROM starboard_blacklist WHERE guild_id = ? AND channel_id = ?",
                (ctx.guild.id, channel.id),
            )
            await ctx.send(embed=self._ok(f"{channel.mention} can be starred again."))
            return
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO starboard_blacklist (guild_id, channel_id) "
            "VALUES (?, ?)",
            (ctx.guild.id, channel.id),
        )
        await ctx.send(embed=self._ok(f"{channel.mention} will never be starred."))

    @starboard.command(name="top")
    @permissions.guild_only()
    async def starboard_top(self, ctx: commands.Context) -> None:
        """Show the most-starred messages of all time."""
        rows = await self.bot.db.fetchall(
            "SELECT * FROM starboard_messages WHERE guild_id = ? AND stars > 0 "
            "ORDER BY stars DESC LIMIT 100",
            (ctx.guild.id,),
        )
        emoji = await self.bot.guild_config.value(ctx.guild.id, "starboard_emoji")
        lines = [
            f"**{index}.** {emoji} **{row['stars']}** — <@{row['author_id']}> in "
            f"<#{row['channel_id']}>\n"
            f"https://discord.com/channels/{ctx.guild.id}/{row['channel_id']}/{row['source_id']}"
            for index, row in enumerate(rows, start=1)
        ]
        pages = paginate_lines(
            lines,
            title="Most-starred messages",
            per_page=6,
            empty_message="Nothing has been starred yet.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @commands.Cog.listener("on_raw_reaction_add")
    async def starboard_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._update_star(payload)

    @commands.Cog.listener("on_raw_reaction_remove")
    async def starboard_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._update_star(payload)

    async def _update_star(self, payload: discord.RawReactionActionEvent) -> None:
        """Recount a message's stars and create, edit or remove its board post."""
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        config = await self.bot.guild_config.get(guild.id)
        board_id = config["starboard_channel_id"]
        if not board_id:
            return
        if str(payload.emoji) != config["starboard_emoji"]:
            return
        if payload.channel_id == int(board_id):
            return  # Starring the board post itself is not a vote on the source.

        blocked = await self.bot.db.fetchone(
            "SELECT 1 FROM starboard_blacklist WHERE guild_id = ? AND channel_id = ?",
            (guild.id, payload.channel_id),
        )
        if blocked:
            return

        channel = guild.get_channel(payload.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        if getattr(channel, "nsfw", False) and not config["starboard_nsfw"]:
            return

        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            return

        reaction = discord.utils.find(
            lambda r: str(r.emoji) == config["starboard_emoji"], message.reactions
        )
        stars = reaction.count if reaction else 0
        if reaction and not config["starboard_selfstar"]:
            # Discount the author's own star without paginating every reactor:
            # only their presence matters, and users() is the only way to know.
            try:
                async for user in reaction.users():
                    if user.id == message.author.id:
                        stars -= 1
                        break
            except discord.HTTPException:
                pass

        board = guild.get_channel(int(board_id))
        if not isinstance(board, discord.TextChannel):
            return

        existing = await self.bot.db.fetchone(
            "SELECT * FROM starboard_messages WHERE guild_id = ? AND source_id = ?",
            (guild.id, message.id),
        )
        threshold = int(config["starboard_threshold"])

        if stars < threshold:
            if existing and existing["star_message_id"]:
                try:
                    old = await board.fetch_message(existing["star_message_id"])
                    await old.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                await self.bot.db.execute(
                    "UPDATE starboard_messages SET star_message_id = NULL, stars = ? "
                    "WHERE guild_id = ? AND source_id = ?",
                    (stars, guild.id, message.id),
                )
            return

        embed = self._star_embed(message, stars, config["starboard_emoji"])

        if existing and existing["star_message_id"]:
            try:
                post = await board.fetch_message(existing["star_message_id"])
                await post.edit(
                    content=f"{config['starboard_emoji']} **{stars}** in "
                    f"{channel.mention}",
                    embed=embed,
                )
            except (discord.NotFound, discord.Forbidden):
                existing = None
            else:
                await self.bot.db.execute(
                    "UPDATE starboard_messages SET stars = ? "
                    "WHERE guild_id = ? AND source_id = ?",
                    (stars, guild.id, message.id),
                )
                return

        try:
            post = await board.send(
                content=f"{config['starboard_emoji']} **{stars}** in {channel.mention}",
                embed=embed,
            )
        except discord.HTTPException:
            return

        await self.bot.db.execute(
            "INSERT INTO starboard_messages (guild_id, source_id, channel_id, "
            "star_message_id, author_id, stars, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, source_id) DO UPDATE SET "
            "star_message_id = excluded.star_message_id, stars = excluded.stars",
            (
                guild.id, message.id, channel.id, post.id, message.author.id,
                stars, int(time.time()),
            ),
        )

    @staticmethod
    def _star_embed(message: discord.Message, stars: int, emoji: str) -> discord.Embed:
        """Render a starred message for the board."""
        embed = discord.Embed(
            description=message.content[:3000] or None,
            colour=COLOUR_WARNING,
            timestamp=message.created_at,
        )
        embed.set_author(
            name=str(message.author), icon_url=message.author.display_avatar.url
        )
        embed.add_field(
            name="Source", value=f"[Jump to message]({message.jump_url})", inline=False
        )
        for attachment in message.attachments:
            if attachment.content_type and attachment.content_type.startswith("image/"):
                embed.set_image(url=attachment.url)
                break
        embed.set_footer(text=f"{emoji} {stars} • {message.id}")
        return embed

    # =======================================================================
    # Suggestions
    # =======================================================================

    @commands.hybrid_command(name="suggest")
    @app_commands.describe(text="Your suggestion")
    @commands.cooldown(1, 60, commands.BucketType.member)
    @permissions.guild_only()
    async def suggest(self, ctx: commands.Context, *, text: str) -> None:
        """Submit a suggestion with vote buttons."""
        channel_id = await self.bot.guild_config.channel_id(
            ctx.guild.id, "suggestions_channel_id"
        )
        if channel_id is None:
            raise NotConfigured("a suggestions channel", "Utility")
        channel = ctx.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise DeezeeError("The configured suggestions channel no longer exists.")

        cursor = await self.bot.db.execute(
            "INSERT INTO suggestions (guild_id, channel_id, author_id, author_tag, "
            "body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                ctx.guild.id, channel.id, ctx.author.id, str(ctx.author),
                text[:2000], int(time.time()),
            ),
        )
        suggestion_id = int(cursor.lastrowid or 0)

        view = discord.ui.View(timeout=None)
        view.add_item(SuggestionButton(suggestion_id, "up"))
        view.add_item(SuggestionButton(suggestion_id, "down"))

        message = await channel.send(
            embed=await self.suggestion_embed(suggestion_id), view=view
        )
        await self.bot.db.execute(
            "UPDATE suggestions SET message_id = ? WHERE id = ?",
            (message.id, suggestion_id),
        )
        await ctx.send(
            embed=self._ok(f"Suggestion **#{suggestion_id}** posted: {message.jump_url}"),
            ephemeral=True,
        )

    async def suggestion_embed(self, suggestion_id: int) -> discord.Embed:
        """Render a suggestion with its current tally and status."""
        row = await self.bot.db.fetchone(
            "SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)
        )
        if row is None:
            return discord.Embed(description="Deleted.", colour=COLOUR_ERROR)

        up = int(
            await self.bot.db.fetchval(
                "SELECT COUNT(*) FROM suggestion_votes WHERE suggestion_id = ? "
                "AND vote = 1",
                (suggestion_id,),
            ) or 0
        )
        down = int(
            await self.bot.db.fetchval(
                "SELECT COUNT(*) FROM suggestion_votes WHERE suggestion_id = ? "
                "AND vote = -1",
                (suggestion_id,),
            ) or 0
        )

        colours = {
            "open": COLOUR_INFO,
            "approved": COLOUR_SUCCESS,
            "denied": COLOUR_ERROR,
            "implemented": 0x9B59B6,
        }
        embed = discord.Embed(
            title=f"Suggestion #{suggestion_id}",
            description=row["body"],
            colour=colours.get(row["status"], COLOUR_INFO),
            timestamp=datetime.fromtimestamp(row["created_at"], tz=timezone.utc),
        )
        embed.add_field(
            name="Votes",
            value=f"\N{THUMBS UP SIGN} **{up}**   \N{THUMBS DOWN SIGN} **{down}**   "
            f"(net {up - down:+d})",
            inline=False,
        )
        embed.add_field(name="By", value=f"<@{row['author_id']}>", inline=True)
        embed.add_field(name="Status", value=row["status"].title(), inline=True)
        if row["staff_comment"]:
            embed.add_field(
                name=f"Staff — {row['status']}",
                value=f"{row['staff_comment']}\n— <@{row['staff_id']}>",
                inline=False,
            )
        return embed

    async def record_suggestion_vote(
        self, interaction: discord.Interaction, suggestion_id: int, vote: int
    ) -> None:
        """Record, flip or withdraw a vote, then redraw the suggestion."""
        row = await self.bot.db.fetchone(
            "SELECT status FROM suggestions WHERE id = ?", (suggestion_id,)
        )
        if row is None:
            await interaction.response.send_message(
                "That suggestion no longer exists.", ephemeral=True
            )
            return
        if row["status"] != "open":
            await interaction.response.send_message(
                f"That suggestion is already **{row['status']}**.", ephemeral=True
            )
            return

        existing = await self.bot.db.fetchval(
            "SELECT vote FROM suggestion_votes WHERE suggestion_id = ? AND user_id = ?",
            (suggestion_id, interaction.user.id),
        )

        if existing == vote:
            await self.bot.db.execute(
                "DELETE FROM suggestion_votes WHERE suggestion_id = ? AND user_id = ?",
                (suggestion_id, interaction.user.id),
            )
            note = "Vote withdrawn."
        else:
            await self.bot.db.execute(
                "INSERT INTO suggestion_votes (suggestion_id, user_id, vote) "
                "VALUES (?, ?, ?) ON CONFLICT(suggestion_id, user_id) "
                "DO UPDATE SET vote = excluded.vote",
                (suggestion_id, interaction.user.id, vote),
            )
            note = "Vote recorded."

        await interaction.response.send_message(note, ephemeral=True)
        try:
            await interaction.message.edit(
                embed=await self.suggestion_embed(suggestion_id)
            )
        except discord.HTTPException:
            pass

    @commands.hybrid_command(name="suggestion", with_app_command=False)
    @app_commands.describe(
        action="approve, deny, implement or comment",
        suggestion_id="Number from the suggestion embed",
        reason="Shown publicly on the suggestion",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def suggestion(
        self,
        ctx: commands.Context,
        action: str,
        suggestion_id: int,
        *,
        reason: str = "",
    ) -> None:
        """Approve, deny, implement or comment on a suggestion."""
        choice = action.lower()
        statuses = {
            "approve": "approved",
            "deny": "denied",
            "implement": "implemented",
            "comment": None,
        }
        if choice not in statuses:
            raise DeezeeError("Use `approve`, `deny`, `implement` or `comment`.")

        row = await self.bot.db.fetchone(
            "SELECT * FROM suggestions WHERE id = ? AND guild_id = ?",
            (suggestion_id, ctx.guild.id),
        )
        if row is None:
            raise DeezeeError(f"No suggestion **#{suggestion_id}** in this server.")

        status = statuses[choice] or row["status"]
        await self.bot.db.execute(
            "UPDATE suggestions SET status = ?, staff_id = ?, staff_comment = ? "
            "WHERE id = ?",
            (status, ctx.author.id, reason.strip() or None, suggestion_id),
        )

        channel = ctx.guild.get_channel(row["channel_id"])
        if isinstance(channel, discord.TextChannel) and row["message_id"]:
            try:
                message = await channel.fetch_message(row["message_id"])
                await message.edit(
                    embed=await self.suggestion_embed(suggestion_id),
                    view=None if status != "open" else discord.utils.MISSING,
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        author = ctx.guild.get_member(row["author_id"])
        if author is not None and status != row["status"]:
            try:
                await author.send(
                    embed=discord.Embed(
                        title=f"Your suggestion #{suggestion_id} was {status}",
                        description=reason or "No comment was given.",
                        colour=COLOUR_INFO,
                    )
                )
            except discord.HTTPException:
                pass

        await ctx.send(
            embed=self._ok(f"Suggestion **#{suggestion_id}** is now **{status}**.")
        )

    # =======================================================================
    # Small tools
    # =======================================================================

    @commands.hybrid_command(name="firstmessage", with_app_command=False, aliases=["first"])
    @app_commands.describe(channel="Which channel. Defaults to here")
    @permissions.guild_only()
    async def firstmessage(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Link the first message ever sent in a channel."""
        target = channel or ctx.channel
        await self._defer(ctx)

        try:
            first = [
                message
                async for message in target.history(limit=1, oldest_first=True)
            ]
        except discord.Forbidden:
            raise DeezeeError(f"I cannot read history in {target.mention}.") from None

        if not first:
            raise DeezeeError(f"{target.mention} has no messages.")

        message = first[0]
        embed = discord.Embed(
            title=f"First message in #{target.name}",
            description=message.content[:1000] or "*(no text)*",
            colour=COLOUR_INFO,
            timestamp=message.created_at,
        )
        embed.set_author(
            name=str(message.author), icon_url=message.author.display_avatar.url
        )
        embed.add_field(name="Jump", value=f"[Go there]({message.jump_url})", inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="color", aliases=["colour"])
    @app_commands.describe(value="Hex like #ff0000, a colour name, or random")
    async def color(self, ctx: commands.Context, value: str = "random") -> None:
        """Preview a hex or named colour, or generate a random one."""
        from cogs.roles import parse_colour

        colour = parse_colour(value)
        if colour is None:
            raise DeezeeError(f"`{value}` is not a colour. Try `#5865f2` or `random`.")

        red, green, blue = colour.r, colour.g, colour.b
        biggest, smallest = max(red, green, blue) / 255, min(red, green, blue) / 255
        lightness = (biggest + smallest) / 2

        embed = discord.Embed(
            title=str(colour).upper(),
            colour=colour,
            description=(
                f"**Hex** `{colour}`\n"
                f"**RGB** `{red}, {green}, {blue}`\n"
                f"**Int** `{colour.value}`\n"
                f"**Lightness** {lightness * 100:.0f}%"
            ),
        )
        # No swatch image. The embed's own colour bar already shows the colour,
        # and rendering one would mean either a Pillow job or a call to a
        # third-party image host this bot has no other reason to depend on.
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="inviteinfo", with_app_command=False)
    @app_commands.describe(code="An invite code or full invite link")
    async def inviteinfo(self, ctx: commands.Context, code: str) -> None:
        """Show which server an invite points to, its uses and expiry."""
        clean = code.strip().rsplit("/", 1)[-1].split("?")[0]
        await self._defer(ctx)

        try:
            invite = await self.bot.fetch_invite(clean, with_counts=True)
        except discord.NotFound:
            raise DeezeeError(f"`{clean}` is not a valid invite, or it has expired.") from None
        except discord.HTTPException as exc:
            raise DeezeeError(f"Discord refused that lookup: {exc}") from exc

        embed = discord.Embed(
            title=invite.guild.name if invite.guild else "Unknown server",
            colour=COLOUR_INFO,
            description=getattr(invite.guild, "description", None),
        )
        if invite.guild and getattr(invite.guild, "icon", None):
            embed.set_thumbnail(url=invite.guild.icon.url)
        embed.add_field(name="Code", value=f"`{invite.code}`", inline=True)
        if invite.guild:
            created = (invite.guild.id >> 22) + DISCORD_EPOCH_MS
            embed.add_field(
                name="Server created",
                value=f"<t:{created // 1000}:{TS_RELATIVE}>",
                inline=True,
            )
            embed.add_field(name="Server ID", value=f"`{invite.guild.id}`", inline=True)
        embed.add_field(
            name="Members",
            value=f"{invite.approximate_member_count or '?'} "
            f"({invite.approximate_presence_count or '?'} online)",
            inline=True,
        )
        embed.add_field(
            name="Channel",
            value=f"#{invite.channel.name}" if invite.channel else "unknown",
            inline=True,
        )
        embed.add_field(
            name="Inviter", value=str(invite.inviter) if invite.inviter else "unknown",
            inline=True,
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="timestamp", aliases=["time"])
    @app_commands.describe(
        when="A time: 'in 2h', '2026-08-07 14:30', '14:30', or a unix timestamp",
        tz="IANA timezone, e.g. Europe/Amsterdam. Defaults to UTC",
    )
    async def timestamp(
        self, ctx: commands.Context, when: str, tz: str = "UTC"
    ) -> None:
        """Convert a date and time into every Discord timestamp format."""
        try:
            from zoneinfo import ZoneInfo

            zone = ZoneInfo(tz)
        except Exception:
            raise DeezeeError(
                f"`{tz}` is not a timezone I know. Use an IANA name such as "
                "`Europe/Amsterdam`, `America/New_York` or `UTC`."
            ) from None

        epoch = self._parse_when(when, zone)
        if epoch is None:
            raise DeezeeError(
                f"I could not read `{when}` as a time. Try `in 2h`, "
                "`2026-08-07 14:30`, `14:30`, or a unix timestamp."
            )

        styles = [
            ("Short time", "t"), ("Long time", "T"), ("Short date", "d"),
            ("Long date", "D"), ("Date and time", "f"),
            ("Full", "F"), ("Relative", "R"),
        ]
        embed = discord.Embed(
            title="Discord timestamps",
            description=f"`{when}` in `{tz}` → epoch `{epoch}`",
            colour=COLOUR_INFO,
        )
        for label, style in styles:
            embed.add_field(
                name=label,
                value=f"<t:{epoch}:{style}>\n`<t:{epoch}:{style}>`",
                inline=True,
            )
        await ctx.send(embed=embed)

    @staticmethod
    def _parse_when(text: str, zone: Any) -> int | None:
        """Turn a written time into epoch seconds.

        Deliberately narrow: four shapes it can read exactly, and ``None`` for
        anything else. A time parser that guesses produces announcements posted
        on the wrong day.
        """
        cleaned = text.strip()
        lowered = cleaned.lower()

        if lowered.startswith("in "):
            delta = parse_duration(lowered[3:])
            return int(time.time() + delta.total_seconds()) if delta else None

        if cleaned.isdigit() and len(cleaned) >= 9:
            return int(cleaned)  # Already a unix timestamp.

        if lowered in {"now", "today"}:
            return int(time.time())

        match = _DATE_TIME.match(cleaned)
        if match:
            year, month, day = int(match[1]), int(match[2]), int(match[3])
            hour = int(match[4] or 0)
            minute = int(match[5] or 0)
            second = int(match[6] or 0)
            try:
                moment = datetime(year, month, day, hour, minute, second, tzinfo=zone)
            except ValueError:
                return None
            return int(moment.timestamp())

        match = _TIME_ONLY.match(cleaned)
        if match:
            now = datetime.now(tz=zone)
            try:
                moment = now.replace(
                    hour=int(match[1]),
                    minute=int(match[2]),
                    second=int(match[3] or 0),
                    microsecond=0,
                )
            except ValueError:
                return None
            # A time already past today means tomorrow -- which is what someone
            # writing "09:00" at midday means.
            if moment < now:
                moment += timedelta(days=1)
            return int(moment.timestamp())

        delta = parse_duration(cleaned)
        return int(time.time() + delta.total_seconds()) if delta else None


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point."""
    await bot.add_cog(Utility(bot))
