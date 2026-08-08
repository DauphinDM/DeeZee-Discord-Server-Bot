"""Per-guild settings, cached write-through.

Every command that reads a setting -- the prefix resolver runs on every single
message -- goes through this cache. Hitting SQLite for the prefix on each
message would be the bot's single hottest query, and the CPU budget does not
have room for it.

Writes update SQLite and the cache in one call, so a config panel never shows a
stale value after a change.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .database import Database

log = logging.getLogger(__name__)

#: Settings table. Column names are the setting names used everywhere else.
_TABLE = "guild_config"

#: Columns that are structural rather than settings. Writable through their own
#: dedicated code paths, never through ``set()``.
_READONLY = frozenset({"guild_id", "created_at", "updated_at", "case_counter"})


class GuildConfig:
    """Write-through cache over the ``guild_config`` table.

    One dict per guild, loaded on first touch and kept until eviction. At
    single-guild scale this is a few hundred bytes; the cache is bounded by the
    number of guilds the bot is allowed in, which the config file caps.
    """

    __slots__ = ("_db", "_cache", "_columns", "_defaults")

    def __init__(self, db: Database) -> None:
        self._db = db
        self._cache: dict[int, dict[str, Any]] = {}
        self._columns: frozenset[str] = frozenset()
        self._defaults: dict[str, Any] = {}

    async def prepare(self) -> None:
        """Read the table's shape from SQLite.

        Called once after migrations. Building the writable-column whitelist
        from ``PRAGMA table_info`` rather than a hardcoded list means a later
        migration that adds a setting needs no change here -- and means a
        setting name can be interpolated into an UPDATE only after it has been
        matched against real columns.
        """
        rows = await self._db.fetchall(f"PRAGMA table_info({_TABLE})")
        if not rows:
            raise RuntimeError(
                f"Table {_TABLE} does not exist. Migrations did not run."
            )

        self._columns = frozenset(row["name"] for row in rows)

        # SQLite reports defaults as SQL literals: strip the quoting so the
        # values match what a SELECT would return.
        defaults: dict[str, Any] = {}
        for row in rows:
            raw = row["dflt_value"]
            if raw is None:
                defaults[row["name"]] = None
                continue
            text = str(raw)
            if text.startswith("'") and text.endswith("'"):
                defaults[row["name"]] = text[1:-1]
            elif text.lstrip("-").isdigit():
                defaults[row["name"]] = int(text)
            else:
                defaults[row["name"]] = text
        self._defaults = defaults

        log.debug("Guild config has %d columns", len(self._columns))

    @property
    def settings(self) -> frozenset[str]:
        """Every writable setting name."""
        return frozenset(self._columns - _READONLY)

    # --- Reads -------------------------------------------------------------

    async def get(self, guild_id: int) -> dict[str, Any]:
        """Return every setting for a guild, creating the row if absent.

        The returned dict is the cached object. Treat it as read-only -- mutating
        it changes the cache without touching the database. Use :meth:`set`.
        """
        cached = self._cache.get(guild_id)
        if cached is not None:
            return cached

        row = await self._db.fetchone(
            f"SELECT * FROM {_TABLE} WHERE guild_id = ?", (guild_id,)
        )
        if row is None:
            row = await self._insert_defaults(guild_id)

        config = dict(row)
        self._cache[guild_id] = config
        return config

    async def value(self, guild_id: int, setting: str) -> Any:
        """Return one setting."""
        config = await self.get(guild_id)
        return config.get(setting)

    async def prefix(self, guild_id: int, fallback: str) -> str:
        """Return the guild's command prefix, never empty."""
        config = await self.get(guild_id)
        return config.get("prefix") or fallback

    async def channel_id(self, guild_id: int, setting: str) -> int | None:
        """Return a configured channel ID, or ``None`` if that log is disabled."""
        value = await self.value(guild_id, setting)
        return int(value) if value else None

    async def flag(self, guild_id: int, setting: str) -> bool:
        """Return a boolean setting. SQLite stores these as INTEGER 0/1."""
        return bool(await self.value(guild_id, setting))

    # --- Writes ------------------------------------------------------------

    async def set(self, guild_id: int, setting: str, value: Any) -> None:
        """Write one setting to SQLite and the cache together.

        Raises:
            KeyError: If ``setting`` is not a writable column. The name is
                validated against the real table before it is interpolated,
                because column names cannot be bound as parameters.
        """
        self._assert_writable(setting)
        await self.get(guild_id)  # Guarantees the row and the cache entry exist.

        now = int(time.time())
        await self._db.execute(
            f"UPDATE {_TABLE} SET {setting} = ?, updated_at = ? WHERE guild_id = ?",
            (value, now, guild_id),
        )
        entry = self._cache[guild_id]
        entry[setting] = value
        entry["updated_at"] = now

    async def set_many(self, guild_id: int, values: dict[str, Any]) -> None:
        """Write several settings atomically.

        Used by config panels that offer a single Save button: either every
        change lands or none does.
        """
        if not values:
            return
        for setting in values:
            self._assert_writable(setting)
        await self.get(guild_id)

        now = int(time.time())
        assignments = ", ".join(f"{name} = ?" for name in values)
        params = [*values.values(), now, guild_id]

        async with self._db.transaction() as conn:
            await conn.execute(
                f"UPDATE {_TABLE} SET {assignments}, updated_at = ? WHERE guild_id = ?",
                params,
            )

        entry = self._cache[guild_id]
        entry.update(values)
        entry["updated_at"] = now

    async def reset(self, guild_id: int, setting: str) -> None:
        """Restore one setting to its schema default."""
        self._assert_writable(setting)
        await self.set(guild_id, setting, self._defaults.get(setting))

    async def reset_all(self, guild_id: int) -> None:
        """Delete the guild's row and recreate it at defaults.

        Backs ``?configreset``. Deliberately destructive and gated behind a
        confirmation view at the command layer.
        """
        async with self._db.transaction() as conn:
            await conn.execute(f"DELETE FROM {_TABLE} WHERE guild_id = ?", (guild_id,))
        self._cache.pop(guild_id, None)
        await self.get(guild_id)
        log.info("Reset all settings for guild %s", guild_id)

    async def next_case_number(self, guild_id: int) -> int:
        """Increment and return the guild's case counter.

        Read and write happen in one transaction so two simultaneous bans cannot
        be handed the same case number.
        """
        await self.get(guild_id)
        async with self._db.transaction() as conn:
            await conn.execute(
                f"UPDATE {_TABLE} SET case_counter = case_counter + 1 WHERE guild_id = ?",
                (guild_id,),
            )
            async with conn.execute(
                f"SELECT case_counter FROM {_TABLE} WHERE guild_id = ?", (guild_id,)
            ) as cursor:
                row = await cursor.fetchone()

        number = int(row[0])
        self._cache[guild_id]["case_counter"] = number
        return number

    # --- Cache management --------------------------------------------------

    async def warm(self, guild_ids: list[int]) -> None:
        """Preload several guilds. Called on ready so the first command is fast."""
        for guild_id in guild_ids:
            await self.get(guild_id)
        log.info("Warmed config cache for %d guild(s)", len(guild_ids))

    def invalidate(self, guild_id: int) -> None:
        """Drop one guild from the cache; the next read reloads it."""
        self._cache.pop(guild_id, None)

    def clear(self) -> None:
        """Drop the whole cache."""
        self._cache.clear()

    # --- Internals ---------------------------------------------------------

    def _assert_writable(self, setting: str) -> None:
        if setting not in self._columns:
            raise KeyError(
                f"{setting!r} is not a guild setting. "
                f"Known settings: {', '.join(sorted(self.settings))}"
            )
        if setting in _READONLY:
            raise KeyError(f"{setting!r} is managed internally and cannot be set directly.")

    async def _insert_defaults(self, guild_id: int) -> Any:
        """Create a guild's row at schema defaults and return it."""
        now = int(time.time())
        await self._db.execute(
            f"INSERT OR IGNORE INTO {_TABLE} (guild_id, created_at, updated_at) "
            "VALUES (?, ?, ?)",
            (guild_id, now, now),
        )
        row = await self._db.fetchone(
            f"SELECT * FROM {_TABLE} WHERE guild_id = ?", (guild_id,)
        )
        if row is None:  # pragma: no cover - only reachable if the insert vanished
            raise RuntimeError(f"Could not create config row for guild {guild_id}.")
        log.info("Created default config for guild %s", guild_id)
        return row
