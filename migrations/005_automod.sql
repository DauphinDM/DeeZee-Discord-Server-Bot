-- 005_automod: filters, exemptions, link scanning.
--
-- This file is shipped. Never edit it -- corrections go in a new migration.

-- ---------------------------------------------------------------------------
-- guild_config additions: the escalation ladder.
--
-- Filters punish individually; the ladder is what turns repeated trips into a
-- bigger consequence. Warnings from any source count towards it.
-- ---------------------------------------------------------------------------

-- Warnings inside the window that trigger the escalation action. 0 disables it.
ALTER TABLE guild_config ADD COLUMN automod_warn_threshold INTEGER NOT NULL DEFAULT 0;

-- Window in seconds that warnings are counted over. Default 7 days.
ALTER TABLE guild_config ADD COLUMN automod_warn_window INTEGER NOT NULL DEFAULT 604800;

-- What happens at the threshold: mute, kick or ban.
ALTER TABLE guild_config ADD COLUMN automod_escalation_action TEXT NOT NULL DEFAULT 'mute';

-- Duration in seconds for a mute or temporary ban escalation.
ALTER TABLE guild_config ADD COLUMN automod_escalation_duration INTEGER NOT NULL DEFAULT 3600;

-- DM the member explaining which filter they tripped.
ALTER TABLE guild_config ADD COLUMN automod_notify INTEGER NOT NULL DEFAULT 1;

-- ---------------------------------------------------------------------------
-- automod_filters: one row per filter per guild.
--
-- param1 and param2 are deliberately generic; their meaning is per filter and
-- documented in cogs/automod.py FILTERS. A column per filter would mean a
-- migration every time a filter gains a knob.
--
--   words        param1 unused          mode: wildcard | whole
--   invites      param1 unused          mode: strict | allowlist
--   links        param1 unused          mode: all | allowlist | blocklist
--   caps         param1 percent 1-100   param2 minimum message length
--   spam         param1 message count   param2 window seconds
--   mentions     param1 max mentions
--   emoji        param1 max emoji
--   attachments  param1 max count       param2 window seconds
--   zalgo        param1 percent 1-100 of combining characters
--   duplicates   param1 max repeats     param2 window seconds
--   newlines     param1 max newlines    param2 max characters
--   stickers     param1 max count       param2 window seconds
--   scanlinks    param1 unused
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS automod_filters (
    guild_id  INTEGER NOT NULL,
    name      TEXT    NOT NULL,
    enabled   INTEGER NOT NULL DEFAULT 0,

    -- delete | warn | mute | kick | ban
    action    TEXT    NOT NULL DEFAULT 'delete',

    -- Seconds, for mute and temporary ban actions.
    duration  INTEGER NOT NULL DEFAULT 3600,

    param1    INTEGER NOT NULL DEFAULT 0,
    param2    INTEGER NOT NULL DEFAULT 0,
    mode      TEXT    NOT NULL DEFAULT '',

    PRIMARY KEY (guild_id, name),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- filter_values: the list-shaped parts of a filter's configuration.
--
-- Banned words, allowed invite guild IDs, allowed and blocked domains, blocked
-- file extensions. One table rather than five, because every one of them is a
-- set of strings scoped to a guild and a filter.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS filter_values (
    guild_id   INTEGER NOT NULL,

    -- Which filter this belongs to: words, invites, links, attachments.
    name       TEXT    NOT NULL,

    -- word | allow | deny | extension
    kind       TEXT    NOT NULL,

    value      TEXT    NOT NULL,
    added_by   INTEGER NOT NULL,
    created_at INTEGER NOT NULL,

    PRIMARY KEY (guild_id, name, kind, value),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- automod_exempt: who automod ignores.
--
-- filter_name '*' exempts from every filter. Anything else exempts from that
-- one filter, so a bot channel can allow links without allowing slurs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS automod_exempt (
    guild_id    INTEGER NOT NULL,
    entity_id   INTEGER NOT NULL,

    -- role | channel | member
    entity_type TEXT    NOT NULL,

    filter_name TEXT    NOT NULL DEFAULT '*',

    added_by    INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,

    PRIMARY KEY (guild_id, entity_id, entity_type, filter_name),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- media_channels: channels that require an attachment or a link.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS media_channels (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    added_by   INTEGER NOT NULL,
    created_at INTEGER NOT NULL,

    PRIMARY KEY (guild_id, channel_id),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- trusted_domains: per-guild additions to the bundled trusted list.
--
-- Tier 2 of link scanning. Tier 1 is data/trusted_domains.txt, shipped with the
-- bot; tier 3 is the Safe Browsing API, which only ever sees domains that
-- matched neither.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trusted_domains (
    guild_id   INTEGER NOT NULL,
    domain     TEXT    NOT NULL,
    added_by   INTEGER NOT NULL,
    created_at INTEGER NOT NULL,

    PRIMARY KEY (guild_id, domain),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- scan_cache: Safe Browsing verdicts, keyed by a hash of the URL.
--
-- Global rather than per-guild: a malicious URL is malicious everywhere, and
-- the API's quota is per key, not per server. Rows expire on the cacheDuration
-- the API returns and are pruned by the scheduler.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scan_cache (
    url_hash   TEXT PRIMARY KEY,

    -- clean | malware | social_engineering | unwanted_software | ...
    verdict    TEXT    NOT NULL,

    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scan_cache_expiry ON scan_cache (expires_at);
