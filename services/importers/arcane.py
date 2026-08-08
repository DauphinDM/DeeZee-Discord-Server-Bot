"""Arcane leaderboard parsing.

Arcane has no API at all. Its public leaderboard is a rendered HTML page at
``arcane.bot/leaderboard/<guild_id>``, and this module scrapes it.

Be clear about what that means, because it is the weakest path in the importer
and pretending otherwise would waste somebody's afternoon:

* The page sits behind Cloudflare. A plain request is frequently answered with a
  challenge page rather than the leaderboard, and there is nothing this bot can
  or should do about that -- solving it would mean defeating a bot check.
* Even when it does answer, the free tier renders roughly the first hundred
  members. There is no page two to ask for.
* It is HTML, so a redesign breaks the parse. :func:`parse` reports how many
  rows it understood so a silent zero is visible rather than mistaken for an
  empty server.

Arcane's XP curve is steeper than Deezee's, so the **level** is what gets
carried across and the XP is rebuilt from it. Copying the raw number would
demote everybody.
"""

from __future__ import annotations

import re

from .. import levelcurve

#: The public leaderboard page.
URL = "https://arcane.bot/leaderboard/{guild_id}"

#: Signs that Cloudflare answered instead of Arcane.
CHALLENGE_MARKERS = (
    "cf-browser-verification",
    "challenge-platform",
    "Just a moment...",
    "Enable JavaScript and cookies to continue",
)

#: A leaderboard row. Deliberately loose about the markup between the fields,
#: because the exact tags are the part most likely to change.
_ROW = re.compile(
    r"data-user-id=\"(?P<id>\d{15,20})\".{0,600}?"
    r"[Ll]evel\D{0,20}(?P<level>\d{1,4}).{0,400}?"
    r"(?P<xp>[\d,\.]+)\s*(?:XP|xp)",
    re.DOTALL,
)

#: Fallback for a layout that puts the ID in a profile link instead.
_ROW_ALT = re.compile(
    r"/user/(?P<id>\d{15,20}).{0,600}?[Ll]evel\D{0,20}(?P<level>\d{1,4})",
    re.DOTALL,
)


def looks_like_challenge(html: str) -> bool:
    """Whether the response is a Cloudflare interstitial rather than the page."""
    return any(marker in html for marker in CHALLENGE_MARKERS)


def parse(html: str) -> tuple[list[tuple[int, int]], list[str]]:
    """Extract ``(user_id, deezee_xp)`` pairs from the leaderboard page.

    Returns:
        ``(entries, notes)``. ``notes`` explains anything the caller should tell
        the user -- a challenge page, a layout that produced nothing, or the
        hundred-row ceiling.
    """
    notes: list[str] = []

    if looks_like_challenge(html):
        return [], [
            "Cloudflare answered with a browser check instead of the leaderboard. "
            "Deezee will not try to defeat a bot check. Use the CSV path instead."
        ]

    seen: set[int] = set()
    entries: list[tuple[int, int]] = []

    for pattern in (_ROW, _ROW_ALT):
        for match in pattern.finditer(html):
            user_id = int(match["id"])
            if user_id in seen:
                continue
            seen.add(user_id)
            level = int(match["level"])
            entries.append((user_id, levelcurve.convert_from_arcane(0, level)))
        if entries:
            break

    if not entries:
        notes.append(
            "The page loaded but no leaderboard rows were recognised. Arcane has "
            "most likely changed its layout. Use the CSV path."
        )
    else:
        notes.append(
            f"Read {len(entries)} member(s). Arcane's free tier renders roughly the "
            "first hundred, and there is no second page to ask for."
        )
        notes.append(
            "Levels were carried across, not raw XP: Arcane's curve is steeper than "
            "Deezee's, so copying the number would demote everyone."
        )

    return entries, notes


def convert_level(level: int) -> int:
    """Deezee XP that lands on the same level as an Arcane level."""
    return levelcurve.convert_from_level(level)
