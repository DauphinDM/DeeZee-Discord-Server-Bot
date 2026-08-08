"""Logging configuration.

Two sinks: a rotating file for forensics and stdout for the Wispbyte console.
The rotation cap is deliberate -- the storage budget allows 15 MB of logs and
no more.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

# 5 MB per file, 3 files kept. Matches the storage budget in docs/DESIGN.md.
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-24s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str, logs_dir: Path) -> logging.Logger:
    """Install the file and console handlers and return the bot's own logger.

    Safe to call twice: existing handlers on the root logger are removed first,
    so a reload never doubles every line.

    Args:
        level: One of DEBUG/INFO/WARNING/ERROR/CRITICAL, already validated by
            :func:`core.config.load_config`.
        logs_dir: Directory for ``deezee.log``. Created if missing.

    Returns:
        The ``deezee`` logger. Cogs should use ``logging.getLogger(__name__)``
        instead; module names sit under this one automatically.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(getattr(logging, level))
    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "deezee.log",
        maxBytes=MAX_LOG_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # stdout, not stderr: the panel treats stderr output as a crash indicator.
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # discord.py's HTTP logger reports every request at DEBUG and every rate
    # limit at INFO. At our log volume that drowns everything useful, so it is
    # pinned a level higher than whatever the rest of the bot runs at.
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("discord.client").setLevel(logging.WARNING)

    return logging.getLogger("deezee")
