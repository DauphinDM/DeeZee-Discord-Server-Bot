-- 007_roles: reaction-role menus, autoroles, sticky roles, self-assignable roles.
--
-- This file is shipped. Never edit it -- corrections go in a new migration.

-- ---------------------------------------------------------------------------
-- Sticky roles: give a member back what they had when they rejoin.
--
-- Off by default. It is a genuine moderation hole when it is on and nobody
-- remembers it: a muted member who leaves and rejoins gets the mute back, which
-- is the point, but so does anyone whose roles were removed as a punishment.
-- ---------------------------------------------------------------------------
ALTER TABLE guild_config ADD COLUMN sticky_roles_enabled INTEGER NOT NULL DEFAULT 0;

-- ---------------------------------------------------------------------------
-- reaction_role_menus: one row per self-assign menu.
--
-- ``style`` decides how members interact with it:
--   reaction -- emoji reactions on the message (what Carl-bot does)
--   button   -- one button per role, persistent across restarts
--   select   -- a single dropdown, best above five roles
--
-- ``mode`` decides what happens when they do:
--   normal -- add and remove freely
--   unique -- one role from this menu at a time; picking a second drops the first
--   verify -- add only, never remove
--   drop   -- remove only, never add
--   limit  -- at most ``max_roles`` from this menu
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reaction_role_menus (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,

    -- NULL until the menu has been posted. A menu can be built before it is
    -- published, so the builder never has to post a half-finished message.
    message_id  INTEGER,

    title       TEXT    NOT NULL DEFAULT 'Self-assignable roles',
    description TEXT    NOT NULL DEFAULT '',
    colour      INTEGER,

    style       TEXT    NOT NULL DEFAULT 'button',
    mode        TEXT    NOT NULL DEFAULT 'normal',
    max_roles   INTEGER NOT NULL DEFAULT 0,

    created_by  INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,

    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rr_menus_guild ON reaction_role_menus (guild_id);
CREATE INDEX IF NOT EXISTS idx_rr_menus_message ON reaction_role_menus (message_id);

-- ---------------------------------------------------------------------------
-- reaction_role_options: emoji/label to role, one row each.
--
-- ``emoji`` is stored as the raw string Discord gives back: a unicode character
-- for standard emoji, ``<:name:id>`` for custom ones. Comparing raw strings is
-- what makes reaction matching work without resolving the emoji every time.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reaction_role_options (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    menu_id     INTEGER NOT NULL,
    role_id     INTEGER NOT NULL,

    emoji       TEXT,
    label       TEXT    NOT NULL DEFAULT '',
    description TEXT    NOT NULL DEFAULT '',
    position    INTEGER NOT NULL DEFAULT 0,

    UNIQUE (menu_id, role_id),
    FOREIGN KEY (menu_id) REFERENCES reaction_role_menus (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rr_options_menu ON reaction_role_options (menu_id, position);

-- ---------------------------------------------------------------------------
-- autoroles: granted on join.
--
-- ``for_bots`` separates the two populations: a server usually wants humans in
-- @Member and bots in @Bots, and granting both to both is the common mistake.
--
-- ``delay_seconds`` defers the grant. A join-and-spam raid relies on getting
-- posting rights instantly; a sixty-second delay costs a real member nothing.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS autoroles (
    guild_id      INTEGER NOT NULL,
    role_id       INTEGER NOT NULL,
    for_bots      INTEGER NOT NULL DEFAULT 0,
    delay_seconds INTEGER NOT NULL DEFAULT 0,
    added_by      INTEGER NOT NULL,
    created_at    INTEGER NOT NULL,

    PRIMARY KEY (guild_id, role_id),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- sticky_roles: what each member held when they left.
--
-- Written on leave, read on join, deleted after a successful restore. Only
-- members who have actually left occupy rows, so this does not grow with the
-- member count.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sticky_roles (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,

    -- JSON list of role IDs.
    roles      TEXT    NOT NULL DEFAULT '[]',
    updated_at INTEGER NOT NULL,

    PRIMARY KEY (guild_id, user_id),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- sticky_role_blacklist: roles sticky roles must never restore.
--
-- The reason the feature is safe to enable at all. Put the mute role in here
-- and a punished member cannot wash it off by leaving -- put a staff role in
-- here and a demoted moderator cannot get it back the same way.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sticky_role_blacklist (
    guild_id INTEGER NOT NULL,
    role_id  INTEGER NOT NULL,

    PRIMARY KEY (guild_id, role_id),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- self_roles: roles any member may give themselves with ?selfrole.
--
-- Deliberately separate from reaction-role menus. A self-role is a permission
-- statement ("members may hold this"); a menu is a presentation of some of
-- them. The same role can appear in both, or in neither.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS self_roles (
    guild_id    INTEGER NOT NULL,
    role_id     INTEGER NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    added_by    INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,

    PRIMARY KEY (guild_id, role_id),
    FOREIGN KEY (guild_id) REFERENCES guild_config (guild_id) ON DELETE CASCADE
);
