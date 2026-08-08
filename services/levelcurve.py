"""The XP curve, and conversions from other bots' curves.

Pure arithmetic, no Discord. Kept in ``services/`` because it is exactly the kind
of code that harbours an off-by-one nobody notices until a member's rank is
wrong: it is worth being able to test it without a gateway connection.

Deezee uses MEE6's curve. Not because it is better -- it is arbitrary -- but
because it is the one members coming from MEE6, Arcane and Carl-bot already have
an intuition for, and because an import can then map ranks onto the same place
they were before rather than shuffling everyone.

    XP to go from level n to level n+1  =  5n² + 50n + 100

So level 1 costs 100, level 2 costs 155, level 10 costs 1100.
"""

from __future__ import annotations

#: Refuse to compute past this. A level beyond it means the XP total is corrupt
#: or was set by hand, and the loops below should not run forever on it.
MAX_LEVEL = 1000


def xp_for_next_level(level: int) -> int:
    """XP needed to get from ``level`` to ``level + 1``."""
    if level < 0:
        return 0
    return 5 * level * level + 50 * level + 100


def total_xp_for_level(level: int) -> int:
    """Cumulative XP needed to reach ``level`` from zero.

    Closed form rather than a loop: this is called once per ``?rank`` and once
    per level-up check, and the sum of a quadratic has an exact expression.

        sum(5i² + 50i + 100) for i in 0..n-1
          = 5·(n-1)n(2n-1)/6 + 50·(n-1)n/2 + 100n
    """
    n = max(0, min(level, MAX_LEVEL))
    if n == 0:
        return 0
    squares = (n - 1) * n * (2 * n - 1) // 6
    linear = (n - 1) * n // 2
    return 5 * squares + 50 * linear + 100 * n


def level_from_xp(xp: int) -> int:
    """Highest level fully paid for by ``xp``.

    Walks upward rather than solving the cubic. The loop runs at most a few
    dozen times for any realistic total -- level 100 is 1.7 million XP -- and an
    exact integer answer beats a floating-point root that lands on 41.99999.
    """
    if xp <= 0:
        return 0
    level = 0
    remaining = xp
    while level < MAX_LEVEL:
        cost = xp_for_next_level(level)
        if remaining < cost:
            break
        remaining -= cost
        level += 1
    return level


def progress(xp: int) -> tuple[int, int, int]:
    """Break a total down for display.

    Returns:
        ``(level, xp_into_this_level, xp_needed_for_the_next)``.
    """
    level = level_from_xp(xp)
    base = total_xp_for_level(level)
    return level, xp - base, xp_for_next_level(level)


# --- Foreign curves --------------------------------------------------------
# An import that copied raw XP across would move everybody's rank, because two
# bots that both call something "level 30" rarely mean the same number. Ranks
# are converted through the level, not through the number.


def convert_from_mee6(mee6_xp: int) -> int:
    """MEE6 XP to Deezee XP.

    MEE6 uses the same curve Deezee does, so this is the identity. It exists as
    a named function anyway: the day MEE6 changes their curve, this is the one
    place that has to change, and callers should not have to know that today it
    happens to be a no-op.
    """
    return max(0, mee6_xp)


def convert_from_level(level: int, *, fraction: float = 0.0) -> int:
    """A level on any curve to the Deezee XP that lands on the same level.

    Args:
        level: The member's level on the source bot.
        fraction: How far into that level they were, 0.0 to 1.0, if known.

    Returns:
        XP that produces the same level here.
    """
    level = max(0, min(int(level), MAX_LEVEL))
    base = total_xp_for_level(level)
    span = xp_for_next_level(level)
    return base + int(span * max(0.0, min(1.0, fraction)))


def convert_from_arcane(arcane_xp: int, arcane_level: int) -> int:
    """Arcane XP to Deezee XP.

    Arcane's curve is steeper than MEE6's, so copying the number would demote
    everyone. The level is the reliable part of what their leaderboard exposes,
    so the level is what gets preserved and the XP is rebuilt from it.
    """
    if arcane_level > 0:
        return convert_from_level(arcane_level)
    return max(0, arcane_xp)
