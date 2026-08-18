# Deezee Server Bot — Architecture & Design

Phase 2 deliverable. Approve or amend this before any cog is written.

---

## 1. Stack

| Component | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | `asyncio.TaskGroup`, `tomllib`, exception groups |
| Library | **discord.py 2.x** (`>=2.4,<3.0`) | `discord.ui`, `app_commands`, native timeouts. **2.x API only — no v1.x syntax anywhere** |
| Database | SQLite via `aiosqlite` | Single file, no server process. Fits Wispbyte |
| Config | `python-dotenv` | Secrets in `.env`, never in the DB |
| HTTP | `aiohttp` | Already a discord.py dependency, no extra install |
| Images | `Pillow` | Rank cards, captcha. Local, no API |
| Protobuf | **hand-written decoder** | Safe Browsing v5 is protobuf-only; the message is 3 fields. A 40-line varint reader beats a dependency |

No web framework. No dashboard. No Redis. No ORM.

### Why no ORM

Raw SQL through `aiosqlite`. The schema is ~30 tables of flat per-guild config; an ORM adds import time and indirection for nothing. Wispbyte's free tier boots slowly enough that every dependency is a real cost.

---

## 2. File tree

```
Deezee DC Bot/
├── bot.py                      # entrypoint: load .env, build bot, run
├── requirements.txt
├── .env                        # secrets (git-ignored)
├── .env.example
├── .gitignore
├── README.md                   # setup, intents, OAuth scopes, Wispbyte deploy
│
├── core/
│   ├── __init__.py
│   ├── bot.py                  # DeezeeBot(commands.Bot) — setup_hook, ready, guild gate
│   ├── config.py               # typed .env loader; ROOT_OWNER_IDS, GUILD_IDS, DEV_GUILD_ID
│   ├── database.py             # aiosqlite connection, WAL, query helpers
│   ├── migrations.py           # applies migrations/*.sql in order, records applied version in schema_version
│   ├── guildconfig.py          # per-guild settings cache, write-through
│   ├── permissions.py          # root/owner/admin/mod checks + hierarchy guard
│   ├── errors.py               # exception types + global error handler
│   ├── scheduler.py            # timed actions, rehydrated from DB on boot
│   ├── constants.py            # colours, emoji, limits, ThreatType enum
│   └── logging_setup.py        # rotating file + stdout handlers
│
├── ui/
│   ├── __init__.py
│   ├── views.py                # BaseView (invoker lock, timeout), ConfirmView
│   ├── paginator.py            # embed paginator: first/prev/next/last/jump
│   └── panels/                 # the ?config tree — one file per category
│       ├── root.py             # category buttons, entry point
│       ├── automod.py
│       ├── antiraid.py
│       ├── roles.py            # reaction-role builder + self-role picker
│       ├── logging.py
│       ├── leveling.py
│       ├── welcome.py          # serves both welcome and goodbye
│       ├── utility.py          # help browser + embed builder
│       └── importer.py         # review/apply + emoji→role mapper
```

`modals.py` and `selects.py` were dropped: every modal and select turned out to
belong to exactly one panel, and a shared module of one-use classes is
indirection without a payer. Moderation and starboard have no panel of their own
— their settings are reached from the root panel's hint card and their own
commands.

