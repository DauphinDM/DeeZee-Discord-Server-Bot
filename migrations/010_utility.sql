-- 010_utility: polls, reminders, highlights, AFK, starboard, suggestions.
--
-- This file is shipped. Never edit it -- corrections go in a new migration.

-- ---------------------------------------------------------------------------
-- Starboard.
-- ---------------------------------------------------------------------------
ALTER TABLE guild_config ADD COLUMN starboard_channel_id INTEGER;
ALTER TABLE guild_config ADD COLUMN starboard_emoji TEXT NOT NULL DEFAULT '⭐';
ALTER TABLE guild_config ADD COLUMN starboard_threshold INTEGER NOT NULL DEFAULT 3;

-- Whether the author's own star counts. Off by default: a self-star is the one
-- vote that says nothing about whether anyone else liked it.
ALTER TABLE guild_config ADD COLUMN starboard_selfstar INTEGER NOT NULL DEFAULT 0;

-- Whether messages from age-restricted channels may be starred out of them.
ALTER TABLE guild_config ADD COLUMN starboard_nsfw INTEGER NOT NULL DEFAULT 0;

-- ---------------------------------------------------------------------------
-- Suggestions and announcements.
-- ---------------------------------------------------------------------------
ALTER TABLE guild_config ADD COLUMN suggestions_channel_id INTEGER;

-- Role ?announce is allowed to ping without Mention Everyone.
ALTER TABLE guild_config ADD COLUMN announcement_role_id INTEGER;

-- ---------------------------------------------------------------------------
-- polls: question, options and window. Votes live in their own table so one
-- voter is one row and double-voting is a primary-key violation rather than a
-- thing the code has to remember to check.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS polls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,

    question   TEXT    NOT NULL,

    -- JSON list of option labels, in display order.
    options    TEXT    NOT NULL,

    -- 1 if a voter may pick more than one option.
    multi      INTEGER NOT NULL DEFAULT 0,

    ends_at    INTEGER,
    closed     INTEGER NOT NULL DEFAULT 0,

    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,

    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_polls_message ON polls (message_id);

CREATE TABLE IF NOT EXISTS poll_votes (
    poll_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    option_index INTEGER NOT NULL,

    PRIMARY KEY (poll_id, user_id, option_index),
    FOREIGN KEY (poll_id) REFERENCES polls (id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- reminders: the text and where to deliver it.
--
-- Firing is the scheduler's job; this table exists so ?reminders can list and
-- cancel them, which a bare scheduled_actions payload cannot do readably.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    guild_id     INTEGER,
    channel_id   INTEGER,
    jump_url     TEXT    NOT NULL DEFAULT '',

    text         TEXT    NOT NULL,
    remind_at    INTEGER NOT NULL,
    created_at   INTEGER NOT NULL,

    -- Set when it fires or is cancelled, so the row is history rather than a
    -- deletion.
    completed_at INTEGER,
    cancelled    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_reminders_pending
    ON reminders (user_id, completed_at, cancelled);

-- ---------------------------------------------------------------------------
-- highlights: per user, not per guild.
--
-- Someone who wants to know when their name comes up wants that everywhere they
-- are, and setting it once per server is the thing people forget to do.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS highlights (
    user_id    INTEGER NOT NULL,
    word       TEXT    NOT NULL,
    created_at INTEGER NOT NULL,

    PRIMARY KEY (user_id, word)
);

CREATE TABLE IF NOT EXISTS highlight_blocks (
    user_id     INTEGER NOT NULL,
    entity_id   INTEGER NOT NULL,
    entity_type TEXT    NOT NULL,

    PRIMARY KEY (user_id, entity_id, entity_type)
);

-- ---------------------------------------------------------------------------
-- afk: status shown when someone is mentioned.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS afk (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    message  TEXT    NOT NULL DEFAULT 'AFK',
    since    INTEGER NOT NULL,

    PRIMARY KEY (guild_id, user_id),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- starboard_messages: source message to its starboard post.
--
-- Keyed on the source so a second star edits the existing post instead of
-- creating a duplicate, which is the entire difficulty of a starboard.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS starboard_messages (
    guild_id       INTEGER NOT NULL,
    source_id      INTEGER NOT NULL,
    channel_id     INTEGER NOT NULL,
    star_message_id INTEGER,
    author_id      INTEGER NOT NULL,
    stars          INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL,

    PRIMARY KEY (guild_id, source_id),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_starboard_top ON starboard_messages (guild_id, stars DESC);

CREATE TABLE IF NOT EXISTS starboard_blacklist (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,

    PRIMARY KEY (guild_id, channel_id),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- suggestions: member submissions and their outcome.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS suggestions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    channel_id    INTEGER NOT NULL,
    message_id    INTEGER,

    author_id     INTEGER NOT NULL,
    author_tag    TEXT    NOT NULL,
    body          TEXT    NOT NULL,

    -- open | approved | denied | implemented
    status        TEXT    NOT NULL DEFAULT 'open',
    staff_id      INTEGER,
    staff_comment TEXT,

    created_at    INTEGER NOT NULL,

    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_suggestions_guild ON suggestions (guild_id, status);

CREATE TABLE IF NOT EXISTS suggestion_votes (
    suggestion_id INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,

    -- 1 for up, -1 for down. Stored as the value so a total is a SUM.
    vote          INTEGER NOT NULL,

    PRIMARY KEY (suggestion_id, user_id),
    FOREIGN KEY (suggestion_id) REFERENCES suggestions (id) ON DELETE CASCADE
);
