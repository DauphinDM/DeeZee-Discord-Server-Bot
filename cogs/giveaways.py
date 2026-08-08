"""Giveaways and scheduled messages.

Covers the 7 commands in the command sheet's "Giveaways & Events" section.

Entry is a persistent button whose ``custom_id`` carries the giveaway ID, so a
giveaway running across a restart keeps working with no state in memory. The
draw happens through the same scheduler as timed punishments, which means a
restart cannot swallow one either.

Requirements are checked **twice** -- when someone enters, and again at the draw.
A member who qualified on Monday and lost the role on Tuesday should not win on
Wednesday, and only re-checking catches that.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import permissions
from core.constants import (
    COLOUR_INFO,
    COLOUR_MUTED,
    COLOUR_SUCCESS,
    COLOUR_WARNING,
    EMOJI_SUCCESS,
    TS_LONG_DATE_TIME,
    TS_RELATIVE,
)
from core.errors import DeezeeError
from services import levelcurve
from services.timeparse import format_duration, parse_duration
from ui.paginator import Paginator, paginate_lines
from ui.views import confirm

log = logging.getLogger(__name__)

#: Longest a giveaway may run. Beyond this the entry list outlives the interest.
MAX_GIVEAWAY_SECONDS = 60 * 86400

#: Shortest repeat interval for a scheduled message. Below this it is spam, and
#: it is also a scheduler row firing constantly.
MIN_SCHEDULE_INTERVAL = 600


class GiveawayEntryButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gw:enter:(?P<id>[0-9]+)",
):
    """The entry button. Matched by pattern on every boot."""

    def __init__(self, giveaway_id: int, entries: int = 0) -> None:
        super().__init__(
            discord.ui.Button(
                label=f"Enter ({entries})" if entries else "Enter",
                emoji="\N{PARTY POPPER}",
                style=discord.ButtonStyle.success,
                custom_id=f"gw:enter:{giveaway_id}",
            )
        )
        self.giveaway_id = giveaway_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str],
    ) -> GiveawayEntryButton:
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Giveaways")
        if cog is None:
            await interaction.response.send_message(
                "Giveaways are unavailable right now.", ephemeral=True
            )
            return
        await cog.handle_entry(interaction, self.giveaway_id)


class Giveaways(commands.Cog):
    """Giveaways with entry requirements, and scheduled messages."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.scheduler.register("giveaway_end", self._scheduled_end)
        self.bot.scheduler.register("scheduled_message", self._scheduled_message)

    async def cog_unload(self) -> None:
        for action in ("giveaway_end", "scheduled_message"):
            self.bot.scheduler.unregister(action)

    def register_persistent_views(self, bot: Any) -> None:
        """Re-attach entry buttons to giveaways posted before the restart."""
        bot.add_dynamic_items(GiveawayEntryButton)

    @staticmethod
    def _ok(description: str) -> discord.Embed:
        return discord.Embed(
            description=f"{EMOJI_SUCCESS}  {description}", colour=COLOUR_SUCCESS
        )

    # =======================================================================
    # Rendering
    # =======================================================================

    async def entry_count(self, giveaway_id: int) -> int:
        return int(
            await self.bot.db.fetchval(
                "SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id = ?",
                (giveaway_id,),
            )
            or 0
        )

    async def giveaway_embed(self, row: dict[str, Any]) -> discord.Embed:
        """Render a giveaway in its current state."""
        entries = await self.entry_count(row["id"])
        ended = row["status"] != "pending"

        embed = discord.Embed(
            title=f"\N{PARTY POPPER}  {row['prize']}",
            colour=COLOUR_MUTED if ended else COLOUR_WARNING,
        )
        embed.add_field(
            name="Winners", value=str(row["winners"]), inline=True
        )
        embed.add_field(name="Entries", value=str(entries), inline=True)
        embed.add_field(
            name="Ends" if not ended else "Ended",
            value=f"<t:{row['ends_at']}:{TS_LONG_DATE_TIME}>\n"
            f"(<t:{row['ends_at']}:{TS_RELATIVE}>)",
            inline=True,
        )

        requirements = []
        if row["required_role_id"]:
            requirements.append(f"Hold <@&{row['required_role_id']}>")
        if row["required_level"]:
            requirements.append(f"Reach level **{row['required_level']}**")
        if row["required_messages"]:
            requirements.append(f"Have sent **{row['required_messages']}** messages")
        if requirements:
            embed.add_field(
                name="Requirements",
                value="\n".join(f"• {r}" for r in requirements)
                + "\n\nChecked when you enter **and** again at the draw.",
                inline=False,
            )

        if row["status"] == "cancelled":
            embed.add_field(name="Status", value="**Cancelled** -- no winners drawn.",
                            inline=False)
        elif row["status"] == "ended":
            winners = await self.bot.db.fetchall(
                "SELECT user_id FROM giveaway_winners WHERE giveaway_id = ?",
                (row["id"],),
            )
            embed.add_field(
                name="Winners",
                value=", ".join(f"<@{w['user_id']}>" for w in winners)
                or "Nobody qualified.",
                inline=False,
            )

        embed.set_footer(text=f"Giveaway #{row['id']} • hosted by {row['created_by']}")
        return embed

    async def refresh_message(self, row: dict[str, Any]) -> None:
        """Redraw a giveaway's message in place."""
        guild = self.bot.get_guild(row["guild_id"])
        if guild is None or not row["message_id"]:
            return
        channel = guild.get_channel(row["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            message = await channel.fetch_message(row["message_id"])
        except (discord.NotFound, discord.Forbidden):
            return

        if row["status"] == "pending":
            view = discord.ui.View(timeout=None)
            view.add_item(
                GiveawayEntryButton(row["id"], await self.entry_count(row["id"]))
            )
        else:
            view = None

        try:
            await message.edit(embed=await self.giveaway_embed(row), view=view)
        except discord.HTTPException:
            pass

    async def fetch(self, guild_id: int, reference: str) -> dict[str, Any]:
        """Find a giveaway by its ID or by the message it was posted as."""
        cleaned = reference.strip().rsplit("/", 1)[-1]
        if not cleaned.isdigit():
            raise DeezeeError(
                "Give the giveaway number from `?giveaway list`, or the message ID."
            )
        value = int(cleaned)

        row = await self.bot.db.fetchone(
            "SELECT * FROM giveaways WHERE guild_id = ? AND (id = ? OR message_id = ?)",
            (guild_id, value, value),
        )
        if row is None:
            raise DeezeeError(f"No giveaway matches `{reference}` in this server.")
        return dict(row)

    # =======================================================================
    # Requirements
    # =======================================================================

    async def check_requirements(
        self, member: discord.Member, row: dict[str, Any]
    ) -> list[str]:
        """Return the reasons a member does not qualify. Empty means they do."""
        failures: list[str] = []

        if row["required_role_id"]:
            if not any(r.id == row["required_role_id"] for r in member.roles):
                failures.append(f"You need <@&{row['required_role_id']}>.")

        if row["required_level"] or row["required_messages"]:
            level_row = await self.bot.db.fetchone(
                "SELECT xp, messages FROM levels WHERE guild_id = ? AND user_id = ?",
                (member.guild.id, member.id),
            )
            xp = int(level_row["xp"]) if level_row else 0
            messages = int(level_row["messages"]) if level_row else 0

            if row["required_level"]:
                level = levelcurve.level_from_xp(xp)
                if level < row["required_level"]:
                    failures.append(
                        f"You are level **{level}**; this needs level "
                        f"**{row['required_level']}**."
                    )
            if row["required_messages"] and messages < row["required_messages"]:
                failures.append(
                    f"You have sent **{messages}** messages; this needs "
                    f"**{row['required_messages']}**."
                )

        return failures

    async def handle_entry(
        self, interaction: discord.Interaction, giveaway_id: int
    ) -> None:
        """Enter or withdraw from a giveaway."""
        row = await self.bot.db.fetchone(
            "SELECT * FROM giveaways WHERE id = ?", (giveaway_id,)
        )
        if row is None:
            await interaction.response.send_message(
                "That giveaway no longer exists.", ephemeral=True
            )
            return
        if row["status"] != "pending":
            await interaction.response.send_message(
                f"That giveaway is already **{row['status']}**.", ephemeral=True
            )
            return

        member = interaction.user
        if not isinstance(member, discord.Member):
            return

        existing = await self.bot.db.fetchone(
            "SELECT 1 FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ?",
            (giveaway_id, member.id),
        )
        if existing:
            await self.bot.db.execute(
                "DELETE FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ?",
                (giveaway_id, member.id),
            )
            await interaction.response.send_message(
                "You have withdrawn from this giveaway.", ephemeral=True
            )
            await self.refresh_message(dict(row))
            return

        failures = await self.check_requirements(member, dict(row))
        if failures:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="You do not qualify yet",
                    description="\n".join(f"• {f}" for f in failures),
                    colour=COLOUR_WARNING,
                ),
                ephemeral=True,
            )
            return

        await self.bot.db.execute(
            "INSERT OR IGNORE INTO giveaway_entries (giveaway_id, user_id, entered_at) "
            "VALUES (?, ?, ?)",
            (giveaway_id, member.id, int(time.time())),
        )
        await interaction.response.send_message(
            "You are entered. Press again to withdraw.", ephemeral=True
        )
        await self.refresh_message(dict(row))

    # =======================================================================
    # Drawing
    # =======================================================================

    async def draw(
        self, row: dict[str, Any], count: int, *, exclude_previous: bool = True
    ) -> tuple[list[int], int]:
        """Pick winners, re-checking requirements as they are drawn.

        Returns:
            ``(winner_ids, disqualified)``. Disqualified counts entrants who no
            longer meet the requirements they met when they entered.
        """
        guild = self.bot.get_guild(row["guild_id"])
        if guild is None:
            return [], 0

        entries = [
            entry["user_id"]
            for entry in await self.bot.db.fetchall(
                "SELECT user_id FROM giveaway_entries WHERE giveaway_id = ?",
                (row["id"],),
            )
        ]

        if exclude_previous:
            previous = {
                w["user_id"]
                for w in await self.bot.db.fetchall(
                    "SELECT user_id FROM giveaway_winners WHERE giveaway_id = ?",
                    (row["id"],),
                )
            }
            entries = [uid for uid in entries if uid not in previous]

        random.shuffle(entries)

        winners: list[int] = []
        disqualified = 0
        for user_id in entries:
            if len(winners) >= count:
                break
            member = await self.bot.get_or_fetch_member(guild, user_id)
            if member is None:
                disqualified += 1  # They left the server.
                continue
            if await self.check_requirements(member, row):
                disqualified += 1
                continue
            winners.append(user_id)

        if winners:
            await self.bot.db.executemany(
                "INSERT OR IGNORE INTO giveaway_winners (giveaway_id, user_id, "
                "drawn_at) VALUES (?, ?, ?)",
                [(row["id"], uid, int(time.time())) for uid in winners],
            )

        return winners, disqualified

    async def finish(self, row: dict[str, Any]) -> None:
        """End a giveaway, draw its winners and announce them."""
        await self.bot.db.execute(
            "UPDATE giveaways SET status = 'ended' WHERE id = ?", (row["id"],)
        )
        row = dict(row)
        row["status"] = "ended"

        winners, disqualified = await self.draw(row, row["winners"])
        await self.refresh_message(row)

        guild = self.bot.get_guild(row["guild_id"])
        if guild is None:
            return
        channel = guild.get_channel(row["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return

        if winners:
            body = (
                "Congratulations "
                + ", ".join(f"<@{uid}>" for uid in winners)
                + f" — you won **{row['prize']}**!"
            )
        else:
            body = (
                f"Nobody qualified for **{row['prize']}**. "
                "Reroll with `?giveaway reroll` once there are eligible entries."
            )
        if disqualified:
            body += (
                f"\n\n*{disqualified} entrant(s) were skipped: they left, or no "
                "longer meet the requirements.*"
            )

        jump = ""
        if row["message_id"]:
            jump = (
                f"\nhttps://discord.com/channels/{guild.id}/{channel.id}/"
                f"{row['message_id']}"
            )

        try:
            await channel.send(
                embed=discord.Embed(
                    title="Giveaway ended",
                    description=body + jump,
                    colour=COLOUR_SUCCESS if winners else COLOUR_MUTED,
                ),
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            pass

    async def _scheduled_end(self, guild_id: int, payload: dict[str, Any]) -> None:
        """End a giveaway when its timer comes due."""
        row = await self.bot.db.fetchone(
            "SELECT * FROM giveaways WHERE id = ?", (int(payload.get("giveaway_id", 0)),)
        )
        if row is None or row["status"] != "pending":
            return
        await self.finish(dict(row))

    # =======================================================================
    # Commands
    # =======================================================================

    @commands.hybrid_group(name="giveaway", aliases=["g", "gw"], fallback="list")
    @permissions.admin_only()
    @permissions.guild_only()
    async def giveaway(self, ctx: commands.Context) -> None:
        """List active giveaways with their end times and entry counts."""
        rows = await self.bot.db.fetchall(
            "SELECT * FROM giveaways WHERE guild_id = ? AND status = 'pending' "
            "ORDER BY ends_at",
            (ctx.guild.id,),
        )
        lines = []
        for row in rows:
            entries = await self.entry_count(row["id"])
            lines.append(
                f"**#{row['id']}** {row['prize']}\n"
                f"{row['winners']} winner(s) • {entries} entr(ies) • "
                f"ends <t:{row['ends_at']}:{TS_RELATIVE}> in <#{row['channel_id']}>"
            )
        pages = paginate_lines(
            lines,
            title="Active giveaways",
            per_page=6,
            description="`?giveaway start <duration> <winners> <prize>`",
            empty_message="No giveaways are running.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @giveaway.command(name="start", aliases=["gstart"])
    @app_commands.describe(
        duration="How long it runs, e.g. 2h, 3d",
        winners="How many winners to draw",
        prize="What is being given away",
        role="Optional: a role entrants must hold",
        level="Optional: a level entrants must have reached",
        messages="Optional: how many messages entrants must have sent",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def giveaway_start(
        self,
        ctx: commands.Context,
        duration: str,
        winners: int,
        prize: str,
        role: Optional[discord.Role] = None,
        level: int = 0,
        messages: int = 0,
    ) -> None:
        """Launch a giveaway with entry requirements."""
        delta = parse_duration(duration)
        if delta is None:
            raise DeezeeError(f"`{duration}` is not a duration. Try `2h` or `3d`.")
        if delta.total_seconds() > MAX_GIVEAWAY_SECONDS:
            raise DeezeeError(
                f"A giveaway can run for at most "
                f"{format_duration(MAX_GIVEAWAY_SECONDS)}."
            )
        if not 1 <= winners <= 50:
            raise DeezeeError("Winners must be between 1 and 50.")
        if level and not await self.bot.guild_config.flag(ctx.guild.id, "xp_enabled"):
            raise DeezeeError(
                "A level requirement needs leveling switched on -- otherwise nobody "
                "has a level and nobody can qualify. Turn it on in `?levelconfig`."
            )

        perms = ctx.channel.permissions_for(ctx.guild.me)
        if not perms.send_messages or not perms.embed_links:
            raise DeezeeError("I need Send Messages and Embed Links here.")

        ends_at = int(time.time() + delta.total_seconds())
        cursor = await self.bot.db.execute(
            "INSERT INTO giveaways (guild_id, channel_id, prize, winners, ends_at, "
            "required_role_id, required_level, required_messages, created_by, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ctx.guild.id, ctx.channel.id, prize[:200], winners, ends_at,
                role.id if role else None, max(0, level), max(0, messages),
                ctx.author.id, int(time.time()),
            ),
        )
        giveaway_id = int(cursor.lastrowid or 0)
        row = dict(
            await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id = ?",
                                       (giveaway_id,))
        )

        view = discord.ui.View(timeout=None)
        view.add_item(GiveawayEntryButton(giveaway_id))
        message = await ctx.channel.send(
            embed=await self.giveaway_embed(row), view=view
        )
        await self.bot.db.execute(
            "UPDATE giveaways SET message_id = ? WHERE id = ?", (message.id, giveaway_id)
        )
        await self.bot.scheduler.schedule(
            ctx.guild.id, "giveaway_end", ends_at, {"giveaway_id": giveaway_id}
        )

        if ctx.interaction is not None:
            await ctx.send(
                embed=self._ok(f"Giveaway **#{giveaway_id}** started: {message.jump_url}"),
                ephemeral=True,
            )

    @giveaway.command(name="end", aliases=["gend"])
    @app_commands.describe(reference="Giveaway number or its message ID")
    @permissions.admin_only()
    @permissions.guild_only()
    async def giveaway_end(self, ctx: commands.Context, reference: str) -> None:
        """End a running giveaway early and draw winners now."""
        row = await self.fetch(ctx.guild.id, reference)
        if row["status"] != "pending":
            raise DeezeeError(f"That giveaway is already **{row['status']}**.")

        await self.bot.scheduler.cancel_matching(
            ctx.guild.id, "giveaway_end", giveaway_id=row["id"]
        )
        await self.finish(row)
        await ctx.send(embed=self._ok(f"Ended giveaway **#{row['id']}**."))

    @giveaway.command(name="reroll", aliases=["greroll"])
    @app_commands.describe(
        reference="Giveaway number or its message ID",
        count="How many replacement winners",
        allow_repeat="Allow someone who already won to win again",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def giveaway_reroll(
        self, ctx: commands.Context, reference: str, count: int = 1,
        allow_repeat: bool = False,
    ) -> None:
        """Draw replacement winners for a finished giveaway."""
        row = await self.fetch(ctx.guild.id, reference)
        if row["status"] == "pending":
            raise DeezeeError("That giveaway has not ended yet. Use `?giveaway end`.")
        if not 1 <= count <= 20:
            raise DeezeeError("Reroll count must be between 1 and 20.")

        winners, disqualified = await self.draw(
            row, count, exclude_previous=not allow_repeat
        )
        if not winners:
            raise DeezeeError(
                "No eligible entrants are left."
                + (
                    f" {disqualified} were skipped for leaving or no longer "
                    "qualifying."
                    if disqualified
                    else " Everyone who entered has already won."
                )
            )

        await ctx.send(
            embed=discord.Embed(
                title="Reroll",
                description="New winner(s): "
                + ", ".join(f"<@{uid}>" for uid in winners)
                + f"\nPrize: **{row['prize']}**",
                colour=COLOUR_SUCCESS,
            ),
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    @giveaway.command(name="cancel")
    @app_commands.describe(reference="Giveaway number or its message ID")
    @permissions.admin_only()
    @permissions.guild_only()
    async def giveaway_cancel(self, ctx: commands.Context, reference: str) -> None:
        """Cancel a giveaway without drawing any winner."""
        row = await self.fetch(ctx.guild.id, reference)
        if row["status"] != "pending":
            raise DeezeeError(f"That giveaway is already **{row['status']}**.")

        entries = await self.entry_count(row["id"])
        proceed = await confirm(
            ctx,
            title=f"Cancel the giveaway for {row['prize']}?",
            description=(
                f"**{entries}** entrant(s) will get nothing and no winner is drawn.\n"
                "Use `?giveaway end` instead if you want to draw now."
            ),
            confirm_label="Cancel it",
        )
        if not proceed:
            return

        await self.bot.db.execute(
            "UPDATE giveaways SET status = 'cancelled' WHERE id = ?", (row["id"],)
        )
        await self.bot.scheduler.cancel_matching(
            ctx.guild.id, "giveaway_end", giveaway_id=row["id"]
        )
        row["status"] = "cancelled"
        await self.refresh_message(row)
        await ctx.send(embed=self._ok(f"Cancelled giveaway **#{row['id']}**."))

    # =======================================================================
    # Scheduled messages
    # =======================================================================

    @commands.hybrid_group(name="schedule", aliases=["scheduled"], fallback="list")
    @permissions.admin_only()
    @permissions.guild_only()
    async def schedule(self, ctx: commands.Context) -> None:
        """Messages queued to post later, once or on a repeat."""
        rows = await self.bot.db.fetchall(
            "SELECT * FROM scheduled_messages WHERE guild_id = ? AND active = 1 "
            "ORDER BY next_at",
            (ctx.guild.id,),
        )
        lines = []
        for row in rows:
            repeat = (
                f"every {format_duration(row['interval_seconds'])}"
                if row["interval_seconds"]
                else "once"
            )
            lines.append(
                f"**#{row['id']}** <#{row['channel_id']}> — {repeat}\n"
                f"next <t:{row['next_at']}:{TS_RELATIVE}> • "
                f"{row['posts']} post(s) so far\n"
                f"> {row['content'][:100]}"
            )
        pages = paginate_lines(
            lines,
            title="Scheduled messages",
            per_page=5,
            description="`?schedule create <channel> <when> <message>`",
            empty_message="Nothing is scheduled.",
        )
        await Paginator(pages, ctx.author).start(ctx)

    @schedule.command(name="create")
    @app_commands.describe(
        channel="Where to post",
        when="How long until the first post, e.g. 1h",
        repeat="Optional: repeat this often, e.g. 24h. Minimum 10m",
        message="What to post",
    )
    @permissions.admin_only()
    @permissions.guild_only()
    async def schedule_create(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        when: str,
        repeat: Optional[str] = None,
        *,
        message: str,
    ) -> None:
        """Schedule a message to post once, or on a repeating interval."""
        delta = parse_duration(when)
        if delta is None:
            raise DeezeeError(f"`{when}` is not a duration. Try `1h` or `2d`.")

        interval = 0
        if repeat:
            repeat_delta = parse_duration(repeat)
            if repeat_delta is None:
                raise DeezeeError(f"`{repeat}` is not a duration.")
            interval = int(repeat_delta.total_seconds())
            if interval < MIN_SCHEDULE_INTERVAL:
                raise DeezeeError(
                    f"The shortest repeat is "
                    f"{format_duration(MIN_SCHEDULE_INTERVAL)}. Anything faster is "
                    "a bot spamming a channel."
                )

        perms = channel.permissions_for(ctx.guild.me)
        if not perms.send_messages:
            raise DeezeeError(f"I cannot post in {channel.mention}.")

        next_at = int(time.time() + delta.total_seconds())
        cursor = await self.bot.db.execute(
            "INSERT INTO scheduled_messages (guild_id, channel_id, content, next_at, "
            "interval_seconds, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ctx.guild.id, channel.id, message[:1900], next_at, interval,
                ctx.author.id, int(time.time()),
            ),
        )
        row_id = int(cursor.lastrowid or 0)
        await self.bot.scheduler.schedule(
            ctx.guild.id, "scheduled_message", next_at, {"message_id": row_id}
        )

        await ctx.send(
            embed=self._ok(
                f"Scheduled **#{row_id}** for {channel.mention} at "
                f"<t:{next_at}:{TS_LONG_DATE_TIME}>"
                + (f", repeating every {format_duration(interval)}." if interval
                   else ".")
            )
        )

    @schedule.command(name="delete")
    @app_commands.describe(schedule_id="Number from ?schedule list")
    @permissions.admin_only()
    @permissions.guild_only()
    async def schedule_delete(self, ctx: commands.Context, schedule_id: int) -> None:
        """Cancel a scheduled message."""
        cursor = await self.bot.db.execute(
            "UPDATE scheduled_messages SET active = 0 WHERE guild_id = ? AND id = ?",
            (ctx.guild.id, schedule_id),
        )
        if not cursor.rowcount:
            raise DeezeeError(f"There is no scheduled message **#{schedule_id}**.")
        await self.bot.scheduler.cancel_matching(
            ctx.guild.id, "scheduled_message", message_id=schedule_id
        )
        await ctx.send(embed=self._ok(f"Cancelled scheduled message **#{schedule_id}**."))

    async def _scheduled_message(self, guild_id: int, payload: dict[str, Any]) -> None:
        """Post a scheduled message and queue its next run."""
        row_id = int(payload.get("message_id", 0))
        row = await self.bot.db.fetchone(
            "SELECT * FROM scheduled_messages WHERE id = ?", (row_id,)
        )
        if row is None or not row["active"]:
            return

        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(row["channel_id"]) if guild else None

        posted = False
        if isinstance(channel, discord.TextChannel):
            try:
                if row["is_embed"]:
                    await channel.send(
                        embed=discord.Embed(
                            description=row["content"], colour=COLOUR_INFO
                        )
                    )
                else:
                    await channel.send(
                        row["content"][:2000],
                        allowed_mentions=discord.AllowedMentions(
                            everyone=False, roles=False, users=False
                        ),
                    )
                posted = True
            except discord.HTTPException as exc:
                log.warning("Scheduled message %s failed: %s", row_id, exc)

        if not row["interval_seconds"]:
            await self.bot.db.execute(
                "UPDATE scheduled_messages SET active = 0, posts = posts + ? "
                "WHERE id = ?",
                (int(posted), row_id),
            )
            return

        next_at = int(time.time()) + row["interval_seconds"]
        await self.bot.db.execute(
            "UPDATE scheduled_messages SET next_at = ?, posts = posts + ? WHERE id = ?",
            (next_at, int(posted), row_id),
        )
        await self.bot.scheduler.schedule(
            guild_id, "scheduled_message", next_at, {"message_id": row_id}
        )


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point."""
    await bot.add_cog(Giveaways(bot))
