-- 003_moderation: settings and state the moderation cog needs.
--
-- This file is shipped. Never edit it -- corrections go in a new migration.

-- ---------------------------------------------------------------------------
-- guild_config additions.
--
-- SQLite's ALTER TABLE ADD COLUMN is cheap: it rewrites no rows. This is the
-- normal way to extend settings in a later migration.
-- ---------------------------------------------------------------------------

-- DM sent to a member when they are banned. NULL means send the default text.
-- Supports {user}, {server}, {reason}, {moderator} and {duration}.
ALTER TABLE guild_config ADD COLUMN ban_message TEXT;

-- JSON array of channel IDs affected by ?lockdown. Empty array means every
-- text channel the bot can see.
ALTER TABLE guild_config ADD COLUMN lockdown_channels TEXT NOT NULL DEFAULT '[]';

-- ?purge asks for confirmation above this many messages.
ALTER TABLE guild_config ADD COLUMN purge_confirm_threshold INTEGER NOT NULL DEFAULT 100;

-- ---------------------------------------------------------------------------
-- channel_locks: what ?lock changed, so ?unlock can put it back exactly.
--
-- Discord has three states for a permission overwrite -- allow, deny and unset
-- -- and "unset" is not the same as "allow". Storing the previous value is the
-- only way to restore a channel without silently granting Send Messages to a
-- role that never had it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS channel_locks (
    guild_id      INTEGER NOT NULL,
    channel_id    INTEGER NOT NULL,

    -- The @everyone send_messages value before the lock:
    -- 1 allow, 0 deny, NULL unset.
    previous      INTEGER,

    -- Set when the lock came from ?lockdown rather than ?lock, so ?lockdown end
    -- knows which channels it owns.
    from_lockdown INTEGER NOT NULL DEFAULT 0,

    locked_by     INTEGER NOT NULL,
    locked_at     INTEGER NOT NULL,
    reason        TEXT,

    PRIMARY KEY (guild_id, channel_id),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);