```
│
├── cogs/                       # one per sheet category, built in this order
│   ├── moderation.py           # 32 commands
│   ├── automod.py              # 19
│   ├── antiraid.py             # 15
│   ├── roles.py                # 15
│   ├── logging.py              # 6
│   ├── leveling.py             # 12
│   ├── economy.py              # 7  (split from leveling: different domain)
│   ├── utility.py              # 27
│   ├── fun.py                  # 11
│   ├── giveaways.py            # 7
│   ├── tags.py                 # 13
│   ├── serverconfig.py         # 15
│   ├── importer.py             # 9  (?import family)
│   └── owner.py                # reload, sync, shutdown — root/owner gated
│
├── services/                   # logic with no Discord dependency; unit-testable
│   ├── safebrowsing.py         # v5 urls:search + proto decode + 3-tier allowlist
│   ├── filters.py              # automod matching: leetspeak, zero-width, regex guard
│   ├── riskscore.py            # alt-account heuristics
│   ├── captcha.py              # Pillow captcha generation
│   ├── levelcard.py            # Pillow rank card
│   ├── levelcurve.py           # the XP curve + conversions from other bots
│   ├── tagvars.py              # restricted variable engine
│   ├── timeparse.py            # "10m", "2h30m", "7d" → timedelta
│   └── importers/
│       ├── mee6.py
│       ├── arcane.py
│       ├── csvlevels.py
│       ├── modlog.py           # parses Dyno / Carl-bot / Sapphire case embeds
│       └── capture.py          # parses other bots' config output
│
├── migrations/
│   ├── 001_initial.sql
│   ├── 002_leveling.sql
│   └── ...                     # never edited once shipped; only appended
│
├── data/                       # persistent volume on Wispbyte
│   ├── deezee.db
│   ├── trusted_domains.txt
│   ├── phishing_blocklist.txt
│   └── fonts/
│
├── docs/
│   ├── DESIGN.md               # this file
│   └── Deezee_Command_Sheet.xlsx
│
├── tools/
│   └── build_command_sheet.py
│
└── logs/                       # rotating, git-ignored
```

---

## 3. Startup sequence

`setup_hook()` runs before the gateway connects:

1. Load and validate `.env`. **Fail loudly and exit** on a missing token or malformed ID — never boot half-configured.
2. Open SQLite, set `journal_mode=WAL` and `foreign_keys=ON`.
3. Run pending migrations, in order, inside a transaction each.
4. Load every cog. One cog failing logs the traceback and continues; it does not take the bot down.
5. Register persistent views (§7).
6. Sync the app-command tree to each guild in `GUILD_IDS`.

`on_ready()`:

7. Rehydrate the scheduler from the DB (§8).
8. Leave any guild not in `GUILD_IDS`, unless it is `DEV_GUILD_ID`.
9. Warm the guild-config cache.

`on_ready` can fire more than once on reconnect — every step above is idempotent and guarded by a `self._ready_once` flag.

---

## 4. Database layer

Single `aiosqlite` connection, WAL mode. SQLite handles this workload trivially at one-guild scale; a pool would add contention, not remove it.

Every table is keyed by `guild_id`, so the dev guild and the live guild share a file but never share state.

Core tables:

| Table | Holds |
|---|---|
| `schema_version` | applied migration numbers |
| `guild_config` | one row per guild, all scalar settings |
| `mod_cases` | case_id, guild, target, moderator, action, reason, expires_at |
| `warnings` | separate from cases: warns feed the automod ladder |
| `notes` | private staff notes |
| `scheduled_actions` | unmute/unban/temprole/reminder/giveaway-end |
| `automod_rules` | per-filter enable, threshold, punishment |
| `automod_exempt` | role / channel / member exemptions |
| `trusted_domains` | per-guild additions to the bundled list |
| `scan_cache` | url_hash, verdict, expires_at |
| `levels` | guild, user, xp, last_award_at |
| `level_roles` | level → role |
| `economy` | balance, inventory, cooldowns |
| `reaction_role_menus` / `_options` | menu config + emoji→role |
| `tags`, `custom_commands`, `autoresponses` | content + owner + uses |
| `giveaways`, `giveaway_entries` | prize, ends_at, requirements |
| `starboard_messages` | source→star message mapping |
| `import_draft` | staged migration data, section-keyed |
| `import_snapshots` | pre-apply config, 7-day retention |

### Migrations

`migrations/NNN_name.sql`, applied in numeric order, recorded in `schema_version`. A shipped migration is **never edited** — corrections go in a new file. This is what makes a Wispbyte redeploy safe.

