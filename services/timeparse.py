"""Duration parsing and formatting.

``10m``, ``2h30m``, ``7d``, ``1w2d``, ``90`` (bare seconds). Used by every timed
punishment, by reminders, by giveaways and by slowmode.

Deliberately strict: an unparseable duration returns ``None`` rather than
guessing. ``?ban @user spamming`` must not silently become a ban of "s" days.
"""

from __future__ import annotations

import re
from datetime import timedelta

#: Unit suffixes, longest first so "mo" is matched before "m".
_UNITS: tuple[tuple[str, int], ...] = (
    ("years", 31_536_000),
    ("year", 31_536_000),
    ("months", 2_592_000),
    ("month", 2_592_000),
    ("weeks", 604_800),
    ("week", 604_800),
    ("days", 86_400),
    ("day", 86_400),
    ("hours", 3_600),
    ("hour", 3_600),
    ("minutes", 60),
    ("minute", 60),
    ("seconds", 1),
    ("second", 1),
    ("mo", 2_592_000),
    ("y", 31_536_000),
    ("w", 604_800),
    ("d", 86_400),
    ("h", 3_600),
    ("m", 60),
    ("s", 1),
)

_PATTERN = re.compile(
    r"(?P<amount>\d+)\s*(?P<unit>" + "|".join(unit for unit, _ in _UNITS) + r")",
    re.IGNORECASE,
)

#: Words that explicitly mean "no duration".
_CLEAR_WORDS = frozenset({"off", "none", "clear", "remove", "permanent", "perm", "forever", "0"})

#: Refuse anything past ten years. A duration that long is a typo, and the
#: scheduler would carry the row forever.
MAX_SECONDS = 10 * 31_536_000


def parse_duration(text: str | None) -> timedelta | None:
    """Parse a duration string.

    Accepts several units in one string (``1w2d3h``), with or without spaces,
    and a bare integer as seconds.

    Args:
        text: The string to parse.

    Returns:
        The duration, or ``None`` if the string is empty, means "no duration",
        parses to zero, or is not a duration at all.
    """
    if text is None:
        return None

    cleaned = text.strip().lower()
    if not cleaned or cleaned in _CLEAR_WORDS:
        return None

    # A bare number is seconds. Common for slowmode.
    if cleaned.isdigit():
        seconds = int(cleaned)
        return timedelta(seconds=min(seconds, MAX_SECONDS)) if seconds > 0 else None

    total = 0
    consumed = 0
    for match in _PATTERN.finditer(cleaned):
        for unit, factor in _UNITS:
            if match["unit"].lower() == unit:
                total += int(match["amount"]) * factor
                break
        consumed += len(match.group(0))

    if total <= 0:
        return None

    # Guard against "5minspam" being read as five minutes: if most of the string
    # was not part of a duration, it was not a duration.
    if consumed < len(cleaned.replace(" ", "")) * 0.75:
        return None

    return timedelta(seconds=min(total, MAX_SECONDS))


def is_duration(text: str | None) -> bool:
    """Whether ``text`` parses as a duration.

    Used to tell ``?ban @user 7d spamming`` from ``?ban @user spamming``.
    """
    return parse_duration(text) is not None


def format_duration(delta: timedelta | int | float, *, brief: bool = False) -> str:
    """Render a duration as readable English.

    Args:
        delta: A ``timedelta`` or a number of seconds.
        brief: ``2d 4h`` instead of ``2 days, 4 hours``.

    Returns:
        Text such as ``"2 days, 4 hours"``. Zero or negative gives ``"0 seconds"``.
    """
    seconds = int(delta.total_seconds()) if isinstance(delta, timedelta) else int(delta)
    if seconds <= 0:
        return "0s" if brief else "0 seconds"

    parts: list[str] = []
    for name, short, factor in (
        ("week", "w", 604_800),
        ("day", "d", 86_400),
        ("hour", "h", 3_600),
        ("minute", "m", 60),
        ("second", "s", 1),
    ):
        value, seconds = divmod(seconds, factor)
        if not value:
            continue
        if brief:
            parts.append(f"{value}{short}")
        else:
            parts.append(f"{value} {name}{'s' if value != 1 else ''}")

    # Three units is enough detail for anyone: "2 weeks, 3 days, 4 hours".
    parts = parts[:3]
    return " ".join(parts) if brief else ", ".join(parts)


def split_duration_and_reason(first: str | None, rest: str) -> tuple[timedelta | None, str]:
    """Decide whether the first argument was a duration or the start of a reason.

    ``?ban @user 7d spamming`` and ``?ban @user spamming`` both reach the command
    with the same shape, and only one of them has a duration.

    Args:
        first: The argument that might be a duration.
        rest: Everything after it.

    Returns:
        ``(duration, reason)``. When ``first`` is not a duration it is glued back
        onto the front of the reason, so no word is ever lost.
    """
    if first is None:
        return None, rest.strip()

    duration = parse_duration(first)
    if duration is not None:
        return duration, rest.strip()

    combined = f"{first} {rest}".strip() if rest else first.strip()
    return None, combined
