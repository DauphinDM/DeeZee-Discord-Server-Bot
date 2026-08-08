-- 014_economy: currency, shop and inventory.
--
-- This file is shipped. Never edit it -- corrections go in a new migration.

-- ---------------------------------------------------------------------------
-- Off by default, and separable from leveling on purpose: a server can want XP
-- without wanting a currency.
-- ---------------------------------------------------------------------------
ALTER TABLE guild_config ADD COLUMN economy_enabled INTEGER NOT NULL DEFAULT 0;

ALTER TABLE guild_config ADD COLUMN currency_name TEXT NOT NULL DEFAULT 'coins';
ALTER TABLE guild_config ADD COLUMN currency_symbol TEXT NOT NULL DEFAULT '🪙';

-- ?daily: base amount, plus this much per consecutive day, capped.
ALTER TABLE guild_config ADD COLUMN daily_amount INTEGER NOT NULL DEFAULT 100;
ALTER TABLE guild_config ADD COLUMN daily_streak_bonus INTEGER NOT NULL DEFAULT 10;
ALTER TABLE guild_config ADD COLUMN daily_streak_cap INTEGER NOT NULL DEFAULT 30;

-- ?work: a random amount in this range, once per cooldown.
ALTER TABLE guild_config ADD COLUMN work_min INTEGER NOT NULL DEFAULT 20;
ALTER TABLE guild_config ADD COLUMN work_max INTEGER NOT NULL DEFAULT 80;
ALTER TABLE guild_config ADD COLUMN work_cooldown INTEGER NOT NULL DEFAULT 3600;

-- ?gamble. Off by default: a currency game is a decision, not a default.
ALTER TABLE guild_config ADD COLUMN gamble_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE guild_config ADD COLUMN gamble_max_bet INTEGER NOT NULL DEFAULT 500;

-- ?pay above this amount asks for confirmation first.
ALTER TABLE guild_config ADD COLUMN pay_confirm_threshold INTEGER NOT NULL DEFAULT 1000;

-- ---------------------------------------------------------------------------
-- economy: one row per member who has ever held currency.
--
-- Cooldowns are epoch seconds in the database rather than timers in memory, so
-- a restart is not a way to claim ?daily twice.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS economy (
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,

    balance      INTEGER NOT NULL DEFAULT 0,

    -- Consecutive days claimed, and when the last claim was.
    streak       INTEGER NOT NULL DEFAULT 0,
    last_daily   INTEGER NOT NULL DEFAULT 0,
    last_work    INTEGER NOT NULL DEFAULT 0,

    -- Lifetime figures, so ?ecoadmin can see where currency came from.
    total_earned INTEGER NOT NULL DEFAULT 0,
    total_spent  INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (guild_id, user_id),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_economy_rich ON economy (guild_id, balance DESC);

-- ---------------------------------------------------------------------------
-- shop_items: what the currency buys.
--
-- ``role_id`` makes an item a role purchase; without it the item is an object
-- that lands in an inventory and means whatever the server decides it means.
-- ``stock`` of -1 is unlimited, which is the common case.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shop_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,

    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    price       INTEGER NOT NULL,

    role_id     INTEGER,
    stock       INTEGER NOT NULL DEFAULT -1,

    enabled     INTEGER NOT NULL DEFAULT 1,
    sold        INTEGER NOT NULL DEFAULT 0,

    created_by  INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,

    UNIQUE (guild_id, name),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- inventory: what each member owns.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inventory (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    item_id     INTEGER NOT NULL,

    quantity    INTEGER NOT NULL DEFAULT 1,
    acquired_at INTEGER NOT NULL,

    PRIMARY KEY (guild_id, user_id, item_id),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE,
    FOREIGN KEY (item_id) REFERENCES shop_items (id) ON DELETE CASCADE
);
