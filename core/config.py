"""Typed configuration loader.

Everything the bot needs to start comes from the ``.env`` file and is validated
here, once, before the gateway connection is opened. A malformed value is a
startup failure with a readable message -- never a surprise ``AttributeError``
three hours into a run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Discord snowflakes are 64-bit IDs. The lower bound rules out obviously bogus
# values (0, 1, a truncated paste) without hardcoding a date; the upper bound is
# the maximum unsigned 64-bit integer.
_MIN_SNOWFLAKE = 10**15
_MAX_SNOWFLAKE = (1 << 64) - 1


class ConfigurationError(RuntimeError):
    """Raised when ``.env`` is missing a value or holds an unusable one."""


@dataclass(frozen=True, slots=True)
class Config:
    """Validated runtime configuration.

    Frozen because nothing should rewrite configuration at runtime. The owner
    lists in particular are only changeable by editing ``.env`` and restarting,
    which is what makes root authority impossible to strip from inside Discord.
    """

    token: str
    guild_ids: frozenset[int]
    dev_guild_id: int | None
    root_owner_ids: frozenset[int]
    owner_ids: frozenset[int]
    default_prefix: str
    database_path: Path
    log_level: str
    google_safe_browsing_key: str | None
    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)

    @property
    def allowed_guild_ids(self) -> frozenset[int]:
        """Every guild the bot is permitted to stay in, dev guild included."""
        if self.dev_guild_id is None:
            return self.guild_ids
        return self.guild_ids | {self.dev_guild_id}

    @property
    def all_owner_ids(self) -> frozenset[int]:
        """Root owners plus secondary owners. Order of authority lives in
        ``core.permissions``; this is only the membership union."""
        return self.root_owner_ids | self.owner_ids

    def is_root_owner(self, user_id: int) -> bool:
        """True if this user has absolute, unconditional authority."""
        return user_id in self.root_owner_ids

    def is_owner(self, user_id: int) -> bool:
        """True for root owners and secondary owners alike."""
        return user_id in self.root_owner_ids or user_id in self.owner_ids

    @property
    def migrations_path(self) -> Path:
        return self.project_root / "migrations"

    @property
    def data_path(self) -> Path:
        return self.project_root / "data"

    @property
    def logs_path(self) -> Path:
        return self.project_root / "logs"


def _require(key: str) -> str:
    """Fetch a mandatory value, or fail with a message naming the key."""
    raw = os.getenv(key, "").strip()
    if not raw:
        raise ConfigurationError(
            f"{key} is missing or empty in .env. Copy .env.example and fill it in."
        )
    return raw


def _parse_snowflake(raw: str, key: str) -> int:
    """Parse a single Discord ID, rejecting anything that cannot be one."""
    raw = raw.strip()
    if not raw.isdigit():
        raise ConfigurationError(
            f"{key} contains {raw!r}, which is not a Discord ID. "
            "IDs are digits only -- enable Developer Mode in Discord and use "
            "'Copy ID'."
        )
    value = int(raw)
    if not _MIN_SNOWFLAKE <= value <= _MAX_SNOWFLAKE:
        raise ConfigurationError(
            f"{key} contains {raw!r}, which is outside the valid Discord ID range. "
            "It looks truncated or mistyped."
        )
    return value


def _parse_snowflake_set(key: str, *, required: bool) -> frozenset[int]:
    """Parse a comma-separated ID list into a set of validated snowflakes."""
    raw = os.getenv(key, "").strip()
    if not raw:
        if required:
            raise ConfigurationError(f"{key} is missing or empty in .env.")
        return frozenset()
    ids = {_parse_snowflake(part, key) for part in raw.split(",") if part.strip()}
    if required and not ids:
        raise ConfigurationError(f"{key} is present but lists no usable IDs.")
    return frozenset(ids)


def load_config(env_file: Path | None = None) -> Config:
    """Read ``.env``, validate every value, and return an immutable ``Config``.

    Args:
        env_file: Explicit path to the environment file. Defaults to ``.env``
            beside this project's root, which is what running on Wispbyte uses.

    Raises:
        ConfigurationError: On any missing or unusable value. The message is
            written for a human who is about to go fix the file.
    """
    project_root = Path(__file__).resolve().parent.parent
    env_path = env_file or (project_root / ".env")

    # override=True so a stale shell variable never silently beats the file the
    # user just edited. On a panel host the environment is not ours to trust.
    load_dotenv(env_path, override=True)

    token = _require("DISCORD_TOKEN")
    if token.count(".") != 2:
        raise ConfigurationError(
            "DISCORD_TOKEN does not look like a bot token (expected three "
            "dot-separated segments). Reset it in the Discord Developer Portal "
            "under Bot -> Reset Token."
        )

    guild_ids = _parse_snowflake_set("GUILD_IDS", required=True)

    dev_raw = os.getenv("DEV_GUILD_ID", "").strip()
    dev_guild_id = _parse_snowflake(dev_raw, "DEV_GUILD_ID") if dev_raw else None

    root_owner_ids = _parse_snowflake_set("ROOT_OWNER_IDS", required=True)
    owner_ids = _parse_snowflake_set("OWNER_IDS", required=False)

    # A secondary owner who is also root is harmless but confusing: the root
    # tier already outranks everything. Strip the overlap so the two lists mean
    # exactly what they say.
    owner_ids = frozenset(owner_ids - root_owner_ids)

    prefix = os.getenv("DEFAULT_PREFIX", "?").strip()
    if not prefix:
        raise ConfigurationError("DEFAULT_PREFIX cannot be empty.")
    if len(prefix) > 5:
        raise ConfigurationError(
            f"DEFAULT_PREFIX {prefix!r} is longer than 5 characters. "
            "Long prefixes are unusable in practice."
        )

    db_raw = os.getenv("DATABASE_PATH", "data/deezee.db").strip() or "data/deezee.db"
    database_path = Path(db_raw)
    if not database_path.is_absolute():
        database_path = project_root / database_path

    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError(
            f"LOG_LEVEL {log_level!r} is not a logging level. "
            "Use DEBUG, INFO, WARNING, ERROR or CRITICAL."
        )

    # Optional. Absent means link scanning falls back to the bundled trusted and
    # phishing lists, which still work offline.
    gsb_key = os.getenv("GOOGLE_SAFE_BROWSING_KEY", "").strip() or None

    return Config(
        token=token,
        guild_ids=guild_ids,
        dev_guild_id=dev_guild_id,
        root_owner_ids=root_owner_ids,
        owner_ids=owner_ids,
        default_prefix=prefix,
        database_path=database_path,
        log_level=log_level,
        google_safe_browsing_key=gsb_key,
        project_root=project_root,
    )
