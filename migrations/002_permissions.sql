-- 002_permissions: role-based authority and protection lists.
--
-- The two owner tiers live in .env and are deliberately not represented here:
-- if root authority were a database row, a command could edit it. It cannot.
--
-- This file is shipped. Never edit it -- corrections go in a new migration.

-- ---------------------------------------------------------------------------
-- permission_roles: roles that grant the admin or mod tier.
--
-- Backs ?adminroles and ?modroles. A role can hold both tiers without conflict;
-- tier resolution takes the highest.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS permission_roles (
    guild_id    INTEGER NOT NULL,
    role_id     INTEGER NOT NULL,

    -- admin | mod
    tier        TEXT    NOT NULL,

    added_by    INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,

    PRIMARY KEY (guild_id, role_id, tier),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- protected_roles: roles whose holders cannot be moderated by this bot.
--
-- Backs ?protectedroles. Checked before role position, so a protected role
-- shields its holders even from someone who outranks them.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS protected_roles (
    guild_id    INTEGER NOT NULL,
    role_id     INTEGER NOT NULL,
    added_by    INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,

    PRIMARY KEY (guild_id, role_id),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- permission_audit: every refused action against a root owner.
--
-- Written by core.permissions itself, not by a cog, so an attempt is recorded
-- even if the logging cog failed to load. Silent attempts are impossible.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS permission_audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    invoker_id   INTEGER NOT NULL,
    invoker_tag  TEXT    NOT NULL,
    target_id    INTEGER NOT NULL,
    target_tag   TEXT    NOT NULL,

    -- The command that was attempted, e.g. "ban".
    action       TEXT    NOT NULL,

    -- Why it was refused, e.g. "root_owner_protected".
    refusal      TEXT    NOT NULL,

    created_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_permission_audit_guild
    ON permission_audit (guild_id, created_at DESC);