---

## 5. Permissions & hierarchy

One helper module. Every mod command routes through it — no command does its own check.

### Authority tiers, highest first

| Tier | Source | Power |
|---|---|---|
| **Root owner** | `ROOT_OWNER_IDS` in `.env` | Absolute. Bypasses every check. Cannot be targeted, denied, blacklisted or demoted by anything at runtime |
| **Owner** | `OWNER_IDS` in `.env` | Full bot power, but cannot act against a root owner and cannot edit either list |
| **Admin** | `?adminroles` or Discord Administrator | Server-wide config |
| **Mod** | `?modroles` or the relevant Discord permission | Moderation commands |
| **Member** | — | Public commands only |

Neither owner list is editable from inside Discord. Changing them means editing `.env` and restarting — so no command sequence exists that strips root power.

### Hierarchy guard

Before any action against a member, in order:

1. Target is a root owner → **refuse**, always, whoever asked.
2. Target is the bot itself → refuse. Sits above the root bypass because it is
   not an authority question: Discord refuses a bot's action against itself no
   matter who asked.
3. Invoker is root → allow, skip the rest.
4. Target is the guild owner → refuse.
5. Target holds a role in `?protectedroles` → refuse.
6. Target's top role >= invoker's top role → refuse.
7. Target's top role >= **bot's** top role → refuse with "move my role higher".
8. Bot lacks the Discord permission → refuse, naming the missing permission.

Refusals against a root owner are logged to mod-log naming the invoker. Silent attempts are impossible.

---

## 6. Config UI — the no-dashboard pattern

`?config` opens the root panel. Every setting in the bot is reachable from it.

```
?config
  └─ [Moderation] [Automod] [Anti-raid] [Roles] [Logging]
     [Leveling] [Welcome] [Starboard] [Import] [Close]
        │
        └─ sub-panel: current values in an embed, plus
             • Select menus  → channels, roles, members
             • Toggle buttons → on/off, colour-coded green/red
             • Modals         → text, numbers, message templates
             • [Back] [Save] [Reset]
```

Rules for every panel:

- **Locked to the invoker.** Another user's interaction gets an ephemeral refusal.
- **180s timeout**, then buttons disable in place — no orphaned live components.
- **Live values shown**, always. The embed re-renders after each change; you never guess current state.
- **Write-through cache.** Change hits SQLite and the in-memory cache together.
- **Ephemeral by default** so config work doesn't spam the channel.

### Confirmation pattern

`ban`, `softban`, `massban`, `kick`, `purge >100`, `nuke`, `lockdown`, `massrole`, `clearwarns`, `clearnotes`, `resetxp all`, `configreset`, `configimport`, `import review apply`:

Green **Confirm** / grey **Cancel**, invoker-locked, 30s timeout (10s in the dev guild). The embed states exactly what will happen and to how many members before you press. Timeout = cancel.

---

## 7. Persistent views

Anything whose buttons must survive a restart uses a fixed `custom_id` and is registered in `setup_hook()` via `bot.add_view()`:

| View | custom_id pattern |
|---|---|
| Reaction-role menus | `rr:<menu_id>:<option_id>` |
| Giveaway entry | `gw:enter:<giveaway_id>` |
| Verification gate | `verify:start` |
| Poll votes | `poll:<poll_id>:<option>` |
| Suggestion votes | `sug:<id>:<up\|down>` |

State lives in SQLite, never in the view object. A restart re-attaches handlers; the message is untouched.

---

## 8. Scheduler

One `asyncio` task loop, one table: `scheduled_actions`.

Covers unmute, unban, temprole removal, reminders, giveaway ends, lockdown expiry, raid-mode auto-off.

- On boot, everything due-or-overdue is executed immediately, then the loop resumes. **Wispbyte restarts do not lose timed punishments** — the whole point.
- Sleeps until the next due row rather than polling on a fixed tick.
- Every execution is wrapped: a failure marks the row failed and logs it, it never kills the loop.

