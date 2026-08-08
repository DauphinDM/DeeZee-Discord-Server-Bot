-- 008_logging: per-group log destinations, per-event toggles, ignore lists.
--
-- This file is shipped. Never edit it -- corrections go in a new migration.

-- ---------------------------------------------------------------------------
-- One destination channel per event group.
--
-- Separate columns rather than a table because there are exactly nine of them
-- and they are read on every logged event. A NULL column means that whole group
-- is off, which is also the default: a bot that starts logging everything the
-- moment it joins is a bot that fills a channel nobody asked it to.
-- ---------------------------------------------------------------------------
ALTER TABLE guild_config ADD COLUMN log_messages_channel_id INTEGER;
ALTER TABLE guild_config ADD COLUMN log_members_channel_id INTEGER;
ALTER TABLE guild_config ADD COLUMN log_roles_channel_id INTEGER;
ALTER TABLE guild_config ADD COLUMN log_channels_channel_id INTEGER;
ALTER TABLE guild_config ADD COLUMN log_voice_channel_id INTEGER;
ALTER TABLE guild_config ADD COLUMN log_server_channel_id INTEGER;
ALTER TABLE guild_config ADD COLUMN log_invites_channel_id INTEGER;
ALTER TABLE guild_config ADD COLUMN log_moderation_channel_id INTEGER;
ALTER TABLE guild_config ADD COLUMN log_automod_channel_id INTEGER;

-- ---------------------------------------------------------------------------
-- log_disabled_events: individual events switched off inside an enabled group.
--
-- Stored as exceptions rather than as a full on/off row per event. A group with
-- a channel set logs everything in it by default, which is what someone who
-- just picked a channel expects; turning one noisy event off writes one row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS log_disabled_events (
    guild_id INTEGER NOT NULL,
    event    TEXT    NOT NULL,

    PRIMARY KEY (guild_id, event),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- log_ignores: channels, roles and members excluded from logging.
--
-- Distinct from the command blacklist in ``blacklist``: that one stops the bot
-- responding to someone, this one stops it writing about them. A bot channel
-- usually wants the second and not the first.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS log_ignores (
    guild_id    INTEGER NOT NULL,
    entity_id   INTEGER NOT NULL,
    entity_type TEXT    NOT NULL,
    added_by    INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,

    PRIMARY KEY (guild_id, entity_id, entity_type),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);
