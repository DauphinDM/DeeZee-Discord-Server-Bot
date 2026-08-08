-- 015_fun: social profiles and reputation.
--
-- This file is shipped. Never edit it -- corrections go in a new migration.

-- ---------------------------------------------------------------------------
-- profiles: the bio behind ?profile, and the reputation counter behind ?rep.
--
-- Reputation is stored as a total plus the last time the member *gave* one,
-- because the cooldown belongs to the giver, not the receiver.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS profiles (
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,

    bio          TEXT    NOT NULL DEFAULT '',
    reputation   INTEGER NOT NULL DEFAULT 0,

    -- Epoch seconds of the last reputation this member handed out. In the
    -- database rather than in memory, so a restart is not a way to give twice.
    last_rep_at  INTEGER NOT NULL DEFAULT 0,

    created_at   INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (guild_id, user_id),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_profiles_rep ON profiles (guild_id, reputation DESC);

-- ---------------------------------------------------------------------------
-- reputation_log: who gave what to whom.
--
-- Kept so a moderator can see a brigade rather than only its result. Pruned
-- with the rest of the growth-bounded tables.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reputation_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    giver_id   INTEGER NOT NULL,
    target_id  INTEGER NOT NULL,
    reason     TEXT,
    created_at INTEGER NOT NULL,

    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rep_log_target
    ON reputation_log (guild_id, target_id, created_at DESC);
