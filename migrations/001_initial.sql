-- 001_initial: core tables.
--
-- Scope is deliberately narrow: per-guild settings, the moderation record, and
-- the scheduler queue. Cog-specific tables (leveling, tags, giveaways, ...)
-- arrive in their own migrations as those cogs are built.
--
-- This file is shipped. Never edit it -- corrections go in a new migration.
--
-- All timestamps are Unix epoch seconds, UTC, stored as INTEGER. SQLite has no
-- date type, and integers sort, compare and index correctly without a parser.

-- ---------------------------------------------------------------------------
-- guild_config: one row per guild, every scalar setting.
--
-- Later migrations extend this with ALTER TABLE ... ADD COLUMN, which SQLite
-- supports cheaply. Booleans are INTEGER 0/1; SQLite has no BOOLEAN type.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id                  INTEGER PRIMARY KEY,

    -- Command prefix for this guild. Slash commands ignore it entirely.
    prefix                    TEXT    NOT NULL DEFAULT '?',

    -- Log destinations. NULL means that log category is disabled.
    mod_log_channel_id        INTEGER,
    message_log_channel_id    INTEGER,
    member_log_channel_id     INTEGER,
    server_log_channel_id     INTEGER,
    voice_log_channel_id      INTEGER,

    -- Fallback mute role, used when a punishment outruns Discord's 28-day
    -- native timeout cap.
    mute_role_id              INTEGER,

    -- DM the target before a ban/kick/mute/warn lands. Failure is never fatal.
    dm_on_punish              INTEGER NOT NULL DEFAULT 1,

    -- Delete the invoking message after a successful prefix command.
    delete_invocation         INTEGER NOT NULL DEFAULT 0,

    -- Monotonic per-guild case number. Incremented inside the same transaction
    -- that writes the case, so two concurrent bans cannot collide.
    case_counter              INTEGER NOT NULL DEFAULT 0,

    -- IANA name, used to render human-readable times in embeds.
    timezone                  TEXT    NOT NULL DEFAULT 'UTC',

    created_at                INTEGER NOT NULL,
    updated_at                INTEGER NOT NULL
);

-- ---------------------------------------------------------------------------
-- mod_cases: the permanent moderation record.
--
-- Never auto-pruned. This is the one table worth its disk indefinitely.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mod_cases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,

    -- Per-guild, human-facing case number shown in embeds ("Case #42").
    case_number     INTEGER NOT NULL,

    -- ban, softban, unban, kick, mute, unmute, timeout, warn, note, ...
    action          TEXT    NOT NULL,

    target_id       INTEGER NOT NULL,
    -- Denormalised on purpose: the account may be deleted or renamed by the
    -- time anyone reads the case, and the log should still say who it was.
    target_tag      TEXT    NOT NULL,

    moderator_id    INTEGER NOT NULL,
    moderator_tag   TEXT    NOT NULL,

    reason          TEXT,

    created_at      INTEGER NOT NULL,
    -- NULL for permanent actions. Set for tempban/tempmute/timeout.
    expires_at      INTEGER,

    -- 1 while the punishment is in force. Cleared on unban/unmute or expiry.
    active          INTEGER NOT NULL DEFAULT 1,

    -- The mod-log message, so ?reason can edit the original embed in place.
    log_message_id  INTEGER,

    UNIQUE (guild_id, case_number),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mod_cases_target
    ON mod_cases (guild_id, target_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mod_cases_moderator
    ON mod_cases (guild_id, moderator_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mod_cases_active
    ON mod_cases (guild_id, active, expires_at) WHERE active = 1;

-- ---------------------------------------------------------------------------
-- warnings: separate from mod_cases because warnings drive the automod ladder.
--
-- A case row is written too, for the mod-log; this table is what ?warnings and
-- the escalation rules count. Cleared warnings stay as rows with active = 0 so
-- history survives a ?clearwarns.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS warnings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    moderator_id  INTEGER NOT NULL,
    reason        TEXT,
    created_at    INTEGER NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,

    -- Set when an automod rule issued the warning rather than a person.
    source        TEXT    NOT NULL DEFAULT 'manual',

    -- Links back to the mod-log case, if one was written.
    case_id       INTEGER,

    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE,
    FOREIGN KEY (case_id) REFERENCES mod_cases (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_warnings_user
    ON warnings (guild_id, user_id, active);

-- ---------------------------------------------------------------------------
-- notes: private staff notes. Never shown to the target, never DMed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    author_id   INTEGER NOT NULL,
    content     TEXT    NOT NULL,
    created_at  INTEGER NOT NULL,

    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_notes_user
    ON notes (guild_id, user_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- scheduled_actions: the timed-action queue.
--
-- Unmute, unban, temporary role removal, reminders, giveaway ends, lockdown
-- expiry, raid-mode auto-off. Rehydrated on every boot, so a restart does not
-- lose a timed punishment.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scheduled_actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,

    -- Handler key. A cog registers a coroutine under this name at load time.
    action_type   TEXT    NOT NULL,

    execute_at    INTEGER NOT NULL,

    -- JSON object of handler arguments. Opaque to the scheduler.
    payload       TEXT    NOT NULL DEFAULT '{}',

    -- pending | done | failed | cancelled
    status        TEXT    NOT NULL DEFAULT 'pending',

    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,

    created_at    INTEGER NOT NULL,
    completed_at  INTEGER,

    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- The scheduler's only hot query: the next pending row by due time.
CREATE INDEX IF NOT EXISTS idx_scheduled_pending
    ON scheduled_actions (execute_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_scheduled_lookup
    ON scheduled_actions (guild_id, action_type, status);

-- ---------------------------------------------------------------------------
-- blacklist: users, roles and channels the bot ignores.
--
-- guild_id 0 means bot-wide, set by an owner. It is 0 rather than NULL because
-- SQLite treats NULLs as distinct inside a UNIQUE constraint, which would let
-- the same entity be blacklisted globally any number of times.
--
-- A root owner can never be listed here; core.permissions refuses the write
-- rather than relying on this table to be well-formed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS blacklist (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL DEFAULT 0,
    entity_id    INTEGER NOT NULL,

    -- user | role | channel
    entity_type  TEXT    NOT NULL,

    reason       TEXT,
    added_by     INTEGER NOT NULL,
    created_at   INTEGER NOT NULL,

    UNIQUE (guild_id, entity_id, entity_type)
);

CREATE INDEX IF NOT EXISTS idx_blacklist_lookup
    ON blacklist (entity_id, entity_type);