---

## 9. Errors, cooldowns, rate limits

**Global handler** on both `on_command_error` and `on_app_command_error`. Maps each exception to a plain-language embed:

| Exception | Message |
|---|---|
| `MissingPermissions` | which permission you lack |
| `BotMissingPermissions` | which permission **the bot** lacks |
| `MemberNotFound` | what was searched for |
| `CommandOnCooldown` | seconds remaining |
| `HierarchyError` | which role blocks it |
| `Forbidden` (50013) | "my role is too low — move it above X" |

Anything unexpected: full traceback to the log file and to the owner DM, short apology + error ID to the user. Never a raw traceback in chat.

**Cooldowns** — per user per guild. 3s on cheap commands, 10s on image generation, 30s on anything hitting an external API.

**Rate limits** — discord.py handles the 429 backoff. Bulk operations (`massban`, `massrole`, `purge`, imports) run as throttled background jobs with a live progress embed rather than a blocking loop.

**DM failures** are never fatal. Ban/kick/warn DM the target first, catch `Forbidden`, and note "could not DM" in the mod-log.

---

## 10. Wispbyte specifics

Pterodactyl-style panel: no systemd, no Docker build, no root.

- Startup installs from `requirements.txt`, so the dependency list stays short — dependencies cost boot time on every restart.
- `data/` is the persistent volume. SQLite and both domain lists live there.
- The process can be killed and restarted at any moment: **all state is in SQLite, nothing important in memory.**
- Logs rotate at 5 MB, 3 files kept. A free-tier disk should not fill with logs.
- No `screen`, no daemonising. `bot.py` runs in the foreground and the panel supervises it.

### 10.1 Resource budget — 0.5 GiB RAM, 35% CPU, 1 GiB storage

These are hard ceilings, not targets. Exceeding RAM gets the process OOM-killed by the panel;
exceeding CPU gets it throttled. Both are designed against below.

#### Memory — 512 MiB

Baseline cost is fixed and unavoidable: CPython 3.11 plus discord.py, aiohttp and Pillow imported
is roughly 110–140 MiB resident. That leaves about 370 MiB of headroom, and the only thing that
can realistically eat it is the member cache.

| Control | Setting | Effect |
|---|---|---|
| Guild chunking | `chunk_guilds_at_startup=False` | No full member download on boot. Members are fetched on demand and cached as they are seen |
| Member cache | `MemberCacheFlags(joined=True, voice=False)` | Keeps members the bot has actually interacted with; drops the voice-state cache |
| Message cache | `max_messages=500` (default 1000) | Halves the deleted/edited-message log buffer. Raw events cover anything evicted |
| SQLite page cache | `PRAGMA cache_size = -8000` | Caps SQLite at 8 MiB instead of the unbounded default |
| Snipe buffer | 5 messages per channel, in memory, never persisted | Bounded by channel count, not message volume |

**Pillow is the single biggest risk** and gets its own rules, applied in both
`services/levelcard.py` and `services/captcha.py`:

- One render at a time, behind an `asyncio.Semaphore(1)`. Ten simultaneous `?rank` calls queue;
  they do not allocate ten canvases.
- Every `Image` opened in a `with` block, and avatars closed explicitly after compositing.
- Fixed small canvases — 934×282 for rank cards, 300×100 for captcha. No user-controlled dimensions.
- Fonts loaded **once at module import**, not per render. `ImageFont.truetype` is not free.
- Downloaded avatars are size-capped (`size=128` on the asset URL) and rejected above 2 MiB before
  Pillow ever touches the bytes.

`?import modlog` streams with `async for` over `channel.history()` and flushes to SQLite every 200
rows. It never builds a 5000-message list.

#### CPU — 35%

Read as 35% of one core. The design is event-driven, so idle cost is near zero; the spikes are what
matter.

