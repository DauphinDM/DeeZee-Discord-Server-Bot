-- 011_serverconfig: welcome and goodbye messages, command and module gating.
--
-- This file is shipped. Never edit it -- corrections go in a new migration.

-- ---------------------------------------------------------------------------
-- Welcome and goodbye.
--
-- Both are off until a channel is set. ``*_embed`` switches between a plain
-- message and an embed; ``welcome_dm`` sends the same text to the joiner as
-- well, which is where rules usually go.
-- ---------------------------------------------------------------------------
ALTER TABLE guild_config ADD COLUMN welcome_channel_id INTEGER;
ALTER TABLE guild_config ADD COLUMN welcome_message TEXT NOT NULL
    DEFAULT 'Welcome {mention} to **{server}**! You are member #{count}.';
ALTER TABLE guild_config ADD COLUMN welcome_embed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE guild_config ADD COLUMN welcome_dm INTEGER NOT NULL DEFAULT 0;
ALTER TABLE guild_config ADD COLUMN welcome_dm_message TEXT NOT NULL DEFAULT '';

-- Seconds after which the welcome message deletes itself. 0 keeps it.
ALTER TABLE guild_config ADD COLUMN welcome_delete_after INTEGER NOT NULL DEFAULT 0;

ALTER TABLE guild_config ADD COLUMN goodbye_channel_id INTEGER;
ALTER TABLE guild_config ADD COLUMN goodbye_message TEXT NOT NULL
    DEFAULT '{user} has left **{server}**.';
ALTER TABLE guild_config ADD COLUMN goodbye_embed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE guild_config ADD COLUMN goodbye_delete_after INTEGER NOT NULL DEFAULT 0;

-- ---------------------------------------------------------------------------
-- disabled_commands / disabled_modules: the off switches.
--
-- Stored as exceptions, so a command that has never been touched is simply
-- absent and costs nothing to check. Owner-tier commands ignore both tables --
-- a command that could switch itself back on must not be switchable off.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS disabled_commands (
    guild_id   INTEGER NOT NULL,
    command    TEXT    NOT NULL,
    disabled_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,

    PRIMARY KEY (guild_id, command),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS disabled_modules (
    guild_id    INTEGER NOT NULL,
    module      TEXT    NOT NULL,
    disabled_by INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,

    PRIMARY KEY (guild_id, module),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- command_overrides: per-command allow/deny for a role, channel or member.
--
-- ``allow`` is 1 for an explicit allow and 0 for an explicit deny. A deny always
-- wins over an allow, because the safe reading of two contradictory rules is the
-- restrictive one.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS command_overrides (
    guild_id    INTEGER NOT NULL,
    command     TEXT    NOT NULL,
    entity_id   INTEGER NOT NULL,
    entity_type TEXT    NOT NULL,
    allow       INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,

    PRIMARY KEY (guild_id, command, entity_id, entity_type),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_overrides_command
    ON command_overrides (guild_id, command);

-- ---------------------------------------------------------------------------
-- config_snapshots: what a setting looked like before something changed it.
--
-- Written by ?configimport and by ?import review, and read by ?import undo.
-- Kept for seven days, then pruned.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS config_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,

    -- Which part of the configuration this snapshot covers.
    section    TEXT    NOT NULL,

    -- JSON object of the previous values.
    payload    TEXT    NOT NULL,

    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,

    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_snapshots_section
    ON config_snapshots (guild_id, section, created_at DESC);
