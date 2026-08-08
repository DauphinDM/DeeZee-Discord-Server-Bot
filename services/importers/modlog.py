"""Rebuilding moderation history from another bot's mod-log embeds.

This one works for a reason worth stating: the embeds are already in your
server, and any bot with Read Message History can read them. No API, no
cooperation from the other bot, nothing that can be turned off.

Dyno, Carl-bot and Sapphire all render a case as an embed with the same four
facts arranged differently -- action, target, moderator, reason -- plus a case
number in the title or the footer. This module recognises all three shapes and
reports what it could not parse rather than guessing.

Input is a plain dict shaped like ``discord.Embed.to_dict()``, so the parser has
no Discord dependency and every shape below can be tested from a literal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Action words to the values Deezee stores in ``mod_cases.action``.
ACTION_MAP: dict[str, str] = {
    "ban": "ban",
    "banned": "ban",
    "tempban": "tempban",
    "temporary ban": "tempban",
    "softban": "softban",
    "unban": "unban",
    "unbanned": "unban",
    "kick": "kick",
    "kicked": "kick",
    "mute": "mute",
    "muted": "mute",
    "tempmute": "mute",
    "timeout": "mute",
    "timed out": "mute",
    "unmute": "unmute",
    "unmuted": "unmute",
    "untimeout": "unmute",
    "warn": "warn",
    "warned": "warn",
    "warning": "warn",
    "note": "note",
    "purge": "purge",
    "clear": "purge",
}

_CASE_NUMBER = re.compile(r"case\s*#?\s*(\d{1,6})", re.IGNORECASE)
_SNOWFLAKE = re.compile(r"(\d{15,20})")
_TAG = re.compile(r"([^\s<>@#]{2,32}#\d{4}|@?[A-Za-z0-9_.]{2,32})")

#: Field names each bot uses. Compared lowercased and stripped.
_TARGET_FIELDS = frozenset({"user", "member", "target", "offender", "user punished"})
_MODERATOR_FIELDS = frozenset({"moderator", "mod", "responsible", "staff", "by"})
_REASON_FIELDS = frozenset({"reason", "why"})
_DURATION_FIELDS = frozenset({"duration", "length", "expires", "time"})


@dataclass(slots=True)
class ParsedCase:
    """One recovered case.

    Every field is optional except ``action``, because a case whose action could
    not be read is not a case -- it goes to ``unparsed`` instead.
    """

    action: str
    number: int | None = None
    target_id: int | None = None
    target_tag: str = ""
    moderator_id: int | None = None
    moderator_tag: str = ""
    reason: str = ""
    duration: str = ""
    created_at: int = 0
    source: str = "unknown"
    warnings: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Whether this case has enough to be worth writing.

        A target is the minimum. A case with an action and a reason but nobody it
        happened to is a sentence, not a record.
        """
        return self.target_id is not None


def _clean(value: str) -> str:
    return value.replace("`", "").replace("*", "").strip()


def _first_snowflake(*values: str) -> int | None:
    for value in values:
        if not value:
            continue
        match = _SNOWFLAKE.search(value)
        if match:
            return int(match.group(1))
    return None


def _match_action(text: str) -> str | None:
    """Find an action word anywhere in a title or description."""
    lowered = text.lower()
    # Longest first, so "temporary ban" is not read as "ban".
    for word in sorted(ACTION_MAP, key=len, reverse=True):
        if word in lowered:
            return ACTION_MAP[word]
    return None


