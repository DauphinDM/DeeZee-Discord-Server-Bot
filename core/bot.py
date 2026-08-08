"""The bot class: startup order, intents, cog loading, guild gate.

Memory settings here are load-bearing, not stylistic. Chunking every guild's
member list at startup and caching it in full is the one thing that can push
this process past its 512 MiB ceiling, so it is off, and the caches that remain
are explicitly bounded. See ``docs/DESIGN.md`` section 10.1.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import discord
from discord.ext import commands

from . import errors, permissions
from .config import Config
from .constants import VACUUM_THRESHOLD_BYTES
from .database import Database
from .guildconfig import GuildConfig
from .migrations import apply_migrations
from .permissions import PermissionStore
from .scheduler import Scheduler

log = logging.getLogger(__name__)

#: Deleted/edited messages held in the gateway cache. discord.py defaults to
#: 1000; half that still covers message logging, because anything evicted still
#: arrives as a raw event.
MAX_MESSAGES = 500


def _build_intents() -> discord.Intents:
    """Intents this bot needs.

    All three privileged intents are required and are already enabled on the
    application. Everything else is the default set minus the parts that only
    cost memory.
    """
    intents = discord.Intents.default()
    intents.members = True  # joins/leaves, autorole, alt detection, hierarchy
    intents.message_content = True  # automod, prefix commands, ?import capture
    intents.presences = True  # member counts, ?userinfo status

    # Typing events fire constantly and nothing here reads them.
    intents.typing = False
    return intents


def _build_member_cache_flags() -> discord.MemberCacheFlags:
    """Bound the member cache.

    ``joined=True`` keeps members the bot has actually seen -- which is what
    hierarchy checks and moderation need -- while ``voice=False`` drops the
    voice-state cache the bot never reads. Combined with
    ``chunk_guilds_at_startup=False``, the cache grows with activity rather than
    with server size.
    """
    return discord.MemberCacheFlags(joined=True, voice=False)


async def _resolve_prefix(bot: DeezeeBot, message: discord.Message) -> list[str]:
    """Return the accepted prefixes for this message.

    Runs on every message the bot can see, so it must never touch the database
    directly -- the guild-config cache is what makes that true.
    """
    prefixes = commands.when_mentioned(bot, message)

    if message.guild is None:
        prefixes.append(bot.config.default_prefix)
        return prefixes

    try:
        prefixes.append(
            await bot.guild_config.prefix(message.guild.id, bot.config.default_prefix)
        )
    except Exception:
        # A prefix lookup failure must not make the bot deaf. Fall back and log.
        log.exception("Prefix lookup failed for guild %s", message.guild.id)
        prefixes.append(bot.config.default_prefix)

    return prefixes


class DeezeeBot(commands.Bot):
    """Deezee Server Bot.

    Attributes:
        config: Validated ``.env`` configuration.
        db: The SQLite connection wrapper.
        guild_config: Per-guild settings cache.
        permissions: Admin/mod/protected role sets and the blacklist.
        scheduler: Timed-action queue.
        started_at: Epoch seconds of process start, for ``?uptime``.
    """

    def __init__(self, config: Config) -> None:
        super().__init__(
            command_prefix=_resolve_prefix,
            intents=_build_intents(),
            member_cache_flags=_build_member_cache_flags(),
            # Members are fetched on demand instead of downloaded in bulk on
            # connect. Costs one API call for an unseen member; saves the tens
            # of megabytes a full member list would occupy.
            chunk_guilds_at_startup=False,
            max_messages=MAX_MESSAGES,
            case_insensitive=True,
            strip_after_prefix=True,
            # Nothing this bot sends pings anyone unless a command opts in.
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True, replied_user=False
            ),
            # The built-in help command is replaced by the paginated one in
            # cogs/utility.py, which knows about categories and permissions.
            help_command=None,
            # Owner checks route through core.permissions, which enforces the
            # root/secondary split discord.py has no concept of.
            owner_ids=set(config.all_owner_ids),
        )

        self.config = config
        self.db = Database(config.database_path)
        self.guild_config = GuildConfig(self.db)
        self.permissions = PermissionStore(self.db)
        self.scheduler = Scheduler(self.db)
        self.started_at = time.time()

        self._ready_once = False

    # --- Startup -----------------------------------------------------------

    async def setup_hook(self) -> None:
        """Prepare everything before the gateway connection is used.

        Order matters: the database must be migrated before any cog can query
        it, and cogs must be loaded before their persistent views or scheduler
        handlers can be registered.
        """
        await self.db.connect()
        applied = await apply_migrations(self.db, self.config.migrations_path)
        if applied:
            log.info(
                "Applied %d migration(s): %s",
                len(applied),
                ", ".join(m.label for m in applied),
            )

        # After migrations so it sees the final table shape. VACUUM runs at most
        # once per boot and only past the size threshold.
        await self.guild_config.prepare()
        if await self.db.maybe_vacuum():
            log.info("Database compacted on boot")
        else:
            log.debug(
                "VACUUM skipped (threshold is %d MiB)",
                VACUUM_THRESHOLD_BYTES // 1024 // 1024,
            )

        errors.install(self)
        # Before cogs: the global blacklist check has to be registered by the
        # time the first command is added.
        permissions.install(self)

        await self._load_cogs()
        self._register_persistent_views()
        await self._sync_commands()

    async def _load_cogs(self) -> None:
        """Load every cog in ``cogs/``.

        One cog failing logs a traceback and is skipped. A broken leveling cog
        should not take moderation down with it.
        """
        cogs_dir = self.config.project_root / "cogs"
        if not cogs_dir.is_dir():
            log.warning("No cogs directory at %s", cogs_dir)
            return

        loaded = 0
        failed = 0
        for path in sorted(cogs_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            module = f"cogs.{path.stem}"
            try:
                await self.load_extension(module)
            except Exception:
                failed += 1
                log.exception("Failed to load %s", module)
            else:
                loaded += 1
                log.debug("Loaded %s", module)

        log.info("Loaded %d cog(s), %d failed", loaded, failed)

    def _register_persistent_views(self) -> None:
        """Re-attach handlers to buttons that outlived the last restart.

        Reaction-role menus, giveaway entry buttons and the verification gate all
        sit on old messages with fixed ``custom_id``s. A cog that owns such a
        view exposes ``register_persistent_views(bot)``; this calls it.
        """
        registered = 0
        for name, cog in self.cogs.items():
            register = getattr(cog, "register_persistent_views", None)
            if register is None:
                continue
            try:
                register(self)
            except Exception:
                log.exception("Failed to register persistent views for cog %s", name)
            else:
                registered += 1

        if registered:
            log.info("Registered persistent views from %d cog(s)", registered)

    async def _sync_commands(self) -> None:
        """Sync the application-command tree to each allowed guild.

        Guild syncs apply immediately; a global sync can take up to an hour to
        propagate. Since the guild list is fixed in ``.env``, there is no reason
        to use the slow path.
        """
        for guild_id in sorted(self.config.allowed_guild_ids):
            guild = discord.Object(id=guild_id)
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
            except discord.HTTPException as exc:
                # A guild the bot has not been invited to yet is expected on a
                # first run and must not stop startup.
                log.warning("Could not sync commands to guild %s: %s", guild_id, exc)
            else:
                log.info("Synced %d command(s) to guild %s", len(synced), guild_id)

    # --- Ready -------------------------------------------------------------

    async def on_ready(self) -> None:
        """Finish startup once the gateway is live.

        Fires again after every reconnect, so the one-time work is guarded.
        """
        log.info("Connected as %s (ID %s)", self.user, self.user.id if self.user else "?")

        if self._ready_once:
            log.debug("Reconnected; skipping one-time startup")
            return
        self._ready_once = True

        # Shutdown can begin before this handler runs -- a crash during boot, or
        # ?shutdown issued while reconnecting. Starting the scheduler here would
        # then create a task against a database close() has already shut, so the
        # remaining startup is skipped rather than racing it.
        if self.is_closed():
            log.info("Shutdown began before startup finished; skipping the rest")
            return

        await self._enforce_guild_allowlist()
        await self.guild_config.warm([guild.id for guild in self.guilds])

        # Started after cogs have registered their handlers, so the first tick
        # can execute anything that came due while the bot was down.
        self.scheduler.start()
        pending = await self.scheduler.pending_count()
        if pending:
            log.info("Scheduler rehydrated with %d pending action(s)", pending)

        await self._set_presence()

        log.info(
            "Ready in %d guild(s) | %d command(s) | startup took %.1fs",
            len(self.guilds),
            len(self.commands),
            time.time() - self.started_at,
        )

    async def _enforce_guild_allowlist(self) -> None:
        """Leave any guild not listed in ``.env``.

        This bot is built for one server. Being added elsewhere means an invite
        leaked, and staying would mean serving config and moderation to a guild
        nobody here controls.
        """
        allowed = self.config.allowed_guild_ids
        for guild in list(self.guilds):
            if guild.id in allowed:
                continue
            log.warning(
                "Leaving unauthorised guild %s (%s) -- not in GUILD_IDS", guild.name, guild.id
            )
            try:
                await guild.leave()
            except discord.HTTPException as exc:
                log.error("Could not leave guild %s: %s", guild.id, exc)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Leave immediately if the new guild is not on the allowlist."""
        if guild.id in self.config.allowed_guild_ids:
            log.info("Joined authorised guild %s (%s)", guild.name, guild.id)
            await self.guild_config.get(guild.id)
            return

        log.warning("Left unauthorised guild %s (%s) on join", guild.name, guild.id)
        try:
            await guild.leave()
        except discord.HTTPException as exc:
            log.error("Could not leave guild %s: %s", guild.id, exc)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """Drop the cached config for a guild the bot is no longer in.

        Without this the cache would keep serving settings for a guild whose row
        may since have been deleted, and a re-invite would be answered from a
        stale dict rather than from disk.
        """
        self.guild_config.invalidate(guild.id)
        self.permissions.invalidate(guild.id)
        log.info("Removed from guild %s (%s); caches dropped", guild.name, guild.id)

    async def _set_presence(self) -> None:
        """Show the prefix in the bot's status."""
        try:
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"{self.config.default_prefix}help",
                ),
                status=discord.Status.online,
            )
        except discord.HTTPException as exc:
            log.warning("Could not set presence: %s", exc)

    # --- Helpers -----------------------------------------------------------

    @property
    def uptime(self) -> float:
        """Seconds since the process started."""
        return time.time() - self.started_at

    @property
    def dev_guild_id(self) -> int | None:
        return self.config.dev_guild_id

    def is_dev_guild(self, guild: discord.Guild | int | None) -> bool:
        """True in the development guild, where confirmations are shortened."""
        if guild is None or self.config.dev_guild_id is None:
            return False
        guild_id = guild if isinstance(guild, int) else guild.id
        return guild_id == self.config.dev_guild_id

    async def get_or_fetch_member(
        self, guild: discord.Guild, user_id: int
    ) -> discord.Member | None:
        """Return a member from cache, falling back to one API call.

        Necessary because startup chunking is off: a member the bot has not seen
        since boot is simply not cached, and every moderation command needs to
        resolve one.
        """
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None
        except discord.HTTPException as exc:
            log.warning("Could not fetch member %s in guild %s: %s", user_id, guild.id, exc)
            return None

    # --- Shutdown ----------------------------------------------------------

    async def close(self) -> None:
        """Shut down cleanly: stop the scheduler, close the database, disconnect.

        The database is closed last so a handler finishing mid-shutdown still
        has somewhere to write.
        """
        log.info("Shutting down")
        await self.scheduler.stop()
        await super().close()
        await self.db.close()


def project_paths(config: Config) -> tuple[Path, Path, Path]:
    """Create and return the directories the bot writes to.

    Wispbyte ships an empty persistent volume, so these have to exist before
    anything tries to open a file inside them.
    """
    for path in (config.data_path, config.logs_path, config.database_path.parent):
        path.mkdir(parents=True, exist_ok=True)
    return config.data_path, config.logs_path, config.database_path.parent
