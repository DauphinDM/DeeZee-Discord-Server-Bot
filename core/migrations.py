"""Versioned schema migrations.

Files live in ``migrations/`` as ``NNN_name.sql`` and are applied in numeric
order. A shipped migration is never edited -- corrections go in a new file. That
rule is what makes redeploying on Wispbyte safe: the panel may restart the
process at any point, and reapplying the same set must always be a no-op.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .database import Database

log = logging.getLogger(__name__)

_FILENAME_RE = re.compile(r"^(?P<version>\d{3,})_(?P<name>[A-Za-z0-9_-]+)\.sql$")

_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    applied_at INTEGER NOT NULL
)
"""


class MigrationError(RuntimeError):
    """Raised when a migration file is malformed or fails to apply."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One migration file on disk."""

    version: int
    name: str
    path: Path

    @property
    def label(self) -> str:
        return f"{self.version:03d}_{self.name}"


def discover(directory: Path) -> list[Migration]:
    """Find and sort every migration in ``directory``.

    Raises:
        MigrationError: If two files claim the same version number. Silently
            applying one of them would leave two deployments with different
            schemas and the same recorded version.
    """
    if not directory.is_dir():
        raise MigrationError(f"Migrations directory {directory} does not exist.")

    found: dict[int, Migration] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix != ".sql":
            continue
        match = _FILENAME_RE.match(path.name)
        if match is None:
            raise MigrationError(
                f"{path.name} does not match the NNN_name.sql pattern. "
                "Rename it or move it out of the migrations directory."
            )
        version = int(match["version"])
        if version in found:
            raise MigrationError(
                f"Duplicate migration version {version:03d}: "
                f"{found[version].path.name} and {path.name}."
            )
        found[version] = Migration(version=version, name=match["name"], path=path)

    return [found[v] for v in sorted(found)]


def _uses_own_transaction(sql: str) -> bool:
    """Detect transaction control inside a migration file.

    Migrations are wrapped in BEGIN/COMMIT by :func:`apply_migrations`, and a
    nested BEGIN aborts the whole script with a message that says nothing about
    which file caused it. This catches it early with a useful error instead.

    Comment lines are stripped first so prose about a transaction is not
    mistaken for one. Files containing a trigger are skipped entirely: a trigger
    body legitimately opens with ``BEGIN`` and closes with ``END``, which no
    line-level check can tell apart from real transaction control.
    """
    if re.search(r"\bCREATE\s+TRIGGER\b", sql, re.IGNORECASE):
        return False
    for line in sql.splitlines():
        statement = line.split("--", 1)[0].strip()
        if re.match(r"^(BEGIN|COMMIT|ROLLBACK|END\s+TRANSACTION)\b", statement, re.IGNORECASE):
            return True
    return False


async def applied_versions(db: Database) -> set[int]:
    """Every migration version already recorded in this database."""
    await db.execute(_SCHEMA_VERSION_DDL)
    rows = await db.fetchall("SELECT version FROM schema_version")
    return {row["version"] for row in rows}


async def apply_migrations(db: Database, directory: Path) -> list[Migration]:
    """Apply every pending migration, oldest first.

    Each file runs inside its own transaction together with the row that records
    it, so a failure halfway through leaves the database exactly as it was --
    SQLite makes DDL transactional, which most engines do not.

    Args:
        db: A connected database.
        directory: Where the ``.sql`` files live.

    Returns:
        The migrations applied by this call, in order. Empty on an up-to-date
        database.

    Raises:
        MigrationError: On a malformed file or a failing statement. The bot must
            not continue against a half-known schema.
    """
    migrations = discover(directory)
    already = await applied_versions(db)

    pending = [m for m in migrations if m.version not in already]
    if not pending:
        log.info(
            "Schema up to date (%d migration%s applied)",
            len(already),
            "" if len(already) == 1 else "s",
        )
        return []

    # A version recorded in the database with no matching file means someone
    # deleted a migration. The schema is then unreproducible; warn loudly but do
    # not refuse to boot, because the running database is still valid.
    known = {m.version for m in migrations}
    for orphan in sorted(already - known):
        log.warning(
            "schema_version records migration %03d but no file provides it. "
            "This database cannot be rebuilt from migrations/ alone.",
            orphan,
        )

    applied: list[Migration] = []
    for migration in pending:
        sql = migration.path.read_text(encoding="utf-8")
        if not sql.strip():
            raise MigrationError(f"{migration.path.name} is empty.")
        if _uses_own_transaction(sql):
            raise MigrationError(
                f"{migration.path.name} contains its own transaction control. "
                "Migrations are wrapped automatically -- remove BEGIN/COMMIT."
            )

        # executescript() cannot take bound parameters, so the bookkeeping row is
        # built here. Both values are ours, not user input.
        script = (
            "BEGIN;\n"
            f"{sql}\n"
            "INSERT INTO schema_version (version, name, applied_at) VALUES "
            f"({migration.version}, '{migration.name}', {int(time.time())});\n"
            "COMMIT;"
        )

        try:
            await db.conn.executescript(script)
        except Exception as exc:
            # The transaction has already rolled back; make the failure legible.
            raise MigrationError(
                f"Migration {migration.label} failed and was rolled back: {exc}"
            ) from exc

        log.info("Applied migration %s", migration.label)
        applied.append(migration)

    return applied
