"""Automod: content filters, exemptions, link scanning and the warning ladder.

Covers 19 commands from the command sheet: the ``?automod`` panel and its four
subcommands, twelve ``?filter`` subcommands, ``?scanlinks`` and ``?mediaonly``.

The listener runs on every message the bot can see, so it is written to do as
little as possible in the common case: a guild with no filters enabled costs one
dictionary lookup. Configuration is cached in memory and invalidated on write --
querying SQLite per message would be the single hottest query in the bot.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import modlog, permissions
from core.constants import (
    COLOUR_DEFAULT,
    COLOUR_INFO,
    COLOUR_SUCCESS,
    COLOUR_WARNING,
    EMOJI_OFF,
    EMOJI_ON,
    EMOJI_SUCCESS,
    MAX_TIMEOUT_DAYS,
)
from core.errors import DeezeeError
from services import filters as F
from services.safebrowsing import SafeBrowsing
from services.timeparse import format_duration, parse_duration
from ui.paginator import Paginator, paginate_lines
from ui.panels.automod import open_panel
from ui.views import confirm

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FilterSpec:
    """Static description of one filter.

    ``params`` maps the generic ``param1``/``param2`` columns onto names and
    labels for this filter, which is what lets one table serve twelve filters
    without a migration per knob.
    """

    label: str
    summary: str
    #: ``(column, label, placeholder)`` for each numeric threshold.
    params: tuple[tuple[str, str, str], ...] = ()
    #: Allowed values for ``mode``; the first is the default.
    modes: tuple[str, ...] = ()
    defaults: tuple[int, int] = (0, 0)


#: Every filter, in the order they are checked and displayed.
FILTER_SPECS: dict[str, FilterSpec] = {
    "words": FilterSpec(
        "Banned words",
        "Delete messages containing a word from the list",
        modes=("wildcard", "whole"),
    ),
    "invites": FilterSpec(
        "Invite links",
        "Block Discord invites, with an allowlist of permitted servers",
        modes=("strict", "allowlist"),
    ),
    "links": FilterSpec(
        "Links",
        "Block all links, or run an allowlist or blocklist of domains",
        modes=("all", "allowlist", "blocklist"),
    ),
    "caps": FilterSpec(
        "Excessive caps",
        "Delete messages above a percentage of uppercase letters",
        params=(("param1", "Uppercase percent (1-100)", "70"),
                ("param2", "Minimum message length", "10")),
        defaults=(70, 10),
    ),
    "spam": FilterSpec(
        "Message spam",
        "Rate-limit messages per user",
        params=(("param1", "Messages", "5"), ("param2", "Within seconds", "5")),
        defaults=(5, 5),
    ),
    "mentions": FilterSpec(
        "Mass mentions",
        "Punish messages with more than N mentions",
        params=(("param1", "Maximum mentions", "5"),),
        defaults=(5, 0),
    ),
    "emoji": FilterSpec(
        "Emoji spam",
        "Punish messages with more than N emoji",
        params=(("param1", "Maximum emoji", "10"),),
        defaults=(10, 0),
    ),
    "attachments": FilterSpec(
        "Attachment spam",
        "Rate-limit attachments and block dangerous file types",
        params=(("param1", "Attachments", "5"), ("param2", "Within seconds", "10")),
        defaults=(5, 10),
    ),
    "zalgo": FilterSpec(
        "Zalgo text",
        "Delete messages stacked with combining characters",
        params=(("param1", "Combining character percent", "30"),),
        defaults=(30, 0),
    ),
    "duplicates": FilterSpec(
        "Repeated messages",
        "Punish the same message sent repeatedly",
        params=(("param1", "Repeats", "3"), ("param2", "Within seconds", "30")),
        defaults=(3, 30),
    ),
    "newlines": FilterSpec(
        "Wall of text",
        "Punish very long messages or ones with many line breaks",
        params=(("param1", "Maximum newlines", "15"),
                ("param2", "Maximum characters", "1200")),
        defaults=(15, 1200),
    ),
    "stickers": FilterSpec(
        "Sticker spam",
        "Rate-limit stickers",
        params=(("param1", "Stickers", "3"), ("param2", "Within seconds", "10")),
        defaults=(3, 10),
    ),
    "scanlinks": FilterSpec(
        "Link scanning",
        "Check unknown links against Google Safe Browsing",
    ),
}

#: File extensions blocked by default when the attachment filter is on.
DEFAULT_BLOCKED_EXTENSIONS = (
    "exe", "scr", "bat", "cmd", "com", "pif", "vbs", "vbe", "js", "jse",
    "ws", "wsf", "msi", "msp", "hta", "cpl", "jar", "reg", "ps1", "psm1",
    "lnk", "apk", "dmg", "app", "deb", "rpm",
)

#: Punishments a filter may apply.
ACTIONS = ("delete", "warn", "mute", "kick", "ban")


@dataclass(slots=True)
class Trip:
    """One filter deciding a message is not allowed."""

    name: str
    reason: str
    detail: str = ""
    #: Set when the filter wants a stronger action than its configured one.
    override_action: str | None = None


class Automod(commands.Cog):
    """Content filters, exemptions and link scanning."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.specs = FILTER_SPECS
        self.rates = F.RateTracker()
        self.scanner: SafeBrowsing | None = None

        # Per-guild caches. Every one is invalidated on write, and the listener
        # reads only from here.
        self._filters: dict[int, dict[str, dict[str, Any]]] = {}
        self._values: dict[tuple[int, str, str], frozenset[str]] = {}
        self._exempt: dict[int, list[tuple[int, str, str]]] = {}
        self._media: dict[int, frozenset[int]] = {}

    async def open_config_panel(self, interaction: discord.Interaction) -> None:
        """Open this cog's panel from the ``?config`` root panel."""
        from ui.panels.automod import AutomodPanel

        filters = await self.load_filters(interaction.guild.id)
        view = AutomodPanel(self, interaction.user, self.specs, filters)
        await interaction.response.send_message(
            embed=view.embed(interaction.guild), view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    async def cog_load(self) -> None:
        """Open the link scanner and register the cache-pruning task."""
        self.scanner = SafeBrowsing(
            self.bot.config.google_safe_browsing_key,
            self.bot.db,
            self.bot.config.data_path,
        )
        self.bot.scheduler.register("prune_scan_cache", self._prune_scan_cache)

    async def cog_unload(self) -> None:
        self.bot.scheduler.unregister("prune_scan_cache")
        if self.scanner is not None:
            await self.scanner.close()

    @commands.Cog.listener("on_ready")
    async def seed_prune_task(self) -> None:
        """Queue the first scan-cache sweep if one is not already pending.

        The handler reschedules itself, so this only ever has to fire once --
        but without it nothing would ever start the chain, and expired verdicts
        would accumulate against the storage budget forever.
        """
        if not self.bot.guilds:
            return
        pending = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM scheduled_actions "
            "WHERE action_type = 'prune_scan_cache' AND status = 'pending'"
        )
        if pending:
            return
        await self.bot.scheduler.schedule(
            self.bot.guilds[0].id, "prune_scan_cache", int(time.time()) + 300, {}
        )
        log.debug("Seeded the scan-cache pruning task")

    async def _prune_scan_cache(self, guild_id: int, payload: dict[str, Any]) -> None:
        """Delete expired scan verdicts, then queue the next sweep."""
        if self.scanner is not None:
            removed = await self.scanner.prune_cache()
            if removed:
                log.info("Pruned %d expired scan cache row(s)", removed)
        await self.bot.scheduler.schedule(
            guild_id, "prune_scan_cache", int(time.time()) + 21600, {}
        )

    # =======================================================================
    # Configuration access
    # =======================================================================

    async def load_filters(self, guild_id: int) -> dict[str, dict[str, Any]]:
        """Every filter row for a guild, creating missing ones at their defaults."""
        cached = self._filters.get(guild_id)
        if cached is not None:
            return cached

        await self.bot.guild_config.get(guild_id)  # guarantees the parent row
        rows = await self.bot.db.fetchall(
            "SELECT * FROM automod_filters WHERE guild_id = ?", (guild_id,)
        )
        existing = {row["name"]: dict(row) for row in rows}

        missing = [name for name in self.specs if name not in existing]
        if missing:
            now_rows = [
                (
                    guild_id,
                    name,
                    self.specs[name].defaults[0],
                    self.specs[name].defaults[1],
                    self.specs[name].modes[0] if self.specs[name].modes else "",
                )
                for name in missing
            ]
            await self.bot.db.executemany(
                "INSERT OR IGNORE INTO automod_filters "
                "(guild_id, name, param1, param2, mode) VALUES (?, ?, ?, ?, ?)",
                now_rows,
            )
            rows = await self.bot.db.fetchall(
                "SELECT * FROM automod_filters WHERE guild_id = ?", (guild_id,)
            )
            existing = {row["name"]: dict(row) for row in rows}

        self._filters[guild_id] = existing
        return existing

    def invalidate(self, guild_id: int) -> None:
        """Drop every cached automod setting for a guild."""
        self._filters.pop(guild_id, None)
        self._exempt.pop(guild_id, None)
        self._media.pop(guild_id, None)
        for key in [k for k in self._values if k[0] == guild_id]:
            del self._values[key]

    async def set_filter_enabled(self, guild_id: int, name: str, enabled: bool) -> None:
        """Turn one filter on or off."""
        await self.load_filters(guild_id)
        await self.bot.db.execute(
            "UPDATE automod_filters SET enabled = ? WHERE guild_id = ? AND name = ?",
            (int(enabled), guild_id, name),
        )
        self.invalidate(guild_id)

    async def set_filter_action(self, guild_id: int, name: str, action: str) -> None:
        """Set the punishment applied when a filter trips."""
        if action not in ACTIONS:
            raise DeezeeError(f"`{action}` is not a punishment. Use: {', '.join(ACTIONS)}.")
        await self.load_filters(guild_id)
        await self.bot.db.execute(
            "UPDATE automod_filters SET action = ? WHERE guild_id = ? AND name = ?",
            (action, guild_id, name),
        )
        self.invalidate(guild_id)

    async def set_filter_params(
        self, guild_id: int, name: str, values: dict[str, int]
    ) -> None:
        """Write the numeric thresholds for one filter."""
        allowed = {attr for attr, _, _ in self.specs[name].params}
        clean = {k: v for k, v in values.items() if k in allowed}
        if not clean:
            return
        await self.load_filters(guild_id)
        assignments = ", ".join(f"{column} = ?" for column in clean)
        await self.bot.db.execute(
            f"UPDATE automod_filters SET {assignments} WHERE guild_id = ? AND name = ?",
            (*clean.values(), guild_id, name),
        )
        self.invalidate(guild_id)

    async def set_filter_mode(self, guild_id: int, name: str, mode: str) -> None:
        """Set the matching mode for a filter that has one."""
        modes = self.specs[name].modes
        if mode not in modes:
            raise DeezeeError(f"`{mode}` is not a mode. Use: {', '.join(modes)}.")
        await self.load_filters(guild_id)
        await self.bot.db.execute(
            "UPDATE automod_filters SET mode = ? WHERE guild_id = ? AND name = ?",
            (mode, guild_id, name),
        )
        self.invalidate(guild_id)

    async def values(self, guild_id: int, name: str, kind: str) -> frozenset[str]:
        """The list-shaped part of a filter's config: words, domains, extensions."""
        key = (guild_id, name, kind)
        cached = self._values.get(key)
        if cached is not None:
            return cached
        rows = await self.bot.db.fetchall(
            "SELECT value FROM filter_values WHERE guild_id = ? AND name = ? AND kind = ?",
            (guild_id, name, kind),
        )
        result = frozenset(row["value"] for row in rows)
        self._values[key] = result
        return result

    async def add_values(
        self, guild_id: int, name: str, kind: str, items: list[str], author_id: int
    ) -> int:
        """Add entries to a filter list. Returns how many were new."""
        await self.bot.guild_config.get(guild_id)
        now = int(time.time())
        before = len(await self.values(guild_id, name, kind))
        await self.bot.db.executemany(
            "INSERT OR IGNORE INTO filter_values "
            "(guild_id, name, kind, value, added_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(guild_id, name, kind, item, author_id, now) for item in items],
        )
        self.invalidate(guild_id)
        return len(await self.values(guild_id, name, kind)) - before

    async def remove_values(
        self, guild_id: int, name: str, kind: str, items: list[str]
    ) -> int:
        """Remove entries from a filter list. Returns how many existed."""
        removed = 0
        for item in items:
            cursor = await self.bot.db.execute(
                "DELETE FROM filter_values WHERE guild_id = ? AND name = ? "
                "AND kind = ? AND value = ?",
                (guild_id, name, kind, item),
            )
            removed += cursor.rowcount
        self.invalidate(guild_id)
        return removed

    async def exemptions(self, guild_id: int) -> list[tuple[int, str, str]]:
        """``(entity_id, entity_type, filter_name)`` triples for a guild."""
        cached = self._exempt.get(guild_id)
        if cached is not None:
            return cached
        rows = await self.bot.db.fetchall(
            "SELECT entity_id, entity_type, filter_name FROM automod_exempt "
            "WHERE guild_id = ?",
            (guild_id,),
        )
        result = [(r["entity_id"], r["entity_type"], r["filter_name"]) for r in rows]
        self._exempt[guild_id] = result
        return result

    async def media_channels(self, guild_id: int) -> frozenset[int]:
        """Channels that require an attachment or a link."""
        cached = self._media.get(guild_id)
        if cached is not None:
            return cached
        rows = await self.bot.db.fetchall(
            "SELECT channel_id FROM media_channels WHERE guild_id = ?", (guild_id,)
        )
        result = frozenset(row["channel_id"] for row in rows)
        self._media[guild_id] = result
        return result

    # =======================================================================
    # The listener
    # =======================================================================

    @commands.Cog.listener("on_message")
    async def scan_message(self, message: discord.Message) -> None:
        """Run enabled filters against a message.

        Ordered cheapest-check-first: everything that can rule the message out
        without touching the database does so before anything that cannot.
        """
        if message.guild is None or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return

        # Staff are not filtered. Checking the tier first means an ordinary
        # member's message costs one cached lookup and a moderator's costs two.
        tier = await permissions.tier_of(self.bot, message.author, message.guild)
        if tier >= permissions.Tier.MOD:
            return

        guild_id = message.guild.id
        rows = await self.load_filters(guild_id)
        enabled = {name for name, row in rows.items() if row["enabled"]}

        media = await self.media_channels(guild_id)
        if not enabled and message.channel.id not in media:
            return

        exempt = await self.exemptions(guild_id)
        if self._is_exempt(message, exempt, "*"):
            return

        if message.channel.id in media and not self._is_media_post(message):
            await self._enforce_media_only(message)
            return

        trip = await self._first_trip(message, rows, enabled, exempt)
        if trip is None:
            return

        await self._punish(message, trip, rows)

    @staticmethod
    def _is_exempt(
        message: discord.Message,
        exemptions: list[tuple[int, str, str]],
        filter_name: str,
    ) -> bool:
        """Whether this message is exempt from a filter.

        An exemption for ``*`` covers every filter; one naming a filter covers
        only that one, so a links channel can allow links without allowing slurs.
        """
        role_ids = {role.id for role in message.author.roles}
        for entity_id, entity_type, name in exemptions:
            if name not in ("*", filter_name):
                continue
            if entity_type == "member" and entity_id == message.author.id:
                return True
            if entity_type == "channel" and entity_id == message.channel.id:
                return True
            if entity_type == "role" and entity_id in role_ids:
                return True
        return False

    @staticmethod
    def _is_media_post(message: discord.Message) -> bool:
        """Whether a message satisfies a media-only channel."""
        return bool(message.attachments or F.extract_urls(message.content))

    async def _enforce_media_only(self, message: discord.Message) -> None:
        """Delete a text-only post in a media-only channel and say why."""
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            return

        try:
            await message.channel.send(
                f"{message.author.mention} that channel only takes messages with an "
                "image, a file or a link.",
                delete_after=10,
            )
        except discord.HTTPException:
            pass

    async def _first_trip(
        self,
        message: discord.Message,
        rows: dict[str, dict[str, Any]],
        enabled: set[str],
        exempt: list[tuple[int, str, str]],
    ) -> Trip | None:
        """Return the first filter this message trips, or ``None``.

        Stops at the first match: one message produces one punishment, not five.
        """
        content = message.content
        guild_id = message.guild.id
        author_id = message.author.id

        def active(name: str) -> bool:
            return name in enabled and not self._is_exempt(message, exempt, name)

        if active("words") and content:
            words = await self.values(guild_id, "words", "word")
            if words:
                whole = rows["words"]["mode"] == "whole"
                hit = F.find_banned_word(content, words, whole_word=whole)
                if hit:
                    return Trip("words", "used a banned word", f"`{hit}`")

        if active("invites") and content:
            codes = F.find_invites(content)
            if codes:
                allowed = await self.values(guild_id, "invites", "allow")
                if rows["invites"]["mode"] == "strict" or not allowed:
                    return Trip("invites", "posted an invite link", f"`{codes[0]}`")
                # Allowlist mode: resolve the invite to see which guild it points
                # at. One API call, only for invites, only when an allowlist is
                # actually configured.
                for code in codes:
                    try:
                        invite = await self.bot.fetch_invite(code, with_counts=False)
                    except (discord.NotFound, discord.HTTPException):
                        return Trip("invites", "posted an unresolvable invite", f"`{code}`")
                    target = str(invite.guild.id) if invite.guild else ""
                    if target not in allowed and target != str(guild_id):
                        return Trip(
                            "invites", "posted an invite to another server", f"`{code}`"
                        )

        if active("links") and content:
            domains = F.extract_domains(content)
            if domains:
                mode = rows["links"]["mode"] or "all"
                if mode == "all":
                    return Trip("links", "posted a link", f"`{domains[0]}`")
                if mode == "allowlist":
                    allowed = await self.values(guild_id, "links", "allow")
                    for domain in domains:
                        if F.domain_matches(domain, allowed) is None:
                            return Trip("links", "posted a link that is not allowed",
                                        f"`{domain}`")
                elif mode == "blocklist":
                    blocked = await self.values(guild_id, "links", "deny")
                    for domain in domains:
                        if F.domain_matches(domain, blocked) is not None:
                            return Trip("links", "posted a blocked link", f"`{domain}`")

        if active("caps") and content:
            row = rows["caps"]
            if len(content) >= max(1, row["param2"]):
                ratio = F.caps_ratio(content)
                if ratio * 100 >= max(1, row["param1"]):
                    return Trip("caps", "used excessive capitals", f"{ratio:.0%} uppercase")

        if active("mentions"):
            total = len(message.mentions) + len(message.role_mentions)
            limit = max(1, rows["mentions"]["param1"])
            if total > limit:
                return Trip("mentions", "mass-mentioned", f"{total} mentions")

        if active("emoji") and content:
            count = F.count_emoji(content)
            limit = max(1, rows["emoji"]["param1"])
            if count > limit:
                return Trip("emoji", "spammed emoji", f"{count} emoji")

        if active("zalgo") and content:
            ratio = F.zalgo_ratio(content)
            if ratio * 100 >= max(1, rows["zalgo"]["param1"]):
                return Trip("zalgo", "posted zalgo text", f"{ratio:.0%} combining marks")

        if active("newlines") and content:
            row = rows["newlines"]
            lines = F.newline_count(content)
            if row["param1"] and lines > row["param1"]:
                return Trip("newlines", "posted a wall of text", f"{lines} line breaks")
            if row["param2"] and len(content) > row["param2"]:
                return Trip(
                    "newlines", "posted a wall of text", f"{len(content)} characters"
                )

        if active("spam"):
            row = rows["spam"]
            count = self.rates.hit(
                guild_id, author_id, "messages", window=max(1, row["param2"])
            )
            if count > max(1, row["param1"]):
                return Trip("spam", "sent messages too quickly",
                            f"{count} in {row['param2']}s")

        if active("duplicates") and content:
            row = rows["duplicates"]
            repeats = self.rates.repeats(
                guild_id, author_id, content, window=max(1, row["param2"])
            )
            if repeats > max(1, row["param1"]):
                return Trip("duplicates", "repeated the same message", f"{repeats} times")

        if active("attachments") and message.attachments:
            row = rows["attachments"]
            blocked = await self.values(guild_id, "attachments", "extension")
            blocked = blocked or frozenset(DEFAULT_BLOCKED_EXTENSIONS)
            for attachment in message.attachments:
                extension = attachment.filename.rsplit(".", 1)[-1].lower()
                if extension in blocked:
                    return Trip(
                        "attachments",
                        "uploaded a blocked file type",
                        f"`.{extension}`",
                        override_action="delete",
                    )
            count = self.rates.hit(
                guild_id, author_id, "attachments",
                window=max(1, row["param2"]), amount=len(message.attachments),
            )
            if count > max(1, row["param1"]):
                return Trip("attachments", "uploaded attachments too quickly",
                            f"{count} in {row['param2']}s")

        if active("stickers") and message.stickers:
            row = rows["stickers"]
            count = self.rates.hit(
                guild_id, author_id, "stickers",
                window=max(1, row["param2"]), amount=len(message.stickers),
            )
            if count > max(1, row["param1"]):
                return Trip("stickers", "spammed stickers", f"{count} in {row['param2']}s")

        if active("scanlinks") and content and self.scanner is not None:
            urls = F.extract_urls(content)
            if urls:
                verdicts = await self.scanner.scan(guild_id, urls)
                threat = next((v for v in verdicts if v.is_threat), None)
                if threat is not None:
                    return Trip(
                        "scanlinks",
                        "posted a malicious link",
                        f"`{threat.domain}` flagged as **{threat.label}** ({threat.source})",
                        override_action="delete",
                    )

        return None

    # =======================================================================
    # Punishment
    # =======================================================================

    async def _punish(
        self, message: discord.Message, trip: Trip, rows: dict[str, dict[str, Any]]
    ) -> None:
        """Delete the message and apply the filter's configured action."""
        guild = message.guild
        member = message.author
        row = rows[trip.name]
        action = trip.override_action or row["action"]

        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

        reason = f"Automod ({self.specs[trip.name].label}): {trip.reason}"

        # Never act against a root owner, whatever a filter decided. Staff are
        # already skipped before any filter runs; this covers the case where the
        # tier lookup itself could not resolve.
        if self.bot.config.is_root_owner(member.id):
            return

        if action != "delete" and member.top_role >= guild.me.top_role:
            log.info(
                "Automod could not punish %s in guild %s: their role is above mine",
                member,
                guild.id,
            )
            action = "delete"

        try:
            if action == "warn":
                await self._issue_warning(guild, member, reason)
            elif action == "mute":
                # Clamped to Discord's own ceiling: a longer timeout is rejected
                # outright, and automod has no business queueing a mute-role
                # punishment behind the scheduler.
                seconds = min(max(60, row["duration"]), MAX_TIMEOUT_DAYS * 86400)
                await member.timeout(timedelta(seconds=seconds), reason=reason)
            elif action == "kick":
                await guild.kick(member, reason=reason)
            elif action == "ban":
                await guild.ban(member, reason=reason, delete_message_seconds=0)
        except discord.Forbidden:
            log.warning("Automod lacks permission to %s in guild %s", action, guild.id)
            action = "delete"
        except discord.HTTPException as exc:
            log.warning("Automod %s failed in guild %s: %s", action, guild.id, exc)
            action = "delete"

        if action != "delete":
            await modlog.create_case(
                self.bot, guild, action="mute" if action == "mute" else action,
                target=member, moderator=guild.me, reason=reason,
            )

        # Announced so cogs/logging.py can render an automod entry without this
        # cog needing to know where any log channel is.
        self.bot.dispatch("automod_action", message, trip, action)

        if await self.bot.guild_config.flag(guild.id, "automod_notify"):
            try:
                await member.send(
                    embed=discord.Embed(
                        title=f"Message removed in {guild.name}",
                        description=f"You {trip.reason}. {trip.detail}".strip(),
                        colour=COLOUR_WARNING,
                    )
                )
            except discord.HTTPException:
                pass

    async def _issue_warning(
        self, guild: discord.Guild, member: discord.Member, reason: str
    ) -> None:
        """Record an automod warning and run the escalation ladder."""
        case = await modlog.create_case(
            self.bot, guild, action="warn", target=member,
            moderator=guild.me, reason=reason,
        )
        await self.bot.db.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at, "
            "source, case_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild.id, member.id, guild.me.id, reason, int(time.time()), "automod", case.id),
        )
        await self.check_escalation(guild, member)

    async def check_escalation(
        self, guild: discord.Guild, member: discord.Member
    ) -> bool:
        """Apply the escalation action if the warning threshold is reached.

        Counts warnings from every source, manual included: someone warned three
        times by a moderator and twice by automod has five warnings.

        Returns:
            True if an escalation was applied.
        """
        config = await self.bot.guild_config.get(guild.id)
        threshold = config.get("automod_warn_threshold") or 0
        if threshold <= 0:
            return False

        window = config.get("automod_warn_window") or 604800
        since = int(time.time()) - int(window)

        count = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM warnings WHERE guild_id = ? AND user_id = ? "
            "AND active = 1 AND created_at >= ?",
            (guild.id, member.id, since),
        )
        if count < threshold:
            return False

        action = config.get("automod_escalation_action") or "mute"
        duration = int(config.get("automod_escalation_duration") or 3600)
        reason = f"Automod escalation: {count} warnings within {format_duration(window)}"

        if member.top_role >= guild.me.top_role:
            log.info("Cannot escalate against %s: role is above mine", member)
            return False

        try:
            if action == "mute":
                await member.timeout(
                    timedelta(seconds=min(duration, MAX_TIMEOUT_DAYS * 86400)),
                    reason=reason,
                )
            elif action == "kick":
                await guild.kick(member, reason=reason)
            elif action == "ban":
                await guild.ban(member, reason=reason, delete_message_seconds=0)
        except discord.HTTPException as exc:
            log.warning("Escalation failed in guild %s: %s", guild.id, exc)
            return False

        expires = int(time.time()) + duration if action in ("mute", "ban") else None
        await modlog.create_case(
            self.bot, guild, action=action, target=member,
            moderator=guild.me, reason=reason, expires_at=expires,
        )
        return True

    # =======================================================================
    # ?automod
    # =======================================================================

    @commands.hybrid_group(name="automod", aliases=["am"], fallback="panel")
    @permissions.admin_only()
    @permissions.guild_only()
    async def automod(self, ctx: commands.Context) -> None:
        """Open the automod control panel."""
        await open_panel(ctx, self)

    @automod.command(name="whitelist", aliases=["exempt"])
    @app_commands.describe(
        action="add, remove or list",
        target="Role, channel or member to exempt",
        filter_name="Optional: exempt from one filter only. Defaults to all",
    )
    @permissions.admin_only()
    async def automod_whitelist(
        self,
        ctx: commands.Context,
        action: str = "list",
        target: Optional[str] = None,
        filter_name: str = "*",
    ) -> None:
        """Exempt roles, channels or members from automod."""
        action = action.lower()
        if action not in {"add", "remove", "list"}:
            raise DeezeeError("Use `add`, `remove` or `list`.")

        if action == "list":
            entries = await self.exemptions(ctx.guild.id)
            lines = [
                f"{self._mention_entity(entity_id, entity_type)} — "
                + ("all filters" if name == "*" else f"`{name}` only")
                for entity_id, entity_type, name in entries
            ]
            pages = paginate_lines(
                lines,
                title="Automod exemptions",
                per_page=10,
                empty_message="Nothing is exempt from automod.",
            )
            await Paginator(pages, ctx.author).start(ctx)
            return

        if target is None:
            raise DeezeeError("Name a role, channel or member to exempt.")
        if filter_name != "*" and filter_name not in self.specs:
            raise DeezeeError(
                f"`{filter_name}` is not a filter. Use one of: {', '.join(self.specs)}."
            )

        entity_id, entity_type = await self._resolve_entity(ctx, target)

        if action == "add":
            await self.bot.guild_config.get(ctx.guild.id)
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO automod_exempt (guild_id, entity_id, entity_type, "
                "filter_name, added_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (ctx.guild.id, entity_id, entity_type, filter_name,
                 ctx.author.id, int(time.time())),
            )
            self.invalidate(ctx.guild.id)
            scope = "all filters" if filter_name == "*" else f"`{filter_name}`"
            await ctx.send(embed=self._ok(
                f"{self._mention_entity(entity_id, entity_type)} is now exempt from {scope}."
            ))
            return

        cursor = await self.bot.db.execute(
            "DELETE FROM automod_exempt WHERE guild_id = ? AND entity_id = ? "
            "AND entity_type = ? AND filter_name = ?",
            (ctx.guild.id, entity_id, entity_type, filter_name),
        )
        self.invalidate(ctx.guild.id)
        if not cursor.rowcount:
            raise DeezeeError("That exemption does not exist.")
        await ctx.send(embed=self._ok("Exemption removed."))

    @automod.command(name="punishment")
    @app_commands.describe(
        filter_name="Which filter to configure",
        action="delete, warn, mute, kick or ban",
        duration="For mute and ban: how long, e.g. 1h",
    )
    @permissions.admin_only()
    async def automod_punishment(
        self,
        ctx: commands.Context,
        filter_name: str,
        action: str,
        duration: Optional[str] = None,
    ) -> None:
        """Set the punishment applied when a filter trips."""
        filter_name = filter_name.lower()
        if filter_name not in self.specs:
            raise DeezeeError(
                f"`{filter_name}` is not a filter. Use one of: {', '.join(self.specs)}."
            )

        await self.set_filter_action(ctx.guild.id, filter_name, action.lower())

        summary = ""
        if duration:
            delta = parse_duration(duration)
            if delta is None:
                raise DeezeeError(f"`{duration}` is not a duration. Try `1h` or `30m`.")
            await self.bot.db.execute(
                "UPDATE automod_filters SET duration = ? WHERE guild_id = ? AND name = ?",
                (int(delta.total_seconds()), ctx.guild.id, filter_name),
            )
            self.invalidate(ctx.guild.id)
            summary = f" for {format_duration(delta)}"

        await ctx.send(embed=self._ok(
            f"**{self.specs[filter_name].label}** now applies **{action.lower()}**{summary}."
        ))

    @automod.command(name="threshold")
    @app_commands.describe(
        count="Warnings that trigger escalation. 0 disables it",
        window="Time window the warnings are counted over, e.g. 7d",
        action="What happens at the threshold: mute, kick or ban",
        duration="For mute and ban: how long",
    )
    @permissions.admin_only()
    async def automod_threshold(
        self,
        ctx: commands.Context,
        count: int,
        window: Optional[str] = None,
        action: Optional[str] = None,
        duration: Optional[str] = None,
    ) -> None:
        """Set how many warnings trigger the escalation punishment."""
        count = max(0, min(count, 100))
        updates: dict[str, Any] = {"automod_warn_threshold": count}

        if window:
            delta = parse_duration(window)
            if delta is None:
                raise DeezeeError(f"`{window}` is not a duration. Try `7d`.")
            updates["automod_warn_window"] = int(delta.total_seconds())

        if action:
            if action.lower() not in {"mute", "kick", "ban"}:
                raise DeezeeError("Escalation must be `mute`, `kick` or `ban`.")
            updates["automod_escalation_action"] = action.lower()

        if duration:
            delta = parse_duration(duration)
            if delta is None:
                raise DeezeeError(f"`{duration}` is not a duration. Try `1h`.")
            updates["automod_escalation_duration"] = int(delta.total_seconds())

        await self.bot.guild_config.set_many(ctx.guild.id, updates)
        config = await self.bot.guild_config.get(ctx.guild.id)

        if count == 0:
            await ctx.send(embed=self._ok("Warning escalation disabled."))
            return

        await ctx.send(embed=self._ok(
            f"**{count}** warnings within "
            f"{format_duration(config['automod_warn_window'])} now trigger "
            f"**{config['automod_escalation_action']}**"
            + (f" for {format_duration(config['automod_escalation_duration'])}"
               if config["automod_escalation_action"] in ("mute", "ban") else "")
            + "."
        ))

    @automod.command(name="test")
    @app_commands.describe(message="Sample text to run through the active filters")
    @permissions.admin_only()
    async def automod_test(self, ctx: commands.Context, *, message: str) -> None:
        """Run a sample message through the filters without punishing anyone.

        Not in any of the source bots. Added because configuring filters blind --
        change a threshold, post something, see if it vanishes -- is the main
        support burden they all share.
        """
        rows = await self.load_filters(ctx.guild.id)
        results: list[str] = []

        for name, spec in self.specs.items():
            row = rows[name]
            if not row["enabled"]:
                results.append(f"{EMOJI_OFF} **{spec.label}** — disabled")
                continue

            verdict = await self._test_one(ctx.guild.id, name, row, message)
            marker = EMOJI_ON if verdict is None else "\N{LARGE ORANGE CIRCLE}"
            outcome = verdict or "would not trip"
            results.append(f"{marker} **{spec.label}** — {outcome}")

        embed = discord.Embed(
            title="Automod test",
            description=f"Sample:\n```\n{message[:500]}\n```",
            colour=COLOUR_INFO,
        )
        embed.add_field(name="Results", value="\n".join(results)[:1024], inline=False)
        embed.set_footer(text="Nothing was deleted and nobody was punished.")
        await ctx.send(embed=embed)

    async def _test_one(
        self, guild_id: int, name: str, row: dict[str, Any], message: str
    ) -> str | None:
        """Evaluate one filter against sample text.

        Only the content-based filters are meaningful here: rate limits depend on
        message history, which a single sample does not have.
        """
        if name == "words":
            words = await self.values(guild_id, "words", "word")
            hit = F.find_banned_word(message, words, whole_word=row["mode"] == "whole")
            return f"**would trip** on `{hit}`" if hit else None
        if name == "invites":
            codes = F.find_invites(message)
            return f"**would trip** on `{codes[0]}`" if codes else None
        if name == "links":
            domains = F.extract_domains(message)
            if not domains:
                return None
            mode = row["mode"] or "all"
            if mode == "all":
                return f"**would trip** on `{domains[0]}`"
            if mode == "allowlist":
                allowed = await self.values(guild_id, "links", "allow")
                bad = [d for d in domains if F.domain_matches(d, allowed) is None]
                return f"**would trip** on `{bad[0]}`" if bad else None
            blocked = await self.values(guild_id, "links", "deny")
            bad = [d for d in domains if F.domain_matches(d, blocked) is not None]
            return f"**would trip** on `{bad[0]}`" if bad else None
        if name == "caps":
            if len(message) < max(1, row["param2"]):
                return None
            ratio = F.caps_ratio(message)
            return (f"**would trip** at {ratio:.0%} uppercase"
                    if ratio * 100 >= max(1, row["param1"]) else None)
        if name == "emoji":
            count = F.count_emoji(message)
            return (f"**would trip** with {count} emoji"
                    if count > max(1, row["param1"]) else None)
        if name == "zalgo":
            ratio = F.zalgo_ratio(message)
            return (f"**would trip** at {ratio:.0%} combining marks"
                    if ratio * 100 >= max(1, row["param1"]) else None)
        if name == "newlines":
            lines = F.newline_count(message)
            if row["param1"] and lines > row["param1"]:
                return f"**would trip** with {lines} line breaks"
            if row["param2"] and len(message) > row["param2"]:
                return f"**would trip** at {len(message)} characters"
            return None
        if name == "scanlinks":
            urls = F.extract_urls(message)
            if not urls or self.scanner is None:
                return None
            verdicts = await self.scanner.scan(guild_id, urls)
            threat = next((v for v in verdicts if v.is_threat), None)
            return (f"**would trip**: `{threat.domain}` is {threat.label}"
                    if threat else "all links trusted or clean")
        return "needs message history — cannot be tested from one sample"

    # =======================================================================
    # ?filter
    # =======================================================================

    @commands.hybrid_group(name="filter")
    @permissions.admin_only()
    @permissions.guild_only()
    async def filter_group(self, ctx: commands.Context) -> None:
        """Show every filter's current state."""
        rows = await self.load_filters(ctx.guild.id)
        lines = [
            f"{EMOJI_ON if rows[name]['enabled'] else EMOJI_OFF} **{spec.label}** "
            f"(`{name}`) — {rows[name]['action']}"
            for name, spec in self.specs.items()
        ]
        embed = discord.Embed(
            title="Filters",
            description="\n".join(lines),
            colour=COLOUR_DEFAULT,
        )
        embed.set_footer(text="?automod opens the panel • ?filter <name> on")
        await ctx.send(embed=embed)

    async def _simple_toggle(
        self, ctx: commands.Context, name: str, state: str
    ) -> bool:
        """Handle the ``on``/``off`` half of a filter subcommand.

        Returns:
            True if the argument was handled here.
        """
        state = state.lower()
        if state not in {"on", "off", "enable", "disable"}:
            return False
        enabled = state in {"on", "enable"}
        await self.set_filter_enabled(ctx.guild.id, name, enabled)
        await ctx.send(embed=self._ok(
            f"**{self.specs[name].label}** {'enabled' if enabled else 'disabled'}."
        ))
        return True

    @filter_group.command(name="words", aliases=["censor", "bannedwords"])
    @app_commands.describe(
        action="on, off, add, remove, list, clear, or mode",
        words="Words to add or remove, comma or space separated",
    )
    @permissions.admin_only()
    async def filter_words(
        self, ctx: commands.Context, action: str = "list", *, words: str = ""
    ) -> None:
        """Manage the banned-word list."""
        if await self._simple_toggle(ctx, "words", action):
            return

        action = action.lower()
        guild_id = ctx.guild.id

        if action == "mode":
            mode = words.strip().lower()
            if mode not in self.specs["words"].modes:
                raise DeezeeError(
                    "Mode must be `wildcard` (matches inside longer words) or "
                    "`whole` (whole words only)."
                )
            await self.set_filter_mode(guild_id, "words", mode)
            await ctx.send(embed=self._ok(f"Word matching set to **{mode}**."))
            return

        if action == "list":
            current = sorted(await self.values(guild_id, "words", "word"))
            rows = await self.load_filters(guild_id)
            pages = paginate_lines(
                [f"`{word}`" for word in current],
                title="Banned words",
                per_page=25,
                description=f"Matching mode: **{rows['words']['mode']}**. "
                f"{len(current)} word(s).",
                empty_message="No banned words configured.",
            )
            await Paginator(pages, ctx.author).start(ctx, ephemeral=True)
            return

        if action == "clear":
            current = await self.values(guild_id, "words", "word")
            if not current:
                raise DeezeeError("The banned-word list is already empty.")
            if not await confirm(
                ctx,
                title=f"Clear {len(current)} banned word(s)?",
                description="The entire banned-word list will be deleted.",
                confirm_label=f"Clear {len(current)}",
            ):
                return
            await self.bot.db.execute(
                "DELETE FROM filter_values WHERE guild_id = ? AND name = 'words'",
                (guild_id,),
            )
            self.invalidate(guild_id)
            await ctx.send(embed=self._ok(f"Cleared {len(current)} banned word(s)."))
            return

        items = [w.strip().lower() for w in words.replace(",", " ").split() if w.strip()]
        if not items:
            raise DeezeeError("Give me at least one word.")

        if action == "add":
            added = await self.add_values(guild_id, "words", "word", items, ctx.author.id)
            await ctx.send(embed=self._ok(
                f"Added **{added}** word(s). {len(items) - added} were already listed."
            ), ephemeral=True)
            return
        if action == "remove":
            removed = await self.remove_values(guild_id, "words", "word", items)
            await ctx.send(embed=self._ok(f"Removed **{removed}** word(s)."), ephemeral=True)
            return

        raise DeezeeError("Use `on`, `off`, `add`, `remove`, `list`, `clear` or `mode`.")

    @filter_group.command(name="invites")
    @app_commands.describe(
        action="on, off, mode, allow, deny or list",
        guild_id="Server ID to allow or deny",
    )
    @permissions.admin_only()
    async def filter_invites(
        self, ctx: commands.Context, action: str = "list", guild_id: Optional[str] = None
    ) -> None:
        """Block Discord invite links, with an allowlist of permitted servers."""
        if await self._simple_toggle(ctx, "invites", action):
            return
        await self._list_filter(
            ctx, "invites", action, guild_id,
            allow_kind="allow", label="allowed server ID",
            modes_help="`strict` blocks every invite; `allowlist` permits listed servers.",
        )

    @filter_group.command(name="links")
    @app_commands.describe(
        action="on, off, mode, allow, deny or list", domain="Domain to allow or deny"
    )
    @permissions.admin_only()
    async def filter_links(
        self, ctx: commands.Context, action: str = "list", domain: Optional[str] = None
    ) -> None:
        """Block all links, or run an allowlist or blocklist of domains."""
        if await self._simple_toggle(ctx, "links", action):
            return
        await self._list_filter(
            ctx, "links", action, domain,
            allow_kind="allow", label="domain",
            modes_help="`all` blocks every link; `allowlist` permits only listed "
                       "domains; `blocklist` blocks only listed domains.",
        )

    async def _list_filter(
        self,
        ctx: commands.Context,
        name: str,
        action: str,
        value: str | None,
        *,
        allow_kind: str,
        label: str,
        modes_help: str,
    ) -> None:
        """Shared handling for the two filters that carry allow and deny lists."""
        action = action.lower()
        guild_id = ctx.guild.id

        if action == "mode":
            mode = (value or "").strip().lower()
            if mode not in self.specs[name].modes:
                raise DeezeeError(f"Mode must be one of: "
                                  f"{', '.join(self.specs[name].modes)}. {modes_help}")
            await self.set_filter_mode(guild_id, name, mode)
            await ctx.send(embed=self._ok(f"**{self.specs[name].label}** mode set to **{mode}**."))
            return

        if action == "list":
            rows = await self.load_filters(guild_id)
            allowed = sorted(await self.values(guild_id, name, allow_kind))
            denied = sorted(await self.values(guild_id, name, "deny"))
            embed = discord.Embed(
                title=self.specs[name].label,
                description=f"Mode: **{rows[name]['mode']}**\n{modes_help}",
                colour=COLOUR_DEFAULT,
            )
            embed.add_field(
                name=f"Allowed ({len(allowed)})",
                value="\n".join(f"`{v}`" for v in allowed[:20]) or "None",
                inline=True,
            )
            embed.add_field(
                name=f"Denied ({len(denied)})",
                value="\n".join(f"`{v}`" for v in denied[:20]) or "None",
                inline=True,
            )
            await ctx.send(embed=embed)
            return

        if action not in {"allow", "deny", "unallow", "undeny"}:
            raise DeezeeError("Use `on`, `off`, `mode`, `allow`, `deny` or `list`.")

        if not value:
            raise DeezeeError(f"Give me a {label}.")

        cleaned = value.strip().lower()
        if name == "links":
            cleaned = F.extract_domain(cleaned)

        if action in {"allow", "deny"}:
            kind = allow_kind if action == "allow" else "deny"
            added = await self.add_values(guild_id, name, kind, [cleaned], ctx.author.id)
            if not added:
                raise DeezeeError(f"`{cleaned}` is already on that list.")
            await ctx.send(embed=self._ok(f"`{cleaned}` added to the {action} list."))
            return

        kind = allow_kind if action == "unallow" else "deny"
        removed = await self.remove_values(guild_id, name, kind, [cleaned])
        if not removed:
            raise DeezeeError(f"`{cleaned}` was not on that list.")
        await ctx.send(embed=self._ok(f"`{cleaned}` removed."))

    async def _threshold_command(
        self, ctx: commands.Context, name: str, action: str, values: list[str]
    ) -> None:
        """Shared ``on``/``off``/numeric handling for threshold-only filters."""
        if await self._simple_toggle(ctx, name, action):
            return

        spec = self.specs[name]
        numbers = [v for v in values if v.isdigit()]
        if not numbers:
            rows = await self.load_filters(ctx.guild.id)
            row = rows[name]
            detail = "\n".join(
                f"{label}: **{row[attr]}**" for attr, label, _ in spec.params
            )
            embed = discord.Embed(
                title=spec.label,
                description=f"{'Enabled' if row['enabled'] else 'Disabled'} • "
                f"punishment **{row['action']}**\n\n{detail}",
                colour=COLOUR_DEFAULT,
            )
            await ctx.send(embed=embed)
            return

        updates = {
            attr: int(number)
            for (attr, _, _), number in zip(spec.params, numbers)
        }
        await self.set_filter_params(ctx.guild.id, name, updates)
        rows = await self.load_filters(ctx.guild.id)
        detail = ", ".join(
            f"{label.lower()} **{rows[name][attr]}**" for attr, label, _ in spec.params
        )
        await ctx.send(embed=self._ok(f"**{spec.label}** set to {detail}."))

    @filter_group.command(name="caps")
    @app_commands.describe(action="on, off, or a percentage", values="percent, min length")
    @permissions.admin_only()
    async def filter_caps(
        self, ctx: commands.Context, action: str = "show", *, values: str = ""
    ) -> None:
        """Delete messages above a percentage of uppercase characters."""
        await self._threshold_command(ctx, "caps", action, [action, *values.split()])

    @filter_group.command(name="spam")
    @app_commands.describe(action="on, off, or a message count", values="count, seconds")
    @permissions.admin_only()
    async def filter_spam(
        self, ctx: commands.Context, action: str = "show", *, values: str = ""
    ) -> None:
        """Rate-limit messages per user."""
        await self._threshold_command(ctx, "spam", action, [action, *values.split()])

    @filter_group.command(name="mentions", aliases=["massmention"])
    @app_commands.describe(action="on, off, or a mention limit", values="limit")
    @permissions.admin_only()
    async def filter_mentions(
        self, ctx: commands.Context, action: str = "show", *, values: str = ""
    ) -> None:
        """Punish messages containing more than N mentions."""
        await self._threshold_command(ctx, "mentions", action, [action, *values.split()])

    @filter_group.command(name="emoji")
    @app_commands.describe(action="on, off, or an emoji limit", values="limit")
    @permissions.admin_only()
    async def filter_emoji(
        self, ctx: commands.Context, action: str = "show", *, values: str = ""
    ) -> None:
        """Punish messages containing more than N emoji."""
        await self._threshold_command(ctx, "emoji", action, [action, *values.split()])

    @filter_group.command(name="attachments", aliases=["attachmentspam"])
    @app_commands.describe(
        action="on, off, extensions, or a count",
        values="count and seconds, or extensions to block",
    )
    @permissions.admin_only()
    async def filter_attachments(
        self, ctx: commands.Context, action: str = "show", *, values: str = ""
    ) -> None:
        """Rate-limit attachments and block dangerous file types."""
        if action.lower() == "extensions":
            items = [
                v.strip().lstrip(".").lower()
                for v in values.replace(",", " ").split()
                if v.strip()
            ]
            if not items:
                current = sorted(
                    await self.values(ctx.guild.id, "attachments", "extension")
                ) or list(DEFAULT_BLOCKED_EXTENSIONS)
                embed = discord.Embed(
                    title="Blocked file extensions",
                    description=" ".join(f"`.{e}`" for e in current),
                    colour=COLOUR_DEFAULT,
                )
                embed.set_footer(
                    text="Defaults are used until you set your own list."
                )
                await ctx.send(embed=embed)
                return
            added = await self.add_values(
                ctx.guild.id, "attachments", "extension", items, ctx.author.id
            )
            await ctx.send(embed=self._ok(f"Blocked **{added}** new extension(s)."))
            return

        await self._threshold_command(ctx, "attachments", action, [action, *values.split()])

    @filter_group.command(name="zalgo")
    @app_commands.describe(action="on, off, or a percentage", values="percent")
    @permissions.admin_only()
    async def filter_zalgo(
        self, ctx: commands.Context, action: str = "show", *, values: str = ""
    ) -> None:
        """Delete messages using excessive combining characters."""
        await self._threshold_command(ctx, "zalgo", action, [action, *values.split()])

    @filter_group.command(name="duplicates", aliases=["repeat"])
    @app_commands.describe(action="on, off, or a repeat limit", values="repeats, seconds")
    @permissions.admin_only()
    async def filter_duplicates(
        self, ctx: commands.Context, action: str = "show", *, values: str = ""
    ) -> None:
        """Punish the same message repeated N times."""
        await self._threshold_command(ctx, "duplicates", action, [action, *values.split()])

    @filter_group.command(name="newlines", aliases=["wall"])
    @app_commands.describe(action="on, off, or a newline limit", values="newlines, characters")
    @permissions.admin_only()
    async def filter_newlines(
        self, ctx: commands.Context, action: str = "show", *, values: str = ""
    ) -> None:
        """Punish wall-of-text messages."""
        await self._threshold_command(ctx, "newlines", action, [action, *values.split()])

    @filter_group.command(name="stickers")
    @app_commands.describe(action="on, off, or a sticker count", values="count, seconds")
    @permissions.admin_only()
    async def filter_stickers(
        self, ctx: commands.Context, action: str = "show", *, values: str = ""
    ) -> None:
        """Block or rate-limit stickers."""
        await self._threshold_command(ctx, "stickers", action, [action, *values.split()])

    # =======================================================================
    # ?scanlinks and ?mediaonly
    # =======================================================================

    @commands.hybrid_command(name="scanlinks", aliases=["linkscan"])
    @app_commands.describe(
        action="on, off, status, trust, untrust or trustlist",
        domain="Domain to trust or untrust",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def scanlinks(
        self, ctx: commands.Context, action: str = "status", domain: Optional[str] = None
    ) -> None:
        """Scan unknown links against Google Safe Browsing.

        Trusted domains are allowed with no lookup at all. Only what matches
        neither the bundled list nor this server's own list reaches the API.
        """
        action = action.lower()
        guild_id = ctx.guild.id

        if action in {"on", "off", "enable", "disable"}:
            await self._simple_toggle(ctx, "scanlinks", action)
            return

        if action == "status":
            rows = await self.load_filters(guild_id)
            row = rows["scanlinks"]
            guild_trusted = await self.bot.db.fetchval(
                "SELECT COUNT(*) FROM trusted_domains WHERE guild_id = ?", (guild_id,)
            )
            cached = await self.bot.db.fetchval(
                "SELECT COUNT(*) FROM scan_cache WHERE expires_at > ?", (int(time.time()),)
            )
            embed = discord.Embed(
                title="Link scanning",
                colour=COLOUR_SUCCESS if row["enabled"] else COLOUR_DEFAULT,
            )
            embed.add_field(
                name="Status",
                value=f"{EMOJI_ON if row['enabled'] else EMOJI_OFF} "
                f"{'enabled' if row['enabled'] else 'disabled'}",
                inline=True,
            )
            embed.add_field(name="Punishment", value=row["action"], inline=True)
            embed.add_field(
                name="Safe Browsing API",
                value="connected" if (self.scanner and self.scanner.has_api)
                else "not configured — using the bundled blocklist only",
                inline=False,
            )
            embed.add_field(
                name="Tier 1 — bundled trusted",
                value=f"{self.scanner.bundled_count if self.scanner else 0} domains",
                inline=True,
            )
            embed.add_field(
                name="Tier 2 — this server",
                value=f"{guild_trusted} domains", inline=True
            )
            embed.add_field(name="Cached verdicts", value=str(cached), inline=True)
            embed.set_footer(
                text="Only domains matching neither trusted tier are ever sent to the API."
            )
            await ctx.send(embed=embed)
            return

        if action == "trustlist":
            rows = await self.bot.db.fetchall(
                "SELECT domain, added_by FROM trusted_domains WHERE guild_id = ? "
                "ORDER BY domain",
                (guild_id,),
            )
            pages = paginate_lines(
                [f"`{r['domain']}` — added by <@{r['added_by']}>" for r in rows],
                title="Trusted domains for this server",
                per_page=15,
                description="These are checked in addition to the bundled list and "
                "are never sent to the API.",
                empty_message="No server-specific trusted domains. The bundled list "
                "still applies.",
            )
            await Paginator(pages, ctx.author).start(ctx)
            return

        if action in {"trust", "untrust"}:
            if not domain:
                raise DeezeeError("Give me a domain, such as `example.com`.")
            cleaned = F.extract_domain(domain)
            if "." not in cleaned:
                raise DeezeeError(f"`{domain}` does not look like a domain.")

            if action == "trust":
                if self.scanner and self.scanner.is_bundled_trusted(cleaned):
                    raise DeezeeError(
                        f"`{cleaned}` is already on the bundled trusted list, so it is "
                        "never scanned anyway."
                    )
                await self.bot.guild_config.get(guild_id)
                cursor = await self.bot.db.execute(
                    "INSERT OR IGNORE INTO trusted_domains (guild_id, domain, added_by, "
                    "created_at) VALUES (?, ?, ?, ?)",
                    (guild_id, cleaned, ctx.author.id, int(time.time())),
                )
                if not cursor.rowcount:
                    raise DeezeeError(f"`{cleaned}` is already trusted here.")
                await ctx.send(embed=self._ok(
                    f"`{cleaned}` is now trusted and will never be scanned."
                ))
                return

            cursor = await self.bot.db.execute(
                "DELETE FROM trusted_domains WHERE guild_id = ? AND domain = ?",
                (guild_id, cleaned),
            )
            if not cursor.rowcount:
                raise DeezeeError(f"`{cleaned}` was not on this server's trusted list.")
            await ctx.send(embed=self._ok(f"`{cleaned}` is no longer trusted."))
            return

        if action == "action":
            if not domain or domain.lower() not in ACTIONS:
                raise DeezeeError(f"Use one of: {', '.join(ACTIONS)}.")
            await self.set_filter_action(guild_id, "scanlinks", domain.lower())
            await ctx.send(embed=self._ok(f"Link scanning now applies **{domain.lower()}**."))
            return

        raise DeezeeError(
            "Use `on`, `off`, `status`, `action`, `trust`, `untrust` or `trustlist`."
        )

    @commands.hybrid_command(name="mediaonly", aliases=["mediachannel"])
    @app_commands.describe(action="add, remove or list", channel="Channel to restrict")
    @permissions.admin_only()
    @permissions.guild_only()
    async def mediaonly(
        self,
        ctx: commands.Context,
        action: str = "list",
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Restrict a channel to messages containing an attachment or a link."""
        action = action.lower()
        guild_id = ctx.guild.id

        if action == "list":
            current = await self.media_channels(guild_id)
            lines = [f"<#{cid}>" for cid in sorted(current)]
            embed = discord.Embed(
                title="Media-only channels",
                description="\n".join(lines) or "No channels are restricted.",
                colour=COLOUR_DEFAULT,
            )
            embed.set_footer(text="Text-only messages are deleted in these channels.")
            await ctx.send(embed=embed)
            return

        channel = channel or ctx.channel
        if action == "add":
            await self.bot.guild_config.get(guild_id)
            cursor = await self.bot.db.execute(
                "INSERT OR IGNORE INTO media_channels (guild_id, channel_id, added_by, "
                "created_at) VALUES (?, ?, ?, ?)",
                (guild_id, channel.id, ctx.author.id, int(time.time())),
            )
            self.invalidate(guild_id)
            if not cursor.rowcount:
                raise DeezeeError(f"{channel.mention} is already media-only.")
            await ctx.send(embed=self._ok(
                f"{channel.mention} now requires an image, file or link. "
                "Moderators are exempt."
            ))
            return

        if action == "remove":
            cursor = await self.bot.db.execute(
                "DELETE FROM media_channels WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel.id),
            )
            self.invalidate(guild_id)
            if not cursor.rowcount:
                raise DeezeeError(f"{channel.mention} is not media-only.")
            await ctx.send(embed=self._ok(f"{channel.mention} is no longer media-only."))
            return

        raise DeezeeError("Use `add`, `remove` or `list`.")

    # =======================================================================
    # Helpers
    # =======================================================================

    @staticmethod
    def _ok(description: str) -> discord.Embed:
        return discord.Embed(
            description=f"{EMOJI_SUCCESS}  {description}", colour=COLOUR_SUCCESS
        )

    @staticmethod
    def _mention_entity(entity_id: int, entity_type: str) -> str:
        if entity_type == "role":
            return f"<@&{entity_id}>"
        if entity_type == "channel":
            return f"<#{entity_id}>"
        return f"<@{entity_id}>"

    async def _resolve_entity(
        self, ctx: commands.Context, raw: str
    ) -> tuple[int, str]:
        """Turn a mention, name or ID into ``(id, type)``.

        Tries role, channel then member. Accepting all three in one argument is
        what keeps ``?automod whitelist add`` from needing three variants.
        """
        for converter, kind in (
            (commands.RoleConverter(), "role"),
            (commands.TextChannelConverter(), "channel"),
            (commands.MemberConverter(), "member"),
        ):
            try:
                resolved = await converter.convert(ctx, raw)
            except commands.BadArgument:
                continue
            return resolved.id, kind

        raise DeezeeError(
            f"Could not find a role, channel or member matching `{raw}`."
        )


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point."""
    await bot.add_cog(Automod(bot))
