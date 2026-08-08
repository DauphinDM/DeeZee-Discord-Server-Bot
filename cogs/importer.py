"""Migrating off Dyno, Carl-bot, Sapphire, MEE6 and Lawliet.

Covers the 9 ``?import`` rows in the command sheet's "Server Config" section.

**The one rule this whole cog is built around:** nothing an import reads goes
anywhere near live configuration on its own. Every command here writes to
``import_draft``. ``?import review`` is the only thing that copies a section
across, and only after an explicit button press, and it snapshots what it
replaced so ``?import undo`` can put it back for seven days.

Deezee never touches the bots it is replacing. It does not remove them, does not
edit their data, and does not ask them for anything they have not already posted
in public. Removing them stays a manual job for whoever runs the server -- which
is the right way round, because it means an import going wrong costs nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
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
    COLOUR_WARNING,
    EMOJI_OFF,
    EMOJI_ON,
    EMOJI_SUCCESS,
    IMPORT_BATCH_SIZE,
    TS_RELATIVE,
)
from core.errors import DeezeeError, ServiceUnavailable
from services.importers import arcane, capture as capture_parser, csvlevels, mee6
from services.importers import modlog as modlog_parser
from services.timeparse import format_duration, parse_duration
from ui.paginator import Paginator, paginate_lines
from ui.panels.importer import ReactionRoleMapper, open_review
from ui.views import confirm

log = logging.getLogger(__name__)

#: Sections a draft can hold, and what each one becomes when applied.
SECTIONS: dict[str, str] = {
    "levels": "XP totals, written into the leveling table",
    "modlog": "Moderation cases, written into the case history",
    "reactionroles": "A self-assign role menu",
    "welcome": "The join message",
    "goodbye": "The leave message",
    "autoresponses": "Automatic replies",
    "words": "The automod banned-word list",
    "logging": "Log channel destinations",
}

#: Longest a ``?import capture`` window may run. Beyond this the listener is
#: sitting on a channel long enough that somebody has forgotten about it.
MAX_CAPTURE_SECONDS = 900

#: Messages recorded in one capture window.
MAX_CAPTURE_MESSAGES = 300

#: Ceiling on a ``?import modlog`` scan.
MAX_MODLOG_SCAN = 5000

#: Bots this migration is aimed at, by application ID prefix name. Used only to
#: report which are still present -- nothing here acts on them.
REPLACED_BOTS = (
    "Dyno", "Carl-bot", "Carlbot", "Sapphire", "Lawliet", "MEE6",
    "Double Counter", "Arcane",
)

#: How long a snapshot stays undoable.
UNDO_WINDOW_DAYS = 7


class Importer(commands.Cog):
    """Staged migration off the bots Deezee replaces."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        #: Channels currently being recorded, and when each window ends.
        self._capturing: dict[int, float] = {}

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
    # Draft storage
    # =======================================================================

    async def stage(
        self,
        guild_id: int,
        section: str,
        payload: Any,
        *,
        source: str,
        unparsed: list[str],
        author_id: int,
    ) -> None:
        """Write a section into the draft, replacing any previous attempt.

        Replacing rather than appending: a re-run after fixing something should
        not double the data, and there is no reading of "two half imports" that
        anybody wants.
        """
        await self.bot.db.execute(
            "INSERT INTO import_draft (guild_id, section, payload, source, unparsed, "
            "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, section) DO UPDATE SET "
            "payload = excluded.payload, source = excluded.source, "
            "unparsed = excluded.unparsed, created_by = excluded.created_by, "
            "created_at = excluded.created_at, applied_at = NULL",
            (
                guild_id, section, json.dumps(payload), source,
                json.dumps(unparsed[:200]), author_id, int(time.time()),
            ),
        )

    async def load_drafts(self, guild_id: int) -> list[dict[str, Any]]:
        rows = await self.bot.db.fetchall(
            "SELECT * FROM import_draft WHERE guild_id = ? ORDER BY created_at DESC",
            (guild_id,),
        )
        return [dict(row) for row in rows]

    async def load_draft(self, guild_id: int, section: str) -> dict[str, Any] | None:
        row = await self.bot.db.fetchone(
            "SELECT * FROM import_draft WHERE guild_id = ? AND section = ?",
            (guild_id, section),
        )
        return dict(row) if row else None

    async def discard_section(self, guild_id: int, section: str) -> None:
        await self.bot.db.execute(
            "DELETE FROM import_draft WHERE guild_id = ? AND section = ?",
            (guild_id, section),
        )

    async def describe_draft(
        self, guild: discord.Guild, row: dict[str, Any]
    ) -> list[str]:
        """Human-readable diff of one staged section against live config."""
        payload = json.loads(row["payload"])
        unparsed = json.loads(row["unparsed"] or "[]")
        section = row["section"]
        lines: list[str] = [SECTIONS.get(section, "Staged data"), ""]

        if section == "levels":
            existing = int(
                await self.bot.db.fetchval(
                    "SELECT COUNT(*) FROM levels WHERE guild_id = ? AND xp > 0",
                    (guild.id,),
                ) or 0
            )
            lines.append(f"**{len(payload)}** member(s) staged.")
            lines.append(f"**{existing}** already have XP here.")
            lines.append(
                "Applying **overwrites** XP for the staged members. Anyone not in "
                "the file keeps what they have."
            )
            preview = payload[:5]
            if preview:
                lines.append("")
                lines.extend(f"<@{uid}> → {xp:,} XP" for uid, xp in preview)

        elif section == "modlog":
            lines.append(f"**{len(payload)}** case(s) recovered.")
            by_action: dict[str, int] = {}
            for case in payload:
                by_action[case["action"]] = by_action.get(case["action"], 0) + 1
            lines.append(
                ", ".join(f"{count} {name}" for name, count in sorted(by_action.items()))
            )
            lines.append(
                "Applying writes them as Deezee cases, keeping the original case "
                "numbers where they were readable."
            )

        elif section == "reactionroles":
            lines.append(f"**{len(payload.get('options', []))}** emoji-to-role pair(s).")

        elif section in {"welcome", "goodbye"}:
            current = await self.bot.guild_config.value(guild.id, f"{section}_message")
            lines.append("**Current:**")
            lines.append(f"```{str(current)[:400]}```")
            lines.append("**Staged:**")
            lines.append(f"```{str(payload)[:400]}```")

        elif section == "autoresponses":
            lines.append(f"**{len(payload)}** autoresponse(s) staged.")
            lines.extend(
                f"`{trigger}` → {response[:60]}" for trigger, response in payload[:6]
            )

        elif section == "words":
            lines.append(f"**{len(payload)}** banned word(s) staged.")
            lines.append(", ".join(f"`{word}`" for word in payload[:20]))

        elif section == "logging":
            lines.append(f"**{len(payload)}** log channel(s) staged.")
            lines.extend(f"{kind} → <#{cid}>" for kind, cid in payload.items())

        else:
            lines.append(f"```{json.dumps(payload)[:600]}```")

        if unparsed:
            lines.append("")
            lines.append(f"**{len(unparsed)}** item(s) could not be read:")
            lines.extend(f"• `{item[:80]}`" for item in unparsed[:5])
            lines.append("They are kept here rather than dropped, for `?import paste`.")

        return lines

    # =======================================================================
    # Applying
    # =======================================================================

    async def snapshot(
        self, guild_id: int, section: str, payload: Any, author_id: int
    ) -> None:
        """Record what a section replaced, for ``?import undo``."""
        await self.bot.db.execute(
            "INSERT INTO config_snapshots (guild_id, section, payload, created_by, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, section, json.dumps(payload), author_id, int(time.time())),
        )
        await self.bot.db.execute(
            "DELETE FROM config_snapshots WHERE created_at < ?",
            (int(time.time()) - UNDO_WINDOW_DAYS * 86400,),
        )

    async def apply_section(
        self, guild: discord.Guild, row: dict[str, Any], author: discord.abc.User
    ) -> str:
        """Copy one staged section into live configuration.

        Returns:
            A one-line summary for the review panel.
        """
        section = row["section"]
        payload = json.loads(row["payload"])

        if section == "levels":
            written = await self._apply_levels(guild.id, payload, author.id)
            summary = f"Wrote XP for **{written}** member(s)."

        elif section == "modlog":
            written = await self._apply_modlog(guild, payload)
            summary = f"Wrote **{written}** case(s) into the history."

        elif section in {"welcome", "goodbye"}:
            key = f"{section}_message"
            previous = await self.bot.guild_config.value(guild.id, key)
            await self.snapshot(guild.id, section, {key: previous}, author.id)
            await self.bot.guild_config.set(guild.id, key, str(payload)[:1800])
            summary = f"The {section} message is live."

        elif section == "autoresponses":
            written = await self._apply_autoresponses(guild.id, payload, author.id)
            summary = f"Added **{written}** autoresponse(s)."

        elif section == "words":
            written = await self._apply_words(guild.id, payload, author.id)
            summary = f"Added **{written}** banned word(s)."

        elif section == "logging":
            written = await self._apply_logging(guild.id, payload, author.id)
            summary = f"Set **{written}** log channel(s)."

        elif section == "reactionroles":
            summary = (
                "Reaction-role menus are created directly by "
                "`?import reactionroles`; there is nothing left to apply here."
            )

        else:
            raise DeezeeError(f"I do not know how to apply the `{section}` section.")

        await self.bot.db.execute(
            "UPDATE import_draft SET applied_at = ? WHERE guild_id = ? AND section = ?",
            (int(time.time()), guild.id, section),
        )
        return summary

    async def _apply_levels(
        self, guild_id: int, payload: list[list[int]], author_id: int
    ) -> int:
        """Write staged XP in batches, snapshotting what it overwrote."""
        ids = [int(entry[0]) for entry in payload]
        previous: dict[str, int] = {}
        if ids:
            placeholders = ", ".join("?" * min(len(ids), 900))
            for start in range(0, len(ids), 900):
                chunk = ids[start : start + 900]
                placeholders = ", ".join("?" * len(chunk))
                for existing in await self.bot.db.fetchall(
                    f"SELECT user_id, xp FROM levels WHERE guild_id = ? "
                    f"AND user_id IN ({placeholders})",
                    (guild_id, *chunk),
                ):
                    previous[str(existing["user_id"])] = int(existing["xp"])
        await self.snapshot(guild_id, "levels", previous, author_id)

        written = 0
        batch: list[tuple[int, int, int]] = []
        for entry in payload:
            batch.append((guild_id, int(entry[0]), max(0, int(entry[1]))))
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

    async def _apply_modlog(
        self, guild: discord.Guild, payload: list[dict[str, Any]]
    ) -> int:
        """Write recovered cases, keeping their original numbers where possible."""
        existing = {
            row["case_number"]
            for row in await self.bot.db.fetchall(
                "SELECT case_number FROM mod_cases WHERE guild_id = ?", (guild.id,)
            )
        }
        highest = max(existing) if existing else 0

        rows: list[tuple[Any, ...]] = []
        for case in payload:
            number = case.get("number")
            if not number or number in existing:
                highest += 1
                number = highest
            existing.add(number)
            rows.append(
                (
                    guild.id, number, case["action"], case["target_id"],
                    case.get("target_tag") or str(case["target_id"]),
                    case.get("moderator_id") or 0,
                    case.get("moderator_tag") or "imported",
                    (case.get("reason") or "") + "  [imported]",
                    case.get("created_at") or int(time.time()),
                    None, 0,
                )
            )

        if rows:
            await self.bot.db.executemany(
                "INSERT INTO mod_cases (guild_id, case_number, action, target_id, "
                "target_tag, moderator_id, moderator_tag, reason, created_at, "
                "expires_at, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            # Move the guild's counter past everything imported, so the next real
            # case does not collide with a recovered number.
            await self.bot.db.execute(
                "UPDATE guild_config SET case_counter = ? WHERE guild_id = ? "
                "AND case_counter < ?",
                (max(existing), guild.id, max(existing)),
            )
        return len(rows)

    async def _apply_autoresponses(
        self, guild_id: int, payload: list[list[str]], author_id: int
    ) -> int:
        written = 0
        for entry in payload:
            trigger, response = entry[0], entry[1]
            cursor = await self.bot.db.execute(
                "INSERT OR IGNORE INTO autoresponses (guild_id, trigger, response, "
                "mode, created_by, created_at) VALUES (?, ?, ?, 'contains', ?, ?)",
                (guild_id, trigger[:100], response[:1900], author_id, int(time.time())),
            )
            written += cursor.rowcount
        tags = self.bot.get_cog("Tags")
        if tags is not None:
            tags.invalidate(guild_id)
        return written

    async def _apply_words(
        self, guild_id: int, payload: list[str], author_id: int
    ) -> int:
        """Add words to the automod list, if that cog is loaded."""
        automod = self.bot.get_cog("Automod")
        if automod is None:
            raise DeezeeError("The automod module is not loaded, so words cannot land.")

        written = 0
        for word in payload:
            cursor = await self.bot.db.execute(
                "INSERT OR IGNORE INTO filter_values (guild_id, name, kind, value, "
                "added_by, created_at) VALUES (?, 'words', 'word', ?, ?, ?)",
                (guild_id, word.lower()[:60], author_id, int(time.time())),
            )
            written += cursor.rowcount
        if hasattr(automod, "invalidate"):
            automod.invalidate(guild_id)
        return written

    async def _apply_logging(
        self, guild_id: int, payload: dict[str, int], author_id: int
    ) -> int:
        """Point log groups at captured channels."""
        mapping = {
            "message": "log_messages_channel_id",
            "member": "log_members_channel_id",
            "role": "log_roles_channel_id",
            "channel": "log_channels_channel_id",
            "voice": "log_voice_channel_id",
            "server": "log_server_channel_id",
            "invite": "log_invites_channel_id",
            "moderation": "mod_log_channel_id",
            "automod": "log_automod_channel_id",
        }
        previous: dict[str, Any] = {}
        updates: dict[str, Any] = {}
        for kind, channel_id in payload.items():
            key = mapping.get(kind)
            if key is None:
                continue
            previous[key] = await self.bot.guild_config.value(guild_id, key)
            updates[key] = int(channel_id)

        if updates:
            await self.snapshot(guild_id, "logging", previous, author_id)
            await self.bot.guild_config.set_many(guild_id, updates)
        return len(updates)

    # =======================================================================
    # Commands
    # =======================================================================

    @commands.hybrid_group(name="import", aliases=["migrate"], fallback="panel")
    @permissions.admin_only()
    @permissions.guild_only()
    async def import_group(self, ctx: commands.Context) -> None:
        """Open the migration panel."""
        drafts = await self.load_drafts(ctx.guild.id)
        embed = discord.Embed(
            title="Migration",
            description=(
                "Pull data and settings off the bots Deezee replaces, into a "
                "staging area that must be reviewed before anything goes live.\n\n"
                "**Nothing here is ever applied automatically, and nothing is ever "
                "deleted.** Deezee does not touch the other bots, does not remove "
                "them, and does not edit their data -- removing them stays a manual "
                "job for you."
            ),
            colour=COLOUR_INFO,
        )
        embed.add_field(
            name="Getting data in",
            value=(
                "`?import levels <mee6|arcane|csv>` — a leaderboard\n"
                "`?import modlog #channel` — rebuild case history from embeds\n"
                "`?import reactionroles <message link>` — adopt a role menu\n"
                "`?import capture #channel 5m` — record the other bots' own output\n"
                "`?import paste <setting>` — type it in by hand"
            ),
            inline=False,
        )
        embed.add_field(
            name="Getting it live",
            value=(
                "`?import review` — diff each section and apply it, one at a time\n"
                "`?import status` — the checklist\n"
                "`?import undo <section>` — roll one back, for "
                f"{UNDO_WINDOW_DAYS} days"
            ),
            inline=False,
        )
        if drafts:
            embed.add_field(
                name=f"Staged now ({len(drafts)})",
                value="\n".join(
                    f"{EMOJI_ON if row['applied_at'] else EMOJI_OFF} "
                    f"**{row['section']}** from {row['source']}"
                    for row in drafts
                ),
                inline=False,
            )
        await ctx.send(embed=embed)

    @import_group.command(name="levels")
    @app_commands.describe(
        source="mee6, arcane or csv",
        attachment="For csv: a file of user_id,xp",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def import_levels(
        self,
        ctx: commands.Context,
        source: str = "csv",
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        """Stage a leveling leaderboard from another bot."""
        choice = source.strip().lower()
        if choice not in {"mee6", "arcane", "csv"}:
            raise DeezeeError("Source must be `mee6`, `arcane` or `csv`.")

        await self._defer(ctx)
        notes: list[str] = []

        if choice == "csv":
            if attachment is None and ctx.message is not None and ctx.message.attachments:
                attachment = ctx.message.attachments[0]
            if attachment is None:
                raise DeezeeError(
                    "Attach a CSV of `user_id,xp` (a `user_id,level` file works too "
                    "if the header says `level`)."
                )
            if attachment.size > 8 * 1024 * 1024:
                raise DeezeeError("That file is larger than 8 MiB.")
            raw = (await attachment.read()).decode("utf-8", errors="replace")
            entries, unparsed = csvlevels.parse(raw)

        elif choice == "mee6":
            entries, unparsed = await self._fetch_mee6(ctx.guild.id)

        else:
            entries, unparsed = await self._fetch_arcane(ctx.guild.id)
            notes = unparsed
            unparsed = []

        if not entries:
            raise DeezeeError(
                "No usable rows were found."
                + ("\n" + "\n".join(notes) if notes else "")
            )

        await self.stage(
            ctx.guild.id, "levels", [[uid, xp] for uid, xp in entries],
            source=choice, unparsed=unparsed, author_id=ctx.author.id,
        )

        embed = self._ok(
            f"Staged **{len(entries)}** member(s) from `{choice}`.\n"
            "**Nothing is live yet.** Run `?import review` to see the diff and apply it."
        )
        if unparsed:
            embed.add_field(
                name=f"{len(unparsed)} row(s) unreadable",
                value="\n".join(f"`{item[:60]}`" for item in unparsed[:5]),
                inline=False,
            )
        for note in notes:
            embed.add_field(name="Note", value=note, inline=False)
        await ctx.send(embed=embed)

    async def _fetch_mee6(self, guild_id: int) -> tuple[list[tuple[int, int]], list[str]]:
        """Pull every page of a MEE6 leaderboard."""
        entries: list[tuple[int, int]] = []
        unparsed: list[str] = []
        timeout = aiohttp.ClientTimeout(total=20)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for page in range(mee6.MAX_PAGES):
                    async with session.get(
                        mee6.URL.format(guild_id=guild_id),
                        params={"page": str(page), "limit": "1000"},
                    ) as response:
                        if response.status != 200:
                            raise ServiceUnavailable(
                                "The MEE6 leaderboard",
                                mee6.describe_status(response.status),
                            )
                        payload = await response.json(content_type=None)

                    page_entries, page_unparsed = mee6.parse_page(payload)
                    unparsed.extend(page_unparsed)
                    if not page_entries:
                        break
                    entries.extend(page_entries)
        except aiohttp.ClientError as exc:
            raise ServiceUnavailable("The MEE6 leaderboard", str(exc)) from exc

        return entries, unparsed

    async def _fetch_arcane(self, guild_id: int) -> tuple[list[tuple[int, int]], list[str]]:
        """Scrape the Arcane leaderboard page, reporting honestly when blocked."""
        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(arcane.URL.format(guild_id=guild_id)) as response:
                    if response.status != 200:
                        raise ServiceUnavailable(
                            "The Arcane leaderboard",
                            f"It answered HTTP {response.status}. Arcane sits behind "
                            "Cloudflare and often refuses a plain request. Use the "
                            "CSV path.",
                        )
                    html = await response.text()
        except aiohttp.ClientError as exc:
            raise ServiceUnavailable("The Arcane leaderboard", str(exc)) from exc

        return arcane.parse(html)

    @import_group.command(name="modlog")
    @app_commands.describe(
        channel="The existing mod-log channel", limit="Messages to scan. Max 5000"
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def import_modlog(
        self, ctx: commands.Context, channel: discord.TextChannel, limit: int = 1000
    ) -> None:
        """Rebuild moderation history by parsing an existing mod-log channel.

        This works because the embeds are already in your server and any bot with
        Read Message History can read them -- no cooperation from the other bot
        is required, and none is asked for.
        """
        limit = max(1, min(limit, MAX_MODLOG_SCAN))
        perms = channel.permissions_for(ctx.guild.me)
        if not perms.read_message_history:
            raise DeezeeError(f"I need Read Message History in {channel.mention}.")

        await self._defer(ctx)

        # Streamed with `async for` and never materialised as a list: five
        # thousand messages held at once is exactly the shape the memory budget
        # cannot afford.
        pairs: list[tuple[dict, int]] = []
        scanned = 0
        async for message in channel.history(limit=limit, oldest_first=True):
            scanned += 1
            for embed in message.embeds:
                pairs.append((embed.to_dict(), int(message.created_at.timestamp())))
            if len(pairs) >= MAX_MODLOG_SCAN:
                break

        cases, unparsed = modlog_parser.parse_many(pairs)
        if not cases:
            raise DeezeeError(
                f"Scanned **{scanned}** message(s) in {channel.mention} and "
                "recognised no cases. Dyno, Carl-bot and Sapphire formats are "
                "understood; anything else needs `?import paste`."
            )

        sources: dict[str, int] = {}
        for case in cases:
            sources[case.source] = sources.get(case.source, 0) + 1

        await self.stage(
            ctx.guild.id,
            "modlog",
            [
                {
                    "action": case.action,
                    "number": case.number,
                    "target_id": case.target_id,
                    "target_tag": case.target_tag,
                    "moderator_id": case.moderator_id,
                    "moderator_tag": case.moderator_tag,
                    "reason": case.reason,
                    "created_at": case.created_at,
                }
                for case in cases
            ],
            source=f"#{channel.name}",
            unparsed=unparsed,
            author_id=ctx.author.id,
        )

        embed = self._ok(
            f"Scanned **{scanned}** message(s) and recovered **{len(cases)}** case(s).\n"
            "**Nothing is live yet.** Run `?import review`."
        )
        embed.add_field(
            name="Recognised as",
            value=", ".join(f"{name}: {count}" for name, count in sources.items()),
            inline=False,
        )
        if unparsed:
            embed.add_field(
                name=f"{len(unparsed)} looked like cases but could not be read",
                value="\n".join(f"• {item[:70]}" for item in unparsed[:5])
                + "\nThey are flagged rather than guessed at.",
                inline=False,
            )
        await ctx.send(embed=embed)

    @import_group.command(name="reactionroles")
    @app_commands.describe(message="Link to the existing reaction-role message")
    @permissions.admin_only()
    @permissions.guild_only()
    async def import_reactionroles(
        self, ctx: commands.Context, message: str
    ) -> None:
        """Adopt an existing reaction-role message.

        The emoji on the message can be read. Which role each one grants lives in
        the other bot's database and cannot be, so a picker per emoji is shown
        and the mapping is set once, by hand.
        """
        try:
            target = await commands.MessageConverter().convert(ctx, message.strip())
        except commands.BadArgument as exc:
            raise DeezeeError(f"I could not find that message. ({exc})") from exc

        emojis = [str(reaction.emoji) for reaction in target.reactions]
        if not emojis:
            raise DeezeeError(
                "That message has no reactions on it, so there is nothing to map."
            )

        view = ReactionRoleMapper(self, ctx.author, target, emojis)
        view.message = await ctx.send(embed=view.embed(ctx.guild), view=view)

    async def adopt_menu(
        self,
        guild: discord.Guild,
        message: discord.Message,
        mapping: dict[str, int],
        *,
        rebuild: bool,
        author: discord.abc.User,
    ) -> str:
        """Create a Deezee menu from a hand-made emoji-to-role mapping."""
        roles_cog = self.bot.get_cog("Roles")
        if roles_cog is None:
            raise DeezeeError("The roles module is not loaded.")
        if not mapping:
            raise DeezeeError("Map at least one emoji to a role first.")

        cursor = await self.bot.db.execute(
            "INSERT INTO reaction_role_menus (guild_id, channel_id, message_id, title, "
            "description, style, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guild.id, message.channel.id,
                None if rebuild else message.id,
                "Self-assignable roles",
                "Adopted from an existing message.",
                "button" if rebuild else "reaction",
                author.id, int(time.time()),
            ),
        )
        menu_id = int(cursor.lastrowid or 0)

        for position, (emoji, role_id) in enumerate(mapping.items()):
            role = guild.get_role(role_id)
            await self.bot.db.execute(
                "INSERT INTO reaction_role_options (menu_id, role_id, emoji, label, "
                "position) VALUES (?, ?, ?, ?, ?)",
                (menu_id, role_id, emoji, (role.name if role else "Role")[:80], position),
            )

        await self.stage(
            guild.id, "reactionroles",
            {"menu_id": menu_id, "options": list(mapping.items())},
            source=f"message {message.id}", unparsed=[], author_id=author.id,
        )
        await self.bot.db.execute(
            "UPDATE import_draft SET applied_at = ? WHERE guild_id = ? "
            "AND section = 'reactionroles'",
            (int(time.time()), guild.id),
        )

        if rebuild:
            posted = await roles_cog.publish_menu(guild, menu_id)
            return (
                f"Rebuilt as buttons: {posted.jump_url}\n\n"
                "The original message is untouched. The other bot may still respond "
                "to it until you turn that off yourself -- Deezee never does that "
                "for you."
            )

        return (
            f"Menu **#{menu_id}** now watches [that message]({message.jump_url}) for "
            f"**{len(mapping)}** emoji.\n\n"
            "The other bot is still watching it too. Two bots granting the same "
            "role is harmless but noisy, so turn its menu off when you are happy "
            "this one works."
        )

    @import_group.command(name="capture")
    @app_commands.describe(
        channel="Channel to record", duration="How long to listen. Max 15m"
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def import_capture(
        self, ctx: commands.Context, channel: discord.TextChannel,
        duration: str = "5m",
    ) -> None:
        """Record a channel while you run the other bots' own config commands.

        Dyno, Carl-bot and Sapphire have no public configuration API, but all
        three will print their current settings into chat when asked. This
        records what they post during the window and parses it afterwards.
        """
        delta = parse_duration(duration)
        if delta is None:
            raise DeezeeError(f"`{duration}` is not a duration. Try `5m`.")
        seconds = min(int(delta.total_seconds()), MAX_CAPTURE_SECONDS)
        if seconds < 30:
            raise DeezeeError("A capture window must be at least 30 seconds.")

        if channel.id in self._capturing:
            raise DeezeeError(f"{channel.mention} is already being recorded.")

        ends_at = time.monotonic() + seconds
        self._capturing[channel.id] = ends_at

        await self.bot.db.execute(
            "DELETE FROM import_capture WHERE guild_id = ?", (ctx.guild.id,)
        )

        await ctx.send(
            embed=discord.Embed(
                title="Recording",
                description=(
                    f"Listening in {channel.mention} for "
                    f"**{format_duration(seconds)}**.\n\n"
                    "Run the other bots' own settings commands now — things like "
                    "`?welcome`, `!autoresponse list`, `.tags`, `?logs`. Whatever "
                    "they print will be parsed when the window closes.\n\n"
                    "**Everything captured lands in the draft, never live.**"
                ),
                colour=COLOUR_WARNING,
            )
        )

        try:
            await asyncio.sleep(seconds)
        finally:
            self._capturing.pop(channel.id, None)

        rows = await self.bot.db.fetchall(
            "SELECT * FROM import_capture WHERE guild_id = ? ORDER BY created_at",
            (ctx.guild.id,),
        )
        messages = [
            (
                row["author_name"],
                row["content"],
                json.loads(row["embeds"] or "[]"),
            )
            for row in rows
        ]
        result = capture_parser.parse_messages(messages)

        if result.is_empty:
            await ctx.send(
                embed=discord.Embed(
                    title="Nothing recognised",
                    description=(
                        f"Recorded **{len(messages)}** message(s) but recognised no "
                        "settings in them.\n\nThe other bots change their output "
                        "formats, and some settings are simply never printed. Use "
                        "`?import paste <setting>` to type one in by hand."
                    ),
                    colour=COLOUR_DEFAULT,
                )
            )
            return

        staged: list[str] = []
        if result.welcome:
            await self.stage(ctx.guild.id, "welcome", result.welcome,
                             source="capture", unparsed=result.unparsed,
                             author_id=ctx.author.id)
            staged.append("welcome")
        if result.goodbye:
            await self.stage(ctx.guild.id, "goodbye", result.goodbye,
                             source="capture", unparsed=result.unparsed,
                             author_id=ctx.author.id)
            staged.append("goodbye")
        if result.autoresponses:
            await self.stage(ctx.guild.id, "autoresponses",
                             [list(pair) for pair in result.autoresponses],
                             source="capture", unparsed=result.unparsed,
                             author_id=ctx.author.id)
            staged.append("autoresponses")
        if result.banned_words:
            await self.stage(ctx.guild.id, "words", result.banned_words,
                             source="capture", unparsed=result.unparsed,
                             author_id=ctx.author.id)
            staged.append("words")
        if result.log_channels:
            await self.stage(ctx.guild.id, "logging", result.log_channels,
                             source="capture", unparsed=result.unparsed,
                             author_id=ctx.author.id)
            staged.append("logging")

        embed = self._ok(
            f"Recorded **{len(messages)}** message(s) and staged: "
            + ", ".join(f"**{name}**" for name in staged)
            + ".\n**Nothing is live yet.** Run `?import review`."
        )
        embed.add_field(
            name="Found", value="\n".join(f"• {line}" for line in result.summary()),
            inline=False,
        )
        if result.untranslated:
            embed.add_field(
                name="Variables with no Deezee equivalent",
                value=", ".join(f"`{v}`" for v in result.untranslated[:10])
                + "\nThey were left as written rather than guessed at. "
                "`?variables` lists what Deezee understands.",
                inline=False,
            )
        if result.unparsed:
            embed.add_field(
                name=f"{len(result.unparsed)} message(s) not recognised",
                value="\n".join(f"• {item[:70]}" for item in result.unparsed[:3]),
                inline=False,
            )
        await ctx.send(embed=embed)

    @commands.Cog.listener("on_message")
    async def record_capture(self, message: discord.Message) -> None:
        """Store messages posted in a channel currently being recorded."""
        if message.guild is None:
            return
        ends_at = self._capturing.get(message.channel.id)
        if ends_at is None or time.monotonic() > ends_at:
            return

        stored = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM import_capture WHERE guild_id = ?",
            (message.guild.id,),
        )
        if (stored or 0) >= MAX_CAPTURE_MESSAGES:
            return

        await self.bot.db.execute(
            "INSERT INTO import_capture (guild_id, channel_id, author_id, "
            "author_name, content, embeds, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                message.guild.id, message.channel.id, message.author.id,
                str(message.author), message.content[:3000],
                json.dumps([embed.to_dict() for embed in message.embeds])[:6000],
                int(time.time()),
            ),
        )

    @import_group.command(name="paste")
    @app_commands.describe(
        setting="welcome, goodbye, autoresponses or words",
        content="The text. For autoresponses use 'trigger | response' per line",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def import_paste(
        self, ctx: commands.Context, setting: str, *, content: str
    ) -> None:
        """Paste a setting's raw text straight into the draft.

        The fallback for anything ``?import capture`` cannot read. Variables from
        the source bot are translated where they map, and flagged where they do
        not.
        """
        section = setting.strip().lower()
        if section not in {"welcome", "goodbye", "autoresponses", "words"}:
            raise DeezeeError(
                "Setting must be `welcome`, `goodbye`, `autoresponses` or `words`."
            )

        body = content.strip()
        if body.startswith("```"):
            body = body.strip("`")
            if "\n" in body:
                body = body.split("\n", 1)[1]

        if section in {"welcome", "goodbye"}:
            translated, untranslated = capture_parser.translate_variables(body)
            await self.stage(ctx.guild.id, section, translated, source="paste",
                             unparsed=untranslated, author_id=ctx.author.id)
            embed = self._ok(
                f"Staged the **{section}** message. **Not live yet** — "
                "`?import review` to apply it."
            )
            embed.add_field(name="Result", value=f"```{translated[:900]}```",
                            inline=False)
            if untranslated:
                embed.add_field(
                    name="No Deezee equivalent",
                    value=", ".join(f"`{v}`" for v in untranslated)
                    + "\nLeft exactly as written. `?variables` lists what is understood.",
                    inline=False,
                )
            await ctx.send(embed=embed)
            return

        if section == "autoresponses":
            pairs: list[list[str]] = []
            bad: list[str] = []
            for line in body.splitlines():
                if "|" not in line:
                    if line.strip():
                        bad.append(line.strip()[:80])
                    continue
                trigger, _, response = line.partition("|")
                translated, _ = capture_parser.translate_variables(response.strip())
                if trigger.strip() and translated:
                    pairs.append([trigger.strip()[:100], translated[:1900]])
            if not pairs:
                raise DeezeeError(
                    "Nothing usable. One per line, as `trigger | response`."
                )
            await self.stage(ctx.guild.id, "autoresponses", pairs, source="paste",
                             unparsed=bad, author_id=ctx.author.id)
            await ctx.send(
                embed=self._ok(
                    f"Staged **{len(pairs)}** autoresponse(s). `?import review` to "
                    "apply them."
                )
            )
            return

        words = [
            word.strip().lower()
            for chunk in body.replace("\n", ",").split(",")
            if 2 <= len(word := chunk.strip()) <= 40
        ]
        if not words:
            raise DeezeeError("No usable words. Separate them with commas or newlines.")
        await self.stage(ctx.guild.id, "words", sorted(set(words)), source="paste",
                         unparsed=[], author_id=ctx.author.id)
        await ctx.send(
            embed=self._ok(
                f"Staged **{len(set(words))}** banned word(s). `?import review` to "
                "apply them."
            )
        )

    @import_group.command(name="review")
    @app_commands.describe(section="Optional: open straight to one section")
    @permissions.admin_only()
    @permissions.guild_only()
    async def import_review(
        self, ctx: commands.Context, section: Optional[str] = None
    ) -> None:
        """Diff the staged draft against live configuration, then apply or discard.

        The only command that writes imported data into live config, and it does
        it one section at a time.
        """
        await open_review(ctx, self, section)

    @import_group.command(name="status")
    @permissions.admin_only()
    @permissions.guild_only()
    async def import_status(self, ctx: commands.Context) -> None:
        """The migration checklist, and which old bots are still here."""
        drafts = {row["section"]: row for row in await self.load_drafts(ctx.guild.id)}

        lines = []
        for section, description in SECTIONS.items():
            row = drafts.get(section)
            if row is None:
                lines.append(f"{EMOJI_OFF} **{section}** — not imported")
            elif row["applied_at"]:
                lines.append(
                    f"{EMOJI_ON} **{section}** — applied "
                    f"<t:{row['applied_at']}:{TS_RELATIVE}>"
                )
            else:
                lines.append(
                    f"\N{LARGE YELLOW CIRCLE} **{section}** — staged, "
                    "**not yet applied**"
                )

        embed = discord.Embed(
            title="Migration status",
            description="\n".join(lines),
            colour=COLOUR_INFO,
        )

        present = [
            member for member in ctx.guild.members
            if member.bot and any(name.lower() in member.name.lower()
                                  for name in REPLACED_BOTS)
        ]
        if present:
            verdicts = []
            for member in present:
                done = self._verdict_for(member.name, drafts)
                verdicts.append(
                    f"{EMOJI_ON if done else EMOJI_OFF} **{member.name}** — "
                    + ("safe to remove" if done else "still holds data you have not imported")
                )
            embed.add_field(
                name="Old bots still in this server",
                value="\n".join(verdicts)
                + "\n\nDeezee never removes them. That stays your decision.",
                inline=False,
            )
        else:
            embed.add_field(
                name="Old bots",
                value="None of the bots Deezee replaces are in this server.",
                inline=False,
            )

        embed.add_field(
            name="No automatic path exists for",
            value=(
                "• **VPN/proxy/Tor detection** — Discord never gives a bot a "
                "member's IP\n"
                "• **Per-command permission trees** from Sapphire — no public API\n"
                "• **Another bot's warning history** unless it posted to a mod-log\n"
                "These need `?import paste` or setting up fresh."
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    @staticmethod
    def _verdict_for(bot_name: str, drafts: dict[str, Any]) -> bool:
        """Whether everything a given bot held has been imported and applied."""
        lowered = bot_name.lower()
        needed: tuple[str, ...]
        if "mee6" in lowered or "arcane" in lowered:
            needed = ("levels",)
        elif "double counter" in lowered:
            needed = ()  # Nothing importable exists; see the note in ?import status.
        else:
            needed = ("modlog", "welcome")
        return all(
            section in drafts and drafts[section]["applied_at"] for section in needed
        ) if needed else False

    @import_group.command(name="undo")
    @app_commands.describe(section="Which applied section to roll back")
    @permissions.admin_only()
    @permissions.guild_only()
    async def import_undo(self, ctx: commands.Context, section: str) -> None:
        """Roll an applied section back to what preceded it."""
        chosen = section.strip().lower()
        row = await self.bot.db.fetchone(
            "SELECT * FROM config_snapshots WHERE guild_id = ? AND section = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (ctx.guild.id, chosen),
        )
        if row is None:
            raise DeezeeError(
                f"There is no snapshot for `{chosen}`. Snapshots are kept for "
                f"{UNDO_WINDOW_DAYS} days after an apply, and only sections that "
                "replace existing settings produce one."
            )

        payload = json.loads(row["payload"])
        age = int(time.time()) - row["created_at"]

        proceed = await confirm(
            ctx,
            title=f"Roll back the {chosen} import?",
            description=(
                f"**{len(payload)}** value(s) return to what they were "
                f"{format_duration(age)} ago, before the import was applied.\n\n"
                "Anything changed since then is overwritten too -- the snapshot is "
                "a picture of one moment, not a merge."
            ),
            confirm_label="Roll it back",
        )
        if not proceed:
            return

        if chosen == "levels":
            batch = [
                (ctx.guild.id, int(user_id), int(xp)) for user_id, xp in payload.items()
            ]
            if batch:
                await self._flush_levels(batch)
            restored = len(batch)
        else:
            writable = self.bot.guild_config.settings
            updates = {k: v for k, v in payload.items() if k in writable}
            if updates:
                await self.bot.guild_config.set_many(ctx.guild.id, updates)
            restored = len(updates)

        await self.bot.db.execute(
            "DELETE FROM config_snapshots WHERE id = ?", (row["id"],)
        )
        await self.bot.db.execute(
            "UPDATE import_draft SET applied_at = NULL WHERE guild_id = ? "
            "AND section = ?",
            (ctx.guild.id, chosen),
        )
        await ctx.send(
            embed=self._ok(
                f"Rolled back **{restored}** value(s) in the `{chosen}` section. "
                "The staged data is still there if you want to apply it again."
            )
        )

    @commands.command(name="importsections")
    @permissions.admin_only()
    @permissions.guild_only()
    async def importsections(self, ctx: commands.Context) -> None:
        """List the draft sections and what applying each one does.

        Prefix-only, and not one of the sheet's rows: it is the reference table
        the other nine commands refer to.
        """
        lines = [f"**{name}** — {meaning}" for name, meaning in SECTIONS.items()]
        pages = paginate_lines(
            lines, title="Import sections", per_page=15,
            description="Every one is staged first and applied only from "
            "`?import review`.",
        )
        await Paginator(pages, ctx.author).start(ctx)


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point."""
    await bot.add_cog(Importer(bot))
