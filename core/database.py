"""SQLite access layer.

One connection, WAL mode, explicit transactions. At one-guild scale a pool adds
contention rather than removing it, and the memory budget does not have room for
several connections' page caches.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Iterable, Sequence
from pathlib import Path
from typing import Any

import aiosqlite

from .constants import VACUUM_THRESHOLD_BYTES

log = logging.getLogger(__name__)

#: Applied on every connection, in this order.
_PRAGMAS: tuple[str, ...] = (
    # WAL lets the scheduler read while a command writes, and survives a hard
    # kill from the panel far better than the rollback journal.
    "PRAGMA journal_mode = WAL",
    # ON DELETE CASCADE only fires when this is on. SQLite defaults it off.
    "PRAGMA foreign_keys = ON",
    # Negative value means KiB rather than pages: 8 MiB, against the memory
    # budget. The default is unbounded in practice.
    "PRAGMA cache_size = -8000",
    # NORMAL is the correct pairing with WAL: durable across process death,
    # which is the failure mode a panel host actually produces.
    "PRAGMA synchronous = NORMAL",
    # Wait rather than raise if another writer holds the lock.
    "PRAGMA busy_timeout = 5000",
    # Checkpoint at roughly 4 MiB of WAL so the file does not creep.
    "PRAGMA wal_autocheckpoint = 1000",
    # Keep temporary B-trees in memory; they are small and the disk is slow.
    "PRAGMA temp_store = MEMORY",
)

#: Table and column names cannot be bound as parameters, so the few places that
#: must interpolate an identifier check it against this first.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Database:
    """Thin async wrapper over a single ``aiosqlite`` connection.

    The connection runs with ``isolation_level=None``, so nothing is implicitly
    wrapped in a transaction. Writes commit as they execute; multi-statement
    atomicity is opt-in through :meth:`transaction`.
    """

    __slots__ = ("path", "_conn", "_lock")

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        # Serialises explicit transactions. One connection cannot hold two
        # overlapping BEGIN blocks, and two coroutines interleaving on it would
        # silently commit each other's half-finished work. Created in connect()
        # so the lock binds to the loop the bot actually runs on.
        self._lock: asyncio.Lock | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """The live connection.

        Raises:
            RuntimeError: If :meth:`connect` has not completed.
        """
        if self._conn is None:
            raise RuntimeError("Database.connect() has not been awaited.")
        return self._conn

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> None:
        """Open the database file and apply every pragma."""
        self._lock = asyncio.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row

        for pragma in _PRAGMAS:
            await self._conn.execute(pragma)

        mode = await self.fetchval("PRAGMA journal_mode")
        if str(mode).lower() != "wal":
            # Some filesystems (notably network mounts) refuse WAL. The bot still
            # works, but concurrent reads during a write will block, so say so.
            log.warning(
                "Could not enable WAL mode (journal_mode is %r). "
                "The bot will run, but writes will block reads.",
                mode,
            )

        log.info("Database opened at %s", self.path)

    async def close(self) -> None:
        """Checkpoint the WAL and close cleanly.

        Truncating the WAL on shutdown keeps the on-disk footprint honest: an
        uncheckpointed WAL can sit at its high-water mark indefinitely.
        """
        if self._conn is None:
            return
        try:
            await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except aiosqlite.Error as exc:
            log.warning("WAL checkpoint on shutdown failed: %s", exc)
        await self._conn.close()
        self._conn = None
        log.info("Database closed")

    # --- Query helpers -----------------------------------------------------

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Cursor:
        """Run one statement and return its cursor.

        Use the cursor's ``lastrowid`` after an INSERT and ``rowcount`` after an
        UPDATE or DELETE.
        """
        return await self.conn.execute(sql, params)

    async def executemany(self, sql: str, params: Iterable[Sequence[Any]]) -> None:
        """Run one statement over many parameter sets.

        Wrapped in a transaction because SQLite would otherwise commit -- and
        fsync -- once per row.
        """
        async with self.transaction():
            await self.conn.executemany(sql, params)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        """Return the first row, or ``None``."""
        async with self.conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        """Return every row. Only for result sets known to be small."""
        async with self.conn.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    async def fetchval(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """Return the first column of the first row, or ``None``."""
        row = await self.fetchone(sql, params)
        return None if row is None else row[0]

    async def iterate(
        self, sql: str, params: Sequence[Any] = ()
    ) -> AsyncIterator[aiosqlite.Row]:
        """Stream rows one at a time.

        For anything that could be large -- leaderboards, case histories,
        import scans -- this keeps peak memory flat instead of materialising the
        whole result set.
        """
        async with self.conn.execute(sql, params) as cursor:
            async for row in cursor:
                yield row

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Run a block atomically.

        Commits on clean exit, rolls back on any exception (including
        ``CancelledError``), and holds a lock for the duration so two callers
        cannot interleave on the shared connection.
        """
        if self._lock is None:
            raise RuntimeError("Database.connect() has not been awaited.")
        async with self._lock:
            await self.conn.execute("BEGIN")
            try:
                yield self.conn
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            else:
                await self.conn.execute("COMMIT")

    # --- Maintenance -------------------------------------------------------

    async def maybe_vacuum(self) -> bool:
        """VACUUM if the file has grown past the threshold.

        Called once on boot. VACUUM rewrites the whole database, so it is far
        too expensive to run on a schedule -- but after months of pruning,
        reclaiming the free pages is what keeps the 1 GiB budget intact.

        Returns:
            True if a VACUUM ran.
        """
        try:
            size = self.path.stat().st_size
        except OSError:
            return False

        if size < VACUUM_THRESHOLD_BYTES:
            return False

        log.info("Database is %.1f MiB; running VACUUM", size / 1024 / 1024)
        await self.conn.execute("VACUUM")
        new_size = self.path.stat().st_size
        log.info(
            "VACUUM complete: %.1f MiB -> %.1f MiB",
            size / 1024 / 1024,
            new_size / 1024 / 1024,
        )
        return True

    async def table_columns(self, table: str) -> list[str]:
        """Column names of a table, in declaration order.

        Used to build the write whitelist in :mod:`core.guildconfig`, so that
        settings can be addressed by name without ever interpolating user input
        into SQL.

        Raises:
            ValueError: If ``table`` is not a bare identifier. An identifier
                cannot be passed as a bound parameter, so it is validated
                instead of trusted.
        """
        if not _IDENTIFIER_RE.match(table):
            raise ValueError(f"{table!r} is not a valid table name.")
        rows = await self.fetchall(f"PRAGMA table_info({table})")
        return [row["name"] for row in rows]
