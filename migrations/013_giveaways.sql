-- 013_giveaways: giveaways, their entries, and scheduled messages.
--
-- This file is shipped. Never edit it -- corrections go in a new migration.

-- ---------------------------------------------------------------------------
-- giveaways: prize, window and entry requirements.
--
-- Requirements are stored rather than checked once at entry, because they are
-- checked twice: when someone presses the button, and again at the draw. A
-- member who qualified on Monday and lost the role on Tuesday should not win on
-- Wednesday.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS giveaways (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    channel_id        INTEGER NOT NULL,
    message_id        INTEGER,

    prize             TEXT    NOT NULL,
    winners           INTEGER NOT NULL DEFAULT 1,
    ends_at           INTEGER NOT NULL,

    required_role_id  INTEGER,
    required_level    INTEGER NOT NULL DEFAULT 0,
    required_messages INTEGER NOT NULL DEFAULT 0,

    -- pending | ended | cancelled
    status            TEXT    NOT NULL DEFAULT 'pending',

    created_by        INTEGER NOT NULL,
    created_at        INTEGER NOT NULL,

    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_giveaways_message ON giveaways (message_id);
CREATE INDEX IF NOT EXISTS idx_giveaways_status ON giveaways (guild_id, status);

CREATE TABLE IF NOT EXISTS giveaway_entries (
    giveaway_id INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    entered_at  INTEGER NOT NULL,

    PRIMARY KEY (giveaway_id, user_id),
    FOREIGN KEY (giveaway_id) REFERENCES giveaways (id) ON DELETE CASCADE
);

-- Winners are recorded so a reroll can exclude anyone already drawn.
CREATE TABLE IF NOT EXISTS giveaway_winners (
    giveaway_id INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    drawn_at    INTEGER NOT NULL,

    PRIMARY KEY (giveaway_id, user_id),
    FOREIGN KEY (giveaway_id) REFERENCES giveaways (id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- scheduled_messages: post once at a time, or on a repeating interval.
--
-- Covers Carl-bot's feeds and Sapphire's scheduled messages. Firing goes
-- through the same scheduler as timed punishments, so a restart cannot lose one.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scheduled_messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    channel_id       INTEGER NOT NULL,

    content          TEXT    NOT NULL,
    is_embed         INTEGER NOT NULL DEFAULT 0,

    next_at          INTEGER NOT NULL,

    -- 0 for a one-off. Otherwise the gap between repeats, in seconds.
    interval_seconds INTEGER NOT NULL DEFAULT 0,

    -- How many times it has posted, for the listing.
    posts            INTEGER NOT NULL DEFAULT 0,

    active           INTEGER NOT NULL DEFAULT 1,

    created_by       INTEGER NOT NULL,
    created_at       INTEGER NOT NULL,

    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_scheduled_messages_active
    ON scheduled_messages (guild_id, active);
