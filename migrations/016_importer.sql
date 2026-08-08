-- 016_importer: the migration staging area.
--
-- This file is shipped. Never edit it -- corrections go in a new migration.
--
-- Nothing an import reads goes anywhere near live configuration. Every
-- ?import command writes here; ?import review is the only thing that copies a
-- section across, and only after an explicit button press. Deezee never touches
-- the bots it is replacing, never removes them, and never edits their data.

-- ---------------------------------------------------------------------------
-- import_draft: one row per staged section.
--
-- ``payload`` is a JSON object whose shape depends on the section, because the
-- sections genuinely have nothing in common: a levels import is a list of
-- members, a modlog import is a list of cases, a welcome import is one string.
-- A second import of the same section replaces the first rather than appending,
-- so a re-run after fixing something does not double the data.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS import_draft (
    guild_id   INTEGER NOT NULL,

    -- levels | modlog | reactionroles | welcome | goodbye | autoresponses |
    -- tags | automod | logging
    section    TEXT    NOT NULL,

    payload    TEXT    NOT NULL,

    -- Where it came from, for the review screen: mee6, arcane, csv, capture,
    -- paste, or a channel name.
    source     TEXT    NOT NULL DEFAULT 'unknown',

    -- Rows the parser could not understand, kept so they can be reviewed by
    -- hand rather than silently dropped.
    unparsed   TEXT    NOT NULL DEFAULT '[]',

    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,

    -- Set once ?import review applies it. The row is kept as history.
    applied_at INTEGER,

    PRIMARY KEY (guild_id, section),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- import_capture: raw messages recorded during a ?import capture window.
--
-- Held rather than parsed on arrival, so a parser fix can be re-run against the
-- same capture instead of asking someone to sit through the window again.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS import_capture (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,

    author_id   INTEGER NOT NULL,
    author_name TEXT    NOT NULL,

    content     TEXT    NOT NULL DEFAULT '',

    -- JSON list of the message's embeds, since most bots answer with one.
    embeds      TEXT    NOT NULL DEFAULT '[]',

    created_at  INTEGER NOT NULL,

    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_capture_guild
    ON import_capture (guild_id, created_at DESC);
