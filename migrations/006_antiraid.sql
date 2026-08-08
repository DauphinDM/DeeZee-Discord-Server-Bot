-- 006_antiraid: raid detection, verification gate, alt heuristics, quarantine.
--
-- This file is shipped. Never edit it -- corrections go in a new migration.

-- ---------------------------------------------------------------------------
-- Raid detection and response.
-- ---------------------------------------------------------------------------

-- 1 while raid mode is in force. Persisted so a restart does not silently end it.
ALTER TABLE guild_config ADD COLUMN raid_mode INTEGER NOT NULL DEFAULT 0;

-- Joins within the window that trip raid mode automatically. 0 disables it.
ALTER TABLE guild_config ADD COLUMN raid_join_threshold INTEGER NOT NULL DEFAULT 10;
ALTER TABLE guild_config ADD COLUMN raid_join_window INTEGER NOT NULL DEFAULT 60;

-- What happens to members who join while raid mode is on:
-- quarantine | kick | ban | none
ALTER TABLE guild_config ADD COLUMN raid_action TEXT NOT NULL DEFAULT 'quarantine';

-- Where raid alerts are posted. NULL falls back to the mod-log.
ALTER TABLE guild_config ADD COLUMN raid_alert_channel_id INTEGER;

-- Seconds of quiet before raid mode turns itself off. 0 means never.
ALTER TABLE guild_config ADD COLUMN raid_auto_off INTEGER NOT NULL DEFAULT 900;

-- ---------------------------------------------------------------------------
-- Verification gate.
-- ---------------------------------------------------------------------------

-- off | click (button in a channel) | join (gate applied on join)
ALTER TABLE guild_config ADD COLUMN verification_mode TEXT NOT NULL DEFAULT 'off';

-- Role granted on success, and the holding role applied before it.
ALTER TABLE guild_config ADD COLUMN verified_role_id INTEGER;
ALTER TABLE guild_config ADD COLUMN unverified_role_id INTEGER;

-- Channel carrying the persistent verification button.
ALTER TABLE guild_config ADD COLUMN verification_channel_id INTEGER;

-- image | text | button
ALTER TABLE guild_config ADD COLUMN captcha_type TEXT NOT NULL DEFAULT 'image';

-- Seconds a challenge stays valid.
ALTER TABLE guild_config ADD COLUMN verification_timeout INTEGER NOT NULL DEFAULT 600;

-- Attempts allowed before the challenge is refused.
ALTER TABLE guild_config ADD COLUMN verification_attempts INTEGER NOT NULL DEFAULT 3;

-- ---------------------------------------------------------------------------
-- Account age gate and alt heuristics.
-- ---------------------------------------------------------------------------

-- Minimum account age in seconds. 0 disables the check.
ALTER TABLE guild_config ADD COLUMN min_account_age INTEGER NOT NULL DEFAULT 0;

-- kick | ban | quarantine
ALTER TABLE guild_config ADD COLUMN min_age_action TEXT NOT NULL DEFAULT 'kick';

-- Role applied by ?quarantine and by automatic responses.
ALTER TABLE guild_config ADD COLUMN quarantine_role_id INTEGER;

-- Risk score (0-100) at which a join is flagged for review, and at which the
-- automatic action fires. Flagging is passive; the action is not.
ALTER TABLE guild_config ADD COLUMN alt_flag_threshold INTEGER NOT NULL DEFAULT 55;
ALTER TABLE guild_config ADD COLUMN alt_action_threshold INTEGER NOT NULL DEFAULT 80;

-- quarantine | kick | ban | none
ALTER TABLE guild_config ADD COLUMN alt_action TEXT NOT NULL DEFAULT 'none';

-- ---------------------------------------------------------------------------
-- join_log: every join, with the signals that were available at the time.
--
-- Recorded even when nothing acts on it, because ?joins and ?altcheck are
-- retrospective: the interesting question is usually asked after the raid.
-- Pruned by age, since this is the fastest-growing table in the schema.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS join_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    user_tag        TEXT    NOT NULL,

    joined_at       INTEGER NOT NULL,
    account_created INTEGER NOT NULL,

    -- Invite the member arrived on, if it could be determined.
    invite_code     TEXT,
    inviter_id      INTEGER,

    risk_score      INTEGER NOT NULL DEFAULT 0,

    -- JSON list of the reasons behind the score, for ?altcheck to show.
    risk_reasons    TEXT    NOT NULL DEFAULT '[]',

    flagged         INTEGER NOT NULL DEFAULT 0,

    -- Set when the member later leaves, so ?joins can show who did not stay.
    left_at         INTEGER,

    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_join_log_recent ON join_log (guild_id, joined_at DESC);
CREATE INDEX IF NOT EXISTS idx_join_log_user ON join_log (guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_join_log_flagged
    ON join_log (guild_id, flagged) WHERE flagged = 1;

-- ---------------------------------------------------------------------------
-- quarantine: who is held, and what roles to give back.
--
-- The role list is the whole point. Stripping roles is easy; restoring exactly
-- what someone had is only possible if it was written down first.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quarantine (
    guild_id       INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,

    -- JSON list of role IDs held before quarantine.
    roles          TEXT    NOT NULL DEFAULT '[]',

    quarantined_by INTEGER NOT NULL,
    reason         TEXT,
    created_at     INTEGER NOT NULL,

    PRIMARY KEY (guild_id, user_id),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- verification: outstanding challenges.
--
-- The answer lives here rather than on the view object, so a restart does not
-- invalidate every challenge in flight.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS verification (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,

    answer      TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    issued_at   INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,

    -- Set once the member passes. Kept as a record rather than deleted.
    verified_at INTEGER,

    PRIMARY KEY (guild_id, user_id),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- invite_uses: use counts snapshotted so a join can be attributed.
--
-- Discord does not tell a bot which invite was used. The only way to know is to
-- hold the previous counts and find the one that went up.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS invite_uses (
    guild_id   INTEGER NOT NULL,
    code       TEXT    NOT NULL,
    uses       INTEGER NOT NULL DEFAULT 0,
    inviter_id INTEGER,

    PRIMARY KEY (guild_id, code),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);