def parse_embed(embed: dict, *, created_at: int = 0) -> ParsedCase | None:
    """Recover one case from an embed dictionary.

    Returns:
        The case, or ``None`` if this embed is not a mod-log entry at all --
        which is normal, since a mod-log channel usually holds other things too.
    """
    title = str(embed.get("title") or "")
    description = str(embed.get("description") or "")
    footer = str((embed.get("footer") or {}).get("text") or "")
    author = str((embed.get("author") or {}).get("name") or "")

    action = _match_action(title) or _match_action(author) or _match_action(description)
    if action is None:
        return None

    case = ParsedCase(action=action, created_at=created_at)

    number_match = _CASE_NUMBER.search(title) or _CASE_NUMBER.search(footer)
    if number_match:
        case.number = int(number_match.group(1))

    # Sapphire and Carl-bot put everything in fields; Dyno mixes fields and the
    # description. Both are read, fields first because they are unambiguous.
    for raw_field in embed.get("fields") or []:
        name = str(raw_field.get("name") or "").lower().strip().rstrip(":")
        value = str(raw_field.get("value") or "")

        if name in _TARGET_FIELDS:
            case.target_id = _first_snowflake(value)
            case.target_tag = _clean(re.sub(r"<@!?\d+>", "", value)) or case.target_tag
        elif name in _MODERATOR_FIELDS:
            case.moderator_id = _first_snowflake(value)
            case.moderator_tag = _clean(re.sub(r"<@!?\d+>", "", value))
        elif name in _REASON_FIELDS:
            case.reason = _clean(value)[:1000]
        elif name in _DURATION_FIELDS:
            case.duration = _clean(value)[:60]

    if case.target_id is None:
        # Dyno's description form: "**Member:** name#1234 (123456789012345678)".
        member_line = re.search(
            r"(?:member|user|target)\D{0,4}(.{0,80})", description, re.IGNORECASE
        )
        if member_line:
            case.target_id = _first_snowflake(member_line.group(1))
            case.target_tag = _clean(
                re.sub(r"[<>@!()\d]", "", member_line.group(1))
            )[:64]

    if case.target_id is None:
        case.target_id = _first_snowflake(description, footer, author)

    if case.moderator_id is None:
        moderator_line = re.search(
            r"(?:moderator|responsible|by)\D{0,4}(.{0,80})", description, re.IGNORECASE
        )
        if moderator_line:
            case.moderator_id = _first_snowflake(moderator_line.group(1))
            case.moderator_tag = _clean(
                re.sub(r"[<>@!()\d]", "", moderator_line.group(1))
            )[:64]

    if not case.reason:
        reason_line = re.search(r"reason\D{0,4}(.{0,400})", description, re.IGNORECASE)
        if reason_line:
            case.reason = _clean(reason_line.group(1))[:1000]

    if case.target_id is None:
        case.warnings.append("No target could be identified.")
    if case.number is None:
        case.warnings.append("No case number was found; one will be assigned.")

    case.source = detect_source(embed)
    return case


def detect_source(embed: dict) -> str:
    """Guess which bot wrote an embed, for the review screen.

    A guess, and labelled as one. It affects nothing but the wording shown to
    whoever is reviewing the import.
    """
    footer = str((embed.get("footer") or {}).get("text") or "").lower()
    author = str((embed.get("author") or {}).get("name") or "").lower()
    title = str(embed.get("title") or "").lower()

    if "dyno" in footer or "dyno" in author:
        return "Dyno"
    if "carl" in footer or "carl" in author:
        return "Carl-bot"
    if "sapphire" in footer or "sapphire" in author:
        return "Sapphire"
    if title.startswith("case #"):
        return "Carl-bot"
    if "| case" in title:
        return "Sapphire"
    return "unknown"


def parse_many(
    embeds: list[tuple[dict, int]],
) -> tuple[list[ParsedCase], list[str]]:
    """Parse a channel's worth of embeds.

    Args:
        embeds: ``(embed_dict, created_at)`` pairs, newest or oldest first --
            order is preserved, not assumed.

    Returns:
        ``(cases, unparsed)``. ``unparsed`` holds a short description of each
        embed that looked like a case but could not be read, so it can be fixed
        by hand rather than lost.
    """
    cases: list[ParsedCase] = []
    unparsed: list[str] = []

    for embed, created_at in embeds:
        case = parse_embed(embed, created_at=created_at)
        if case is None:
            continue
        if not case.complete:
            title = str(embed.get("title") or embed.get("description") or "")[:80]
            unparsed.append(f"{case.action}: {title or 'no title'}")
            continue
        cases.append(case)

    return cases, unparsed
