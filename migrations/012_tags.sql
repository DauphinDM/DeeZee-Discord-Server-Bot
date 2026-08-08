-- 012_tags: tags, custom commands and autoresponses.
--
-- This file is shipped. Never edit it -- corrections go in a new migration.

-- ---------------------------------------------------------------------------
-- tags: member-owned snippets recalled by name.
--
-- ``alias_of`` makes a second name point at an existing tag rather than copying
-- its content, so editing the original updates every alias. A row is either a
-- real tag (content set, alias_of NULL) or an alias (the reverse).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,

    -- Stored lowercase. Lookup is case-insensitive, and two tags differing only
    -- in case would be indistinguishable to whoever tries to use them.
    name       TEXT    NOT NULL,

    content    TEXT    NOT NULL DEFAULT '',
    alias_of   INTEGER,

    owner_id   INTEGER NOT NULL,
    uses       INTEGER NOT NULL DEFAULT 0,

    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,

    UNIQUE (guild_id, name),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE,
    FOREIGN KEY (alias_of) REFERENCES tags (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tags_owner ON tags (guild_id, owner_id);
CREATE INDEX IF NOT EXISTS idx_tags_uses ON tags (guild_id, uses DESC);

-- ---------------------------------------------------------------------------
-- custom_commands: like a tag, but with role actions and restrictions.
--
-- The distinction from a tag is the point: a tag is content anybody may recall,
-- a custom command can hand out a role. They are separate tables because they
-- are separate permission stories.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS custom_commands (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    name             TEXT    NOT NULL,

    response         TEXT    NOT NULL DEFAULT '',
    is_embed         INTEGER NOT NULL DEFAULT 0,

    -- JSON lists of role IDs to grant and to take when the command runs.
    add_roles        TEXT    NOT NULL DEFAULT '[]',
    remove_roles     TEXT    NOT NULL DEFAULT '[]',

    -- NULL means anyone may run it.
    required_role_id INTEGER,

    -- NULL means it works anywhere.
    channel_id       INTEGER,

    delete_invocation INTEGER NOT NULL DEFAULT 0,
    enabled          INTEGER NOT NULL DEFAULT 1,
    uses             INTEGER NOT NULL DEFAULT 0,

    created_by       INTEGER NOT NULL,
    created_at       INTEGER NOT NULL,

    UNIQUE (guild_id, name),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- autoresponses: replies triggered by message content, with no prefix.
--
-- ``mode`` is contains | exact | startswith | endswith | regex. A regex is
-- validated against the catastrophic-backtracking guard in services/filters.py
-- before it is ever stored, so an unsafe pattern is a config-time error rather
-- than a pinned core at runtime.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS autoresponses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,

    trigger        TEXT    NOT NULL,
    response       TEXT    NOT NULL,
    mode           TEXT    NOT NULL DEFAULT 'contains',
    case_sensitive INTEGER NOT NULL DEFAULT 0,

    -- Reply to the triggering message rather than posting loose.
    reply          INTEGER NOT NULL DEFAULT 1,

    enabled        INTEGER NOT NULL DEFAULT 1,
    uses           INTEGER NOT NULL DEFAULT 0,

    created_by     INTEGER NOT NULL,
    created_at     INTEGER NOT NULL,

    UNIQUE (guild_id, trigger, mode),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- autoresponse_ignores: channels and roles autoresponses never fire for.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS autoresponse_ignores (
    guild_id    INTEGER NOT NULL,
    entity_id   INTEGER NOT NULL,
    entity_type TEXT    NOT NULL,

    PRIMARY KEY (guild_id, entity_id, entity_type),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);
