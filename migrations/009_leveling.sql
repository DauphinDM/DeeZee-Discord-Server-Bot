-- 009_leveling: XP, levels, level roles and XP blacklists.
--
-- This file is shipped. Never edit it -- corrections go in a new migration.

-- ---------------------------------------------------------------------------
-- Off by default. Turning leveling on is a decision about what a server is for,
-- not a default anyone should inherit.
-- ---------------------------------------------------------------------------
ALTER TABLE guild_config ADD COLUMN xp_enabled INTEGER NOT NULL DEFAULT 0;

-- XP granted per qualifying message, picked uniformly from this range. A range
-- rather than a constant so message count cannot be reversed from a total.
ALTER TABLE guild_config ADD COLUMN xp_min INTEGER NOT NULL DEFAULT 15;
ALTER TABLE guild_config ADD COLUMN xp_max INTEGER NOT NULL DEFAULT 25;

-- Seconds between awards, per member. This is the CPU control: it caps write
-- volume regardless of how fast the server talks.
ALTER TABLE guild_config ADD COLUMN xp_cooldown INTEGER NOT NULL DEFAULT 60;

-- Whole-number multiplier applied to every award, for events.
ALTER TABLE guild_config ADD COLUMN xp_multiplier INTEGER NOT NULL DEFAULT 1;

-- Where level-ups are announced: channel | current | dm | off
ALTER TABLE guild_config ADD COLUMN level_up_mode TEXT NOT NULL DEFAULT 'current';
ALTER TABLE guild_config ADD COLUMN level_up_channel_id INTEGER;

-- Supports {user}, {mention}, {level}, {server}, {xp}.
ALTER TABLE guild_config ADD COLUMN level_up_message TEXT NOT NULL
    DEFAULT '{mention} reached level **{level}**.';

-- 1 keeps every level role earned; 0 removes the previous one on promotion.
ALTER TABLE guild_config ADD COLUMN level_role_stack INTEGER NOT NULL DEFAULT 1;

-- Voice XP, granted per minute of qualifying voice time.
ALTER TABLE guild_config ADD COLUMN voice_xp_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE guild_config ADD COLUMN voice_xp_rate INTEGER NOT NULL DEFAULT 5;

-- ---------------------------------------------------------------------------
-- levels: one row per member who has ever earned XP.
--
-- ``xp`` is the lifetime total; the level is derived from it rather than stored,
-- so changing the curve never leaves the two disagreeing.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS levels (
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,

    xp            INTEGER NOT NULL DEFAULT 0,
    messages      INTEGER NOT NULL DEFAULT 0,
    voice_seconds INTEGER NOT NULL DEFAULT 0,

    -- Epoch seconds of the last award, for the cooldown. In the database rather
    -- than in memory so a restart cannot be used to farm XP.
    last_award_at INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (guild_id, user_id),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_levels_leaderboard ON levels (guild_id, xp DESC);

-- ---------------------------------------------------------------------------
-- level_roles: role granted on reaching a level.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS level_roles (
    guild_id INTEGER NOT NULL,
    level    INTEGER NOT NULL,
    role_id  INTEGER NOT NULL,

    PRIMARY KEY (guild_id, level),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- xp_blacklist: channels and roles that earn nothing.
--
-- Channels for bot-spam and counting games; roles for muted members, so a
-- punishment cannot be sat out productively.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS xp_blacklist (
    guild_id    INTEGER NOT NULL,
    entity_id   INTEGER NOT NULL,
    entity_type TEXT    NOT NULL,

    PRIMARY KEY (guild_id, entity_id, entity_type),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);
