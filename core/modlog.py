"""Mod-log cases: creation, posting and editing.

Kept out of ``cogs/moderation.py`` because the automod and anti-raid cogs write
cases too, and out of ``cogs/logging.py`` because a case must be recorded even
when that cog is not loaded. A case is a database row first and a Discord embed
second -- if the mod-log channel is unset or unreachable, the row still exists
and ``?case`` still finds it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

from .constants import ACTION_COLOURS, COLOUR_DEFAULT, TS_LONG_DATE_TIME, TS_RELATIVE

if TYPE_CHECKING:
    from .bot import DeezeeBot

log = logging.getLogger(__name__)

#: Human labels for the action strings stored in ``mod_cases.action``.
ACTION_LABELS: dict[str, str] = {
    "ban": "Ban",
    "tempban": "Temporary Ban",
    "softban": "Softban",
    "unban": "Unban",
    "kick": "Kick",
    "mute": "Mute",
    "unmute": "Unmute",
    "warn": "Warn",
    "unwarn": "Warning Removed",
    "note": "Note",
    "purge": "Purge",
    "lockdown": "Lockdown",
    "lock": "Channel Locked",
    "unlock": "Channel Unlocked",
    "slowmode": "Slowmode",
    "nickname": "Nickname Change",
    "nuke": "Channel Nuked",
    "voicekick": "Voice Kick",
    "voicemute": "Voice Mute",
    "voiceunmute": "Voice Unmute",
    "deafen": "Voice Deafen",
    "undeafen": "Voice Undeafen",
}


@dataclass(slots=True)
class Case:
    """One row of ``mod_cases``."""

    id: int
    guild_id: int
    number: int
    action: str
    target_id: int
    target_tag: str
    moderator_id: int
    moderator_tag: str
    reason: str | None
    created_at: int
    expires_at: int | None
    active: bool
    log_message_id: int | None

    @property
    def label(self) -> str:
        return ACTION_LABELS.get(self.action, self.action.title())

    @property
    def colour(self) -> int:
        return ACTION_COLOURS.get(self.action, COLOUR_DEFAULT)

    @classmethod
    def from_row(cls, row: object) -> Case:
        """Build a case from an ``aiosqlite.Row``."""
        return cls(
            id=row["id"],
            guild_id=row["guild_id"],
            number=row["case_number"],
            action=row["action"],
            target_id=row["target_id"],
            target_tag=row["target_tag"],
            moderator_id=row["moderator_id"],
            moderator_tag=row["moderator_tag"],
            reason=row["reason"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            active=bool(row["active"]),
            log_message_id=row["log_message_id"],
        )


def build_embed(case: Case, *, guild: discord.Guild | None = None) -> discord.Embed:
    """Render a case as a mod-log embed."""
    embed = discord.Embed(
        title=f"{case.label} | Case #{case.number}",
        colour=case.colour,
        timestamp=datetime.fromtimestamp(case.created_at, tz=timezone.utc),
    )
    embed.description = (
        f"**Member:** {case.target_tag} (`{case.target_id}`)\n"
        f"**Moderator:** {case.moderator_tag} (`{case.moderator_id}`)\n"
        f"**Reason:** {case.reason or 'No reason given'}"
    )

    if case.expires_at:
        embed.add_field(
            name="Expires",
            value=f"<t:{case.expires_at}:{TS_LONG_DATE_TIME}> "
            f"(<t:{case.expires_at}:{TS_RELATIVE}>)",
            inline=False,
        )
    if not case.active and case.expires_at:
        embed.add_field(name="Status", value="No longer active", inline=False)

    embed.set_footer(text=f"Case {case.number}")
    return embed


async def create_case(
    bot: DeezeeBot,
    guild: discord.Guild,
    *,
    action: str,
    target: discord.abc.User,
    moderator: discord.abc.User,
    reason: str | None = None,
    expires_at: int | None = None,
    active: bool = True,
    post: bool = True,
) -> Case:
    """Write a case and, unless told otherwise, post it to the mod-log.

    The case number comes from the guild's counter, incremented inside its own
    transaction, so two simultaneous bans cannot share a number.

    Args:
        bot: The bot, for database and config access.
        guild: Where the action happened.
        action: One of the keys in :data:`ACTION_LABELS`.
        target: Who it was done to.
        moderator: Who did it.
        reason: Free text, or ``None``.
        expires_at: Epoch seconds, for timed punishments.
        active: Whether the punishment is currently in force.
        post: Set ``False`` for actions that log their own richer embed.

    Returns:
        The created case, with ``log_message_id`` filled in if it was posted.
    """
    number = await bot.guild_config.next_case_number(guild.id)
    now = int(time.time())

    cursor = await bot.db.execute(
        "INSERT INTO mod_cases (guild_id, case_number, action, target_id, target_tag, "
        "moderator_id, moderator_tag, reason, created_at, expires_at, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            guild.id,
            number,
            action,
            target.id,
            str(target),
            moderator.id,
            str(moderator),
            reason,
            now,
            expires_at,
            int(active),
        ),
    )

    case = Case(
        id=int(cursor.lastrowid or 0),
        guild_id=guild.id,
        number=number,
        action=action,
        target_id=target.id,
        target_tag=str(target),
        moderator_id=moderator.id,
        moderator_tag=str(moderator),
        reason=reason,
        created_at=now,
        expires_at=expires_at,
        active=active,
        log_message_id=None,
    )

    if post:
        await post_case(bot, guild, case)

    return case


async def post_case(bot: DeezeeBot, guild: discord.Guild, case: Case) -> None:
    """Post a case to the mod-log channel and remember the message.

    Every failure here is non-fatal. A missing channel, a deleted channel or a
    missing permission must not undo a ban that already happened.
    """
    channel_id = await bot.guild_config.channel_id(guild.id, "mod_log_channel_id")
    if channel_id is None:
        return

    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        log.debug("Mod-log channel %s is missing or not a text channel", channel_id)
        return

    try:
        message = await channel.send(embed=build_embed(case, guild=guild))
    except discord.Forbidden:
        log.warning(
            "Cannot post to mod-log channel %s in guild %s: missing permissions",
            channel_id,
            guild.id,
        )
        return
    except discord.HTTPException as exc:
        log.warning("Could not post case #%d: %s", case.number, exc)
        return

    case.log_message_id = message.id
    await bot.db.execute(
        "UPDATE mod_cases SET log_message_id = ? WHERE id = ?", (message.id, case.id)
    )


async def fetch_case(bot: DeezeeBot, guild_id: int, number: int) -> Case | None:
    """Look up one case by its per-guild number."""
    row = await bot.db.fetchone(
        "SELECT * FROM mod_cases WHERE guild_id = ? AND case_number = ?", (guild_id, number)
    )
    return Case.from_row(row) if row else None


async def update_reason(
    bot: DeezeeBot, guild: discord.Guild, case: Case, reason: str
) -> bool:
    """Change a case's reason and edit the posted embed to match.

    Returns:
        True if the posted embed was updated too. False means only the database
        row changed -- the message was deleted, or was never posted.
    """
    case.reason = reason
    await bot.db.execute("UPDATE mod_cases SET reason = ? WHERE id = ?", (reason, case.id))

    if case.log_message_id is None:
        return False

    channel_id = await bot.guild_config.channel_id(guild.id, "mod_log_channel_id")
    if channel_id is None:
        return False
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return False

    try:
        message = await channel.fetch_message(case.log_message_id)
        await message.edit(embed=build_embed(case, guild=guild))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return False
    return True


async def deactivate(bot: DeezeeBot, case_id: int) -> None:
    """Mark a punishment as no longer in force."""
    await bot.db.execute("UPDATE mod_cases SET active = 0 WHERE id = ?", (case_id,))


async def active_case_for(
    bot: DeezeeBot, guild_id: int, user_id: int, actions: tuple[str, ...]
) -> Case | None:
    """Find the newest active case of the given kinds against a user.

    Used to close the right case when a punishment is lifted by hand.
    """
    placeholders = ", ".join("?" * len(actions))
    row = await bot.db.fetchone(
        f"SELECT * FROM mod_cases WHERE guild_id = ? AND target_id = ? AND active = 1 "
        f"AND action IN ({placeholders}) ORDER BY created_at DESC LIMIT 1",
        (guild_id, user_id, *actions),
    )
    return Case.from_row(row) if row else None
