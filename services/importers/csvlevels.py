"""CSV leaderboard parsing -- the path that always works.

Every other levels importer depends on somebody else's server staying up and
staying the same shape. This one depends on a text file, so it is the one to
reach for when an import matters.

Accepted shapes, with or without a header row:

* ``user_id,xp``
* ``user_id,xp,anything_else`` -- extra columns are ignored
* ``user_id,level`` -- when the header names the second column ``level``, the
  value is treated as a level and converted through the curve rather than being
  read as an XP total

Anything else on a line puts that line in ``unparsed`` rather than dropping it,
because a silently skipped row is how a member ends up wondering where their
rank went.
"""

from __future__ import annotations

import csv
import io

from .. import levelcurve

#: Refuse a file past this. A million rows is not a leaderboard.
MAX_ROWS = 100_000

#: Header names that mean the second column holds a level, not an XP total.
LEVEL_HEADERS = frozenset({"level", "lvl", "rank_level"})


def parse(raw: str) -> tuple[list[tuple[int, int]], list[str]]:
    """Parse CSV text into ``(user_id, xp)`` pairs.

    Returns:
        ``(entries, unparsed)``.
    """
    entries: list[tuple[int, int]] = []
    unparsed: list[str] = []
    second_is_level = False

    reader = csv.reader(io.StringIO(raw))
    for index, fields in enumerate(reader):
        if index >= MAX_ROWS:
            unparsed.append(f"Stopped at {MAX_ROWS} rows; the rest was ignored.")
            break
        if not fields or all(not field.strip() for field in fields):
            continue
        if len(fields) < 2:
            unparsed.append(",".join(fields)[:120])
            continue

        first = fields[0].strip()
        second = fields[1].strip()

        # Header detection: a first column that is not a snowflake, on the first
        # non-empty line, is a header rather than data.
        if index == 0 and not first.isdigit():
            second_is_level = second.lower() in LEVEL_HEADERS
            continue

        if not first.isdigit() or len(first) < 15:
            unparsed.append(",".join(fields)[:120])
            continue

        try:
            value = int(float(second.replace(",", "").replace(" ", "")))
        except ValueError:
            unparsed.append(",".join(fields)[:120])
            continue

        if value < 0:
            unparsed.append(",".join(fields)[:120])
            continue

        xp = levelcurve.convert_from_level(value) if second_is_level else value
        entries.append((int(first), xp))

    return entries, unparsed


def to_csv(entries: list[tuple[int, int]]) -> str:
    """Render pairs back out as CSV, for an export or a round-trip test."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(("user_id", "xp"))
    writer.writerows(entries)
    return buffer.getvalue()
