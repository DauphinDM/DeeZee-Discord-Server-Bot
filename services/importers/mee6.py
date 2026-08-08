"""MEE6 leaderboard parsing.

MEE6 has no public API, but its dashboard is backed by an unauthenticated JSON
endpoint that answers for any server whose leaderboard has been set public.
That is the whole mechanism, and it is worth being blunt about its limits:

* It only answers when the server owner has made the leaderboard public in
  MEE6's own dashboard. Otherwise it is a 401 and no amount of retrying helps.
* It rate-limits aggressively and without documentation.
* It is unofficial, so it can stop existing at any time without notice.

The CSV path is always available and always works. This one is a convenience,
not the plan.
"""

from __future__ import annotations

from typing import Any

from .. import levelcurve

#: MEE6's leaderboard endpoint. ``page`` is zero-based; ``limit`` caps at 1000.
URL = "https://mee6.xyz/api/plugins/levels/leaderboard/{guild_id}"

#: Pages requested before giving up. 10 000 members is far past the point where
#: the endpoint stops answering anyway.
MAX_PAGES = 10


def parse_page(payload: Any) -> tuple[list[tuple[int, int]], list[str]]:
    """Turn one page of the leaderboard into ``(user_id, xp)`` pairs.

    MEE6 uses the same XP curve Deezee does, so the number carries across
    unchanged -- but it still goes through
    :func:`services.levelcurve.convert_from_mee6`, so that the day their curve
    changes there is exactly one place to fix.

    Returns:
        ``(entries, unparsed)``. Anything without a usable ID and XP lands in
        ``unparsed`` rather than being dropped silently.
    """
    entries: list[tuple[int, int]] = []
    unparsed: list[str] = []

    players = payload.get("players") if isinstance(payload, dict) else None
    if not isinstance(players, list):
        return entries, ["The response had no player list."]

    for player in players:
        if not isinstance(player, dict):
            unparsed.append(repr(player)[:120])
            continue
        try:
            user_id = int(player["id"])
            xp = int(player.get("xp", 0))
        except (KeyError, TypeError, ValueError):
            unparsed.append(repr(player)[:120])
            continue
        entries.append((user_id, levelcurve.convert_from_mee6(xp)))

    return entries, unparsed


def describe_status(status: int) -> str:
    """Plain-language explanation of a non-200 response."""
    if status == 401:
        return (
            "This server's MEE6 leaderboard is private. Make it public in MEE6's "
            "own dashboard, or use the CSV path -- which always works."
        )
    if status == 404:
        return "MEE6 does not know this server. It may never have been added."
    if status == 429:
        return "MEE6 is rate-limiting the request. Wait a few minutes, or use CSV."
    return f"MEE6 answered HTTP {status}."
