"""Reset the bot's database.

Deleting ``data/deezee.db`` by hand mostly works, and this exists because of the
three ways it does not:

* **WAL leaves siblings.** ``deezee.db-wal`` and ``deezee.db-shm`` sit next to
  the database and hold committed pages. Removing only the ``.db`` and starting
  the bot can resurrect rows from the leftover WAL, which looks exactly like a
  reset that silently did not happen.
* **``data/`` is not only the database.** The trusted-domain list, the phishing
  blocklist and the fonts live there too. ``rm -rf data`` costs you those.
* **A running bot holds the file open.** On Windows the delete fails outright;
  on Linux it succeeds and the bot keeps writing to an unlinked inode until it
  restarts, so the reset appears to work and then undoes itself.

Three modes:

    python tools/reset.py --list              # what is in there, nothing changed
    python tools/reset.py --guild <guild_id>  # wipe one server, keep the rest
    python tools/reset.py --all               # wipe everything

Every destructive mode backs up first, refuses to run while the bot is running,
and asks before doing anything. The schema is not recreated here -- the bot
applies its migrations on boot, which keeps one code path responsible for the
schema instead of two.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import NoReturn

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "deezee.db"

#: Written alongside the database, and git-ignored.
BACKUP_DIR_NAME = "backups"

#: Never emptied by --guild: they are not per-guild state.
NON_GUILD_TABLES = frozenset({"schema_version", "sqlite_sequence"})


def die(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def open_db(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        die(f"no database at {path}\n"
            "       Nothing to reset -- the bot creates it on first boot.")
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def assert_not_running(path: Path) -> None:
    """Refuse to touch a database the bot currently has open.

    ``BEGIN IMMEDIATE`` takes the write lock without writing anything, which is
    the cheapest way to ask "is another process using this". The bot holds a
    connection for its whole lifetime, so a lock here means it is up.
    """
    db = sqlite3.connect(path, timeout=1.0)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("ROLLBACK")
    except sqlite3.OperationalError:
        die("the database is locked, which means the bot is still running.\n"
            "       Stop it first (Wispbyte: press Stop and wait for the "
            "console to go quiet), then run this again.")
    finally:
        db.close()


def sibling_files(path: Path) -> list[Path]:
    """The database and its WAL siblings, whichever of them exist."""
    return [
        candidate
        for candidate in (path, path.with_name(path.name + "-wal"),
                          path.with_name(path.name + "-shm"))
        if candidate.exists()
    ]


def back_up(path: Path) -> Path:
    """Copy the database to ``data/backups/``, WAL included.

    ``Connection.backup`` rather than a file copy: it produces a single
    consistent ``.db`` with the WAL already folded in, so the backup is
    restorable by dropping one file back into place.
    """
    directory = path.parent / BACKUP_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = directory / f"{path.stem}-{stamp}.db"

    source = sqlite3.connect(path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    size = target.stat().st_size
    print(f"  backup   {target}  ({size / 1024:.0f} KiB)")
    return target


def guild_tables(db: sqlite3.Connection) -> list[str]:
    """Every table with a ``guild_id`` column.

    Discovered rather than listed, so a table added by a future migration is
    covered without anyone remembering to update this file.
    """
    found = []
    for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'"
    ):
        name = row["name"]
        if name in NON_GUILD_TABLES:
            continue
        columns = {c["name"] for c in db.execute(f"PRAGMA table_info({name})")}
        if "guild_id" in columns:
            found.append(name)
    return sorted(found)


def all_tables(db: sqlite3.Connection) -> list[str]:
    return sorted(
        row["name"]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    )


def count(db: sqlite3.Connection, table: str, guild_id: int | None = None) -> int:
    if guild_id is None:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return db.execute(
        f"SELECT COUNT(*) FROM {table} WHERE guild_id = ?", (guild_id,)
    ).fetchone()[0]


def confirm(prompt: str, expected: str, assume_yes: bool) -> bool:
    """Type-the-word confirmation. A bare y is too easy to hit by reflex."""
    if assume_yes:
        print(f"  {prompt}  [--yes]")
        return True
    print(f"\n{prompt}")
    answer = input(f"Type {expected!r} to continue: ").strip()
    if answer != expected:
        print("Cancelled. Nothing was changed.")
        return False
    return True


# --- Modes -----------------------------------------------------------------


def do_list(path: Path) -> int:
    db = open_db(path)
    try:
        print(f"\n{path}  ({path.stat().st_size / 1024:.0f} KiB)")

        versions = [r[0] for r in db.execute(
            "SELECT version FROM schema_version ORDER BY version")]
        print(f"migrations applied: {len(versions)} "
              f"({versions[0]}-{versions[-1]})" if versions else "no migrations")

        guilds = [r[0] for r in db.execute("SELECT guild_id FROM guild_config")]
        print(f"guilds configured : {guilds or 'none'}")

        print("\nrows per table (only non-empty):")
        rows = [(t, count(db, t)) for t in all_tables(db)]
        widest = max((len(t) for t, n in rows if n), default=0)
        for table, n in rows:
            if n:
                print(f"  {table.ljust(widest)}  {n}")
        empty = sum(1 for _, n in rows if not n)
        print(f"\n  ...and {empty} empty table(s)")
    finally:
        db.close()
    return 0


def do_guild(path: Path, guild_id: int, assume_yes: bool, backup: bool,
             dry_run: bool) -> int:
    db = open_db(path)
    try:
        tables = guild_tables(db)
        counts = {t: count(db, t, guild_id) for t in tables}
        total = sum(counts.values())

        print(f"\nGuild {guild_id} in {path.name}:")
        for table, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            if n:
                print(f"  {table:<28} {n}")
        print(f"  {'TOTAL':<28} {total}")

        if not total:
            print("\nNothing to delete -- that guild has no rows.")
            return 0
        if dry_run:
            print("\n--dry-run: nothing was deleted.")
            return 0
    finally:
        db.close()

    if not confirm(
        f"This deletes all {total} row(s) for guild {guild_id}: settings, "
        "levels, economy, warnings, cases, tags, giveaways, everything.\n"
        "Other guilds are untouched.",
        "delete", assume_yes,
    ):
        return 1

    assert_not_running(path)
    if backup:
        back_up(path)

    db = sqlite3.connect(path)
    # guild_tables() reads rows by column name, so this connection needs the
    # same row factory the read-only one had.
    db.row_factory = sqlite3.Row
    try:
        # ON DELETE CASCADE is declared on most child tables but is inert
        # unless this pragma is on -- it is off by default in every new
        # connection, including this one.
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("BEGIN")
        deleted = 0
        # Children first, then guild_config. Doing it the other way round works
        # only where a cascade is declared, and a table that predates the
        # convention would be left orphaned.
        ordered = [t for t in guild_tables(db) if t != "guild_config"]
        ordered.append("guild_config")
        for table in ordered:
            deleted += db.execute(
                f"DELETE FROM {table} WHERE guild_id = ?", (guild_id,)
            ).rowcount
        db.execute("COMMIT")
        db.execute("VACUUM")
    except Exception:
        db.execute("ROLLBACK")
        raise
    finally:
        db.close()

    print(f"\n  deleted  {deleted} row(s) for guild {guild_id}")
    print("  The bot recreates that guild's config row on its next command.")
    return 0


def do_all(path: Path, assume_yes: bool, backup: bool, dry_run: bool) -> int:
    db = open_db(path)
    try:
        rows = sum(count(db, t) for t in all_tables(db))
        guilds = [r[0] for r in db.execute("SELECT guild_id FROM guild_config")]
    finally:
        db.close()

    targets = sibling_files(path)
    print(f"\nAbout to delete {len(targets)} file(s):")
    for target in targets:
        print(f"  {target.name:<20} {target.stat().st_size / 1024:>8.0f} KiB")
    print(f"\n  {rows} row(s) across {len(guilds)} configured guild(s)")
    print("  Kept: trusted_domains.txt, phishing_blocklist.txt, data/fonts/")

    if dry_run:
        print("\n--dry-run: nothing was deleted.")
        return 0

    if not confirm(
        "This wipes EVERYTHING: every guild's settings, all levels and "
        "economy balances, every warning, case and note, all tags, giveaways "
        "and reminders.\nThe schema comes back on the next boot; the data does "
        "not.",
        "wipe everything", assume_yes,
    ):
        return 1

    assert_not_running(path)
    if backup:
        back_up(path)

    for target in targets:
        target.unlink()
        print(f"  deleted  {target.name}")

    print("\n  Done. Start the bot -- it will apply all migrations and come up "
          "empty.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reset.py",
        description="Reset the Deezee bot database. Stop the bot first.",
        epilog="Backups land in data/backups/ and are git-ignored.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true",
                      help="show what the database holds; change nothing")
    mode.add_argument("--guild", type=int, metavar="ID",
                      help="delete one guild's rows, leaving other guilds alone")
    mode.add_argument("--all", action="store_true",
                      help="delete the database outright")

    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"database path (default: {DEFAULT_DB})")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt")
    parser.add_argument("--no-backup", action="store_true",
                        help="do not write a backup first")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would happen, then stop")

    args = parser.parse_args(argv)
    path = args.db.resolve()

    if args.list:
        return do_list(path)
    if args.guild is not None:
        return do_guild(path, args.guild, args.yes, not args.no_backup,
                        args.dry_run)
    return do_all(path, args.yes, not args.no_backup, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