- **No polling anywhere.** The scheduler sleeps until the next due row and is woken by an
  `asyncio.Event` when something sooner is inserted. Not a fixed tick.
- **Regex is the classic footgun.** Every automod pattern is compiled once at load, length-capped,
  and rejected at config time if it contains nested quantifiers — the catastrophic-backtracking
  shape. A user-supplied regex that pins a core is a config-time error, not a runtime outage.
- **Pillow renders go through `asyncio.to_thread`** so a 200 ms composite does not stall the
  gateway heartbeat.
- **XP has a 60-second per-user cooldown**, so message volume does not translate into write volume.
- **Bulk jobs are throttled**, not tight-looped: `massban`, `massrole`, `purge` and imports sleep
  between batches and report progress via an edited embed.

#### Storage — 1 GiB

| Item | Expected | Control |
|---|---|---|
| `deezee.db` | 20–80 MiB at one-guild scale | See pruning below |
| WAL file | ≤ 16 MiB | `PRAGMA wal_autocheckpoint`, checkpoint on idle |
| `trusted_domains.txt` | ~300 KiB | Shipped, static |
| `phishing_blocklist.txt` | ~5 MiB | Refreshed, not appended |
| `data/fonts/` | ~2 MiB | Two faces |
| `logs/` | 15 MiB hard cap | 5 MiB × 3 rotation |
| Python + deps | ~150 MiB | Installed by the panel |

Growth is bounded by scheduled pruning: `scan_cache` rows dropped past expiry, `import_snapshots`
past 7 days, resolved `scheduled_actions` past 30 days, and `levels` rows for members who left.
`mod_cases`, `warnings` and `notes` are **never auto-deleted** — moderation history is the one thing
worth the disk. A `VACUUM` runs on boot if the file has grown past 200 MiB.

---

## 11. Intents & OAuth

**Privileged intents — all three already enabled on your application:**

| Intent | Needed for |
|---|---|
| `SERVER MEMBERS` | joins/leaves, autorole, alt detection, member cache |
| `MESSAGE CONTENT` | automod, autoresponses, prefix commands, `?import capture` |
| `PRESENCE` | member counts, userinfo status |

**Scopes:** `bot` + `applications.commands`

**Permission integer:** `1375895809270` — 22 explicit permissions, deliberately not Administrator.

**Role position:** the bot's role must sit **above** every role it manages — mute role, level roles, reaction-role targets.

---

## 12. Build order (Phase 3)

Per your spec: one cog at a time, shown for approval, no stubs, no `pass`, no `# TODO`.

| # | Deliverable | Sheet commands |
|---|---|---|
| 1 | `bot.py` + `core/` (config, database, migrations, logging) | — |
| 2 | `core/permissions.py` + `ui/views.py` + `ui/paginator.py` | — |
| 3 | `cogs/moderation.py` | 32 |
| 4 | `cogs/automod.py` + `services/filters.py` + `services/safebrowsing.py` | 19 |
| 5 | `cogs/antiraid.py` + `services/riskscore.py` + `services/captcha.py` | 15 |
| 6 | `cogs/roles.py` | 15 |
| 7 | `cogs/logging.py` | 6 |
| 8 | `cogs/leveling.py` + `services/levelcard.py` | 12 |
| 9 | `cogs/utility.py` | 27 |
| 10 | `cogs/serverconfig.py` + `ui/panels/` | 15 |
| 11 | `cogs/tags.py` + `services/tagvars.py` | 13 |
| 12 | `cogs/giveaways.py` | 7 |
| 13 | `cogs/economy.py` | 7 |
| 14 | `cogs/fun.py` | 11 |
| 15 | `cogs/importer.py` + `services/importers/` | 9 |
| 16 | `README.md`, `requirements.txt`, final sync | — |

**188 commands total.**

