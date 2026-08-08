"""Shared constants: colours, emoji, limits and timeouts.

Every magic number the UI and the moderation stack rely on lives here so a
change is one edit rather than a grep.
"""

from __future__ import annotations

from typing import Final

# --- Embed colours ---------------------------------------------------------
# Plain ints so this module stays importable without discord.py, which keeps
# services/ unit-testable.

COLOUR_DEFAULT: Final[int] = 0x5865F2  # Discord blurple: neutral panels
COLOUR_SUCCESS: Final[int] = 0x2ECC71
COLOUR_WARNING: Final[int] = 0xF1C40F
COLOUR_ERROR: Final[int] = 0xE74C3C
COLOUR_INFO: Final[int] = 0x3498DB
COLOUR_MUTED: Final[int] = 0x95A5A6

#: Mod-log embed colour per action, keyed by the value stored in mod_cases.action.
ACTION_COLOURS: Final[dict[str, int]] = {
    "ban": 0xE74C3C,
    "softban": 0xE67E22,
    "unban": 0x2ECC71,
    "kick": 0xE67E22,
    "mute": 0xF1C40F,
    "unmute": 0x2ECC71,
    "timeout": 0xF1C40F,
    "untimeout": 0x2ECC71,
    "warn": 0xF39C12,
    "unwarn": 0x2ECC71,
    "note": 0x95A5A6,
    "purge": 0x9B59B6,
    "lockdown": 0x34495E,
    "unlock": 0x2ECC71,
    "slowmode": 0x3498DB,
    "nickname": 0x3498DB,
    "role_add": 0x3498DB,
    "role_remove": 0x95A5A6,
}

# --- Emoji -----------------------------------------------------------------
# Unicode only. Custom emoji would tie the bot to one guild's emoji set.

EMOJI_SUCCESS: Final[str] = "\N{WHITE HEAVY CHECK MARK}"
EMOJI_ERROR: Final[str] = "\N{CROSS MARK}"
EMOJI_WARNING: Final[str] = "\N{WARNING SIGN}\N{VARIATION SELECTOR-16}"
EMOJI_INFO: Final[str] = "\N{INFORMATION SOURCE}\N{VARIATION SELECTOR-16}"
EMOJI_LOADING: Final[str] = "\N{HOURGLASS WITH FLOWING SAND}"
EMOJI_LOCKED: Final[str] = "\N{LOCK}"
EMOJI_UNLOCKED: Final[str] = "\N{OPEN LOCK}"
EMOJI_ON: Final[str] = "\N{LARGE GREEN CIRCLE}"
EMOJI_OFF: Final[str] = "\N{LARGE RED CIRCLE}"

# Paginator controls.
EMOJI_FIRST: Final[str] = "\N{BLACK LEFT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}"
EMOJI_PREVIOUS: Final[str] = "\N{BLACK LEFT-POINTING TRIANGLE}"
EMOJI_NEXT: Final[str] = "\N{BLACK RIGHT-POINTING TRIANGLE}"
EMOJI_LAST: Final[str] = "\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}"
EMOJI_STOP: Final[str] = "\N{BLACK SQUARE FOR STOP}\N{VARIATION SELECTOR-16}"

# --- Interaction timeouts (seconds) ----------------------------------------

#: Config panels. Long enough to read an embed and think, short enough that a
#: forgotten panel does not sit live for an hour.
PANEL_TIMEOUT: Final[float] = 180.0

#: Confirm/cancel on destructive actions. Timeout counts as cancel.
CONFIRM_TIMEOUT: Final[float] = 30.0

#: Same, in the dev guild, where you are testing rather than deliberating.
CONFIRM_TIMEOUT_DEV: Final[float] = 10.0

#: Paginated embeds.
PAGINATOR_TIMEOUT: Final[float] = 120.0

# --- Discord platform limits -----------------------------------------------
# Enforced by the API. Exceeding any of these is a 400, so inputs are clamped
# against them before a request is built.

MAX_EMBED_DESCRIPTION: Final[int] = 4096
MAX_EMBED_FIELD_VALUE: Final[int] = 1024
MAX_EMBED_FIELDS: Final[int] = 25
MAX_EMBED_TOTAL: Final[int] = 6000
MAX_MESSAGE_LENGTH: Final[int] = 2000
MAX_SELECT_OPTIONS: Final[int] = 25
MAX_BUTTONS_PER_ROW: Final[int] = 5
MAX_ROWS_PER_VIEW: Final[int] = 5
MAX_MODAL_INPUTS: Final[int] = 5

#: ``Message.bulk_delete`` refuses messages older than this.
BULK_DELETE_MAX_AGE_DAYS: Final[int] = 14

#: Hard ceiling on a native Discord timeout. Anything longer needs the mute-role
#: fallback instead.
MAX_TIMEOUT_DAYS: Final[int] = 28

#: Discord's epoch, for decoding creation time out of a snowflake.
DISCORD_EPOCH_MS: Final[int] = 1_420_070_400_000

# --- Bot-imposed limits ----------------------------------------------------
# Chosen against the 0.5 GiB / 35% CPU budget, not against what Discord allows.

#: Messages ``?purge`` will scan in one invocation.
MAX_PURGE_LIMIT: Final[int] = 1000

#: Deleted messages kept per channel for ``?snipe``. In memory, never persisted.
SNIPE_BUFFER_SIZE: Final[int] = 5

#: Rows written per flush during an import. Keeps peak memory flat.
IMPORT_BATCH_SIZE: Final[int] = 200

#: Pause between batches in bulk jobs, so a mass action cannot pin the CPU.
BULK_ACTION_DELAY: Final[float] = 1.0

#: Seconds a user must wait between XP awards. Caps DB write rate under load.
XP_COOLDOWN_SECONDS: Final[int] = 60

#: VACUUM on boot once the database file passes this size.
VACUUM_THRESHOLD_BYTES: Final[int] = 200 * 1024 * 1024

# --- Timestamps ------------------------------------------------------------

#: Discord's ``<t:...:f>`` style suffixes, for readable relative times.
TS_SHORT_TIME: Final[str] = "t"
TS_LONG_DATE_TIME: Final[str] = "F"
TS_RELATIVE: Final[str] = "R"
