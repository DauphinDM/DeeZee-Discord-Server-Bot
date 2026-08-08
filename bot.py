"""Entrypoint for Deezee Server Bot.

Run with ``python bot.py``. On Wispbyte this is the startup command; the panel
supervises the process, so this script stays in the foreground and never
daemonises.
"""

from __future__ import annotations

import asyncio
import sys

import discord

from core.bot import DeezeeBot, project_paths
from core.config import ConfigurationError, load_config
from core.logging_setup import setup_logging


def _fail(message: str) -> None:
    """Print a startup failure to stderr and exit non-zero.

    Used before logging exists, so it writes directly. A non-zero exit tells the
    panel this was a real failure rather than a clean stop.
    """
    print(f"\nSTARTUP FAILED\n{message}\n", file=sys.stderr)
    raise SystemExit(1)


async def _run() -> None:
    """Load configuration, build the bot, and run until disconnected."""
    try:
        config = load_config()
    except ConfigurationError as exc:
        _fail(str(exc))
        return

    project_paths(config)
    log = setup_logging(config.log_level, config.logs_path)

    log.info("=" * 70)
    log.info("Deezee Server Bot starting")
    log.info("Python %s | discord.py %s", sys.version.split()[0], discord.__version__)
    log.info("Guilds allowed: %s", ", ".join(str(g) for g in sorted(config.allowed_guild_ids)))
    log.info("Root owners: %d | secondary owners: %d",
             len(config.root_owner_ids), len(config.owner_ids))
    log.info("Database: %s", config.database_path)
    log.info("=" * 70)

    bot = DeezeeBot(config)

    try:
        # log_handler=None because core.logging_setup already configured the
        # root logger; letting discord.py add its own would double every line.
        await bot.start(config.token)
    except discord.LoginFailure:
        log.critical(
            "Discord rejected the token. Reset it in the Developer Portal "
            "(Bot -> Reset Token) and update DISCORD_TOKEN in .env."
        )
        raise SystemExit(1) from None
    except discord.PrivilegedIntentsRequired:
        log.critical(
            "A privileged intent is not enabled. In the Developer Portal, under "
            "Bot -> Privileged Gateway Intents, enable SERVER MEMBERS, MESSAGE "
            "CONTENT and PRESENCE."
        )
        raise SystemExit(1) from None
    finally:
        if not bot.is_closed():
            await bot.close()


def main() -> None:
    """Start the event loop and handle a manual stop gracefully."""
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        # Ctrl+C locally, or the panel's Stop button. Not an error.
        print("\nStopped.")


if __name__ == "__main__":
    main()