Steps 1–2 come before any cog because every command depends on them. Step 10 lands mid-build rather than last: once several cogs exist there is enough to configure, and building the panel earlier catches config-shape mistakes while they are still cheap to fix.

### Build status

All 16 steps are complete. 187 of the sheet's 188 rows are built; the one that is
not is VPN/proxy/Tor detection, recorded as NOT BUILT in §13 decision 7.

Delivered beyond the sheet: `cogs/owner.py` (7 operator commands — reload, sync,
shutdown, bot-wide blacklist, guilds, dbstats, commandcount), plus three small
prefix-only additions where a command's argument shape forced a split
(`?ecofield`, `?importsections`), and `/reptop` (reputation leaderboard, hybrid
so the competition can be checked from a slash command). **239 commands total**, across 14
cogs and 16 migrations.

---

## 13. Decisions worth flagging

1. **Leveling and Economy are separate cogs.** The sheet lists them as one category, but they share only a `user_id`. Splitting keeps both readable and lets you disable the economy without losing XP.
2. **`?config` panels live in `ui/panels/`, not in the cogs.** Otherwise `serverconfig.py` becomes a 4000-line file. The cog owns commands; the panel owns presentation.
3. **`services/` has no Discord imports.** Filter matching, risk scoring and time parsing are testable without a gateway connection — the parts most likely to harbour subtle bugs.
4. **No `eval` / `exec` command**, and the tag engine is a restricted variable substituter, not a scripting language. Carl-bot's TagScript is genuinely more powerful; it is also arbitrary execution inside your server. Say the word if you want the power instead.
5. **No startup member chunking**, to stay inside 512 MiB. The cost: a command naming a member the
   bot has not seen since boot does one API fetch (~100 ms) instead of a cache hit. Commands that
   sweep the whole member list — `?altcheck`, `?massrole`, member counts — request a chunk on
   demand and release it. If you would rather trade RAM for that latency, say so and I will flip
   `chunk_guilds_at_startup` to `True` and shrink the Pillow budget to pay for it.
6. **`?import` writes only to `import_draft`.** Nothing reaches live config without an explicit per-section apply in `?import review`. Deezee never removes another bot.

7. **VPN / proxy / Tor detection is NOT BUILT**, and cannot be. Discord never
   exposes a member's IP address to a bot. Double Counter obtains one by
   redirecting joiners to a page it hosts, which the no-dashboard constraint
   rules out. `?altcheck` implements the strongest signals actually available and
   prints the breakdown rather than a verdict. This is the 188th sheet row and
   the only one not delivered.

8. **Not every command is a slash command.** Discord caps a guild at **100
   top-level application commands**; this bot has 239. That is a platform limit,
   not a design choice, and no amount of grouping fits 239 flat commands under
   it.

   The resolution: **every command works with the prefix**, and 98 of them are
   also slash commands. The split follows one rule — slash goes to commands typed
   in a hurry or by ordinary members (`/ban`, `/rank`, `/help`, `/poll`), and
   configuration you run once is prefix-only (`?modroles`, `?welcome`,
   `?levelrole`, `?autorole`, `?starboard`, `?configexport`). Discoverability for
   the prefix-only set is served by `?config`, which reaches all of it through
   panels.

   Mechanically this is `with_app_command=False` on the demoted hybrids, each
   carrying a comment saying why. `?commandcount` reports the current split and
   the distance to the cap.

9. **Member counts are fetched, not read from the cache.** With
   `chunk_guilds_at_startup=False` the member cache holds only members the bot has
   seen, so `role.members` under-reports without saying so. `?roles`, `?roleinfo`,
   `?deleterole` and `?massrole` stream the member list over HTTP instead and
   carry a cooldown. A number that is quietly wrong is worse than one that takes
   two seconds.

10. **`?snipe` is in memory and never persisted.** Five messages per channel,
    cleared by a restart. A permanent, searchable record of every deleted message
    is a materially different and much more invasive product than a short undo.
