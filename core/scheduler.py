"""Timed action scheduler.

One task loop, one table. Unmute, unban, temporary role removal, reminders,
giveaway ends, lockdown expiry, raid-mode auto-off -- everything that has to
happen later goes through here.

The loop sleeps until the next due row rather than polling on a tick, so an idle
bot costs nothing, and every pending action is reloaded from SQLite on boot. A
Wispbyte restart does not lose a timed punishment; that is the entire point of
persisting the queue instead of holding ``asyncio`` timers in memory.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .database import Database

log = logging.getLogger(__name__)

#: An action handler: given the guild ID and the decoded payload, do the thing.
Handler = Callable[[int, dict[str, Any]], Awaitable[None]]

#: Retries before a row is marked failed and stops being retried. An action that
#: fails three times is failing for a structural reason -- the channel is gone,
#: the member left -- not a transient one.
MAX_ATTEMPTS = 3

#: Cap on how long the loop will sleep in one go. Without it a reminder set for
#: next month would be a month-long sleep, which no event loop should hold, and
#: which hides scheduler death until it is far too late to notice.
MAX_SLEEP_SECONDS = 300.0


class Scheduler:
    """Executes rows from ``scheduled_actions`` when they come due."""

    __slots__ = ("_db", "_handlers", "_task", "_wakeup", "_running")

    def __init__(self, db: Database) -> None:
        self._db = db
        self._handlers: dict[str, Handler] = {}
        self._task: asyncio.Task[None] | None = None
        self._wakeup = asyncio.Event()
        self._running = False

    # --- Registration ------------------------------------------------------

    def register(self, action_type: str, handler: Handler) -> None:
        """Bind a handler to an action type.

        Cogs call this in ``cog_load``. Registering the same type twice replaces
        the handler, which is what makes ``?reload`` work.
        """
        if action_type in self._handlers:
            log.debug("Replacing scheduler handler for %r", action_type)
        self._handlers[action_type] = handler

    def unregister(self, action_type: str) -> None:
        """Remove a handler. Pending rows of that type wait for it to come back."""
        self._handlers.pop(action_type, None)

    @property
    def registered_types(self) -> frozenset[str]:
        return frozenset(self._handlers)

    # --- Queue management --------------------------------------------------

    async def schedule(
        self,
        guild_id: int,
        action_type: str,
        execute_at: int,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Queue an action and wake the loop if it is now the soonest one.

        Args:
            guild_id: Guild the action belongs to.
            action_type: Handler key, registered by a cog.
            execute_at: Unix epoch seconds. A time in the past runs immediately.
            payload: JSON-serialisable handler arguments.

        Returns:
            The row ID, for later cancellation.
        """
        cursor = await self._db.execute(
            "INSERT INTO scheduled_actions "
            "(guild_id, action_type, execute_at, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                guild_id,
                action_type,
                int(execute_at),
                json.dumps(payload or {}),
                int(time.time()),
            ),
        )
        row_id = int(cursor.lastrowid or 0)

        # The loop may currently be asleep until a later time than this one.
        self._wakeup.set()

        log.debug(
            "Scheduled %s #%d for guild %s at %d", action_type, row_id, guild_id, execute_at
        )
        return row_id

    async def cancel(self, row_id: int) -> bool:
        """Cancel one pending action by row ID. Returns True if it was pending."""
        cursor = await self._db.execute(
            "UPDATE scheduled_actions SET status = 'cancelled', completed_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (int(time.time()), row_id),
        )
        cancelled = cursor.rowcount > 0
        if cancelled:
            self._wakeup.set()
        return cancelled

    async def cancel_matching(
        self, guild_id: int, action_type: str, **payload_filters: Any
    ) -> int:
        """Cancel every pending action of a type matching payload fields.

        Used when a punishment ends early: unbanning by hand should cancel the
        scheduled auto-unban rather than leave it to fire against nothing.

        Payload matching happens in Python because the values live inside a JSON
        column; the row count involved is tiny, so this is cheaper than making
        SQLite parse JSON.
        """
        rows = await self._db.fetchall(
            "SELECT id, payload FROM scheduled_actions "
            "WHERE guild_id = ? AND action_type = ? AND status = 'pending'",
            (guild_id, action_type),
        )

        to_cancel: list[int] = []
        for row in rows:
            payload = self._decode(row["payload"])
            if all(payload.get(key) == value for key, value in payload_filters.items()):
                to_cancel.append(row["id"])

        if not to_cancel:
            return 0

        placeholders = ", ".join("?" * len(to_cancel))
        await self._db.execute(
            f"UPDATE scheduled_actions SET status = 'cancelled', completed_at = ? "
            f"WHERE id IN ({placeholders})",
            (int(time.time()), *to_cancel),
        )
        self._wakeup.set()
        log.debug("Cancelled %d pending %s action(s)", len(to_cancel), action_type)
        return len(to_cancel)

    async def pending_count(self, guild_id: int | None = None) -> int:
        """How many actions are queued, optionally for one guild."""
        if guild_id is None:
            return int(
                await self._db.fetchval(
                    "SELECT COUNT(*) FROM scheduled_actions WHERE status = 'pending'"
                )
            )
        return int(
            await self._db.fetchval(
                "SELECT COUNT(*) FROM scheduled_actions "
                "WHERE status = 'pending' AND guild_id = ?",
                (guild_id,),
            )
        )

    async def prune(self, older_than_days: int = 30) -> int:
        """Delete finished rows past their retention window.

        Part of keeping the database inside the storage budget. Only rows that
        already ran are removed; pending work is untouched regardless of age.
        """
        cutoff = int(time.time()) - older_than_days * 86400
        cursor = await self._db.execute(
            "DELETE FROM scheduled_actions "
            "WHERE status IN ('done', 'cancelled', 'failed') AND completed_at < ?",
            (cutoff,),
        )
        removed = cursor.rowcount
        if removed:
            log.info("Pruned %d completed scheduled action(s)", removed)
        return removed

    # --- Lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the loop. Idempotent -- safe to call on every reconnect.

        Refuses to start against a closed database. The loop's first act is a
        query, so starting after shutdown has begun would raise on the very
        first tick and log a failure that means nothing.
        """
        if self._task is not None and not self._task.done():
            return
        if not self._db.is_connected:
            log.debug("Not starting the scheduler: the database is closed")
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="deezee-scheduler")
        log.info("Scheduler started")

    async def stop(self) -> None:
        """Stop the loop and wait for the current action to finish."""
        self._running = False
        if self._task is None:
            return
        self._wakeup.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        log.info("Scheduler stopped")

    # --- The loop ----------------------------------------------------------

    async def _run(self) -> None:
        """Execute due actions, then sleep until the next one is due."""
        # Everything already overdue runs on the first pass: this is the boot
        # rehydration, and it needs no special case.
        while self._running:
            try:
                delay = await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A bug in the loop body must not silently stop every future
                # timed action. Log it and keep going.
                log.exception("Scheduler tick failed; retrying in 30s")
                delay = 30.0

            if delay <= 0:
                continue

            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=delay)
            except asyncio.TimeoutError:
                # Normal path: nothing new arrived, the next action is now due.
                pass

    async def _tick(self) -> float:
        """Run everything due now.

        Returns:
            Seconds to sleep before the next check. 0 means run again at once.
        """
        now = int(time.time())

        due = await self._db.fetchall(
            "SELECT id, guild_id, action_type, payload, attempts FROM scheduled_actions "
            "WHERE status = 'pending' AND execute_at <= ? "
            "ORDER BY execute_at ASC LIMIT 25",
            (now,),
        )

        for row in due:
            if not self._running:
                break
            await self._execute(row)

        # More were due than the batch limit: come straight back for the rest.
        if len(due) == 25:
            return 0.0

        next_at = await self._db.fetchval(
            "SELECT MIN(execute_at) FROM scheduled_actions WHERE status = 'pending'"
        )
        if next_at is None:
            return MAX_SLEEP_SECONDS

        return max(0.0, min(float(next_at) - time.time(), MAX_SLEEP_SECONDS))

    async def _execute(self, row: Any) -> None:
        """Run one action and record the outcome.

        No failure here can propagate: the loop must survive a handler that
        raises, and the row must end in a terminal state or a retry, never in
        limbo.
        """
        row_id = row["id"]
        action_type = row["action_type"]
        handler = self._handlers.get(action_type)

        if handler is None:
            # The owning cog failed to load. Leave the row pending -- it will run
            # when the cog is back rather than being lost.
            log.warning(
                "No handler registered for scheduled action %r (row %d); leaving pending",
                action_type,
                row_id,
            )
            return

        payload = self._decode(row["payload"])

        try:
            await handler(row["guild_id"], payload)
        except Exception as exc:
            attempts = row["attempts"] + 1
            failed = attempts >= MAX_ATTEMPTS
            await self._db.execute(
                "UPDATE scheduled_actions SET attempts = ?, last_error = ?, "
                "status = ?, completed_at = ? WHERE id = ?",
                (
                    attempts,
                    f"{type(exc).__name__}: {exc}"[:500],
                    "failed" if failed else "pending",
                    int(time.time()) if failed else None,
                    row_id,
                ),
            )
            log.warning(
                "Scheduled action %s #%d failed (attempt %d/%d): %s",
                action_type,
                row_id,
                attempts,
                MAX_ATTEMPTS,
                exc,
                exc_info=failed,
            )
            if not failed:
                # Back off before the retry so a hard-failing action cannot spin
                # the loop. Pushing execute_at forward is what enforces it.
                await self._db.execute(
                    "UPDATE scheduled_actions SET execute_at = ? WHERE id = ?",
                    (int(time.time()) + 60 * attempts, row_id),
                )
            return

        await self._db.execute(
            "UPDATE scheduled_actions SET status = 'done', completed_at = ? WHERE id = ?",
            (int(time.time()), row_id),
        )
        log.debug("Executed scheduled action %s #%d", action_type, row_id)

    @staticmethod
    def _decode(raw: str | None) -> dict[str, Any]:
        """Decode a payload, tolerating a corrupted one.

        A row whose JSON will not parse should not stop the queue; the handler
        gets an empty payload and decides for itself whether it can proceed.
        """
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Scheduled action payload is not valid JSON: %r", raw[:120])
            return {}
        return decoded if isinstance(decoded, dict) else {}
