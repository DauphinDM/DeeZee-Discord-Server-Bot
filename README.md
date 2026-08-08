# Deezee Server Bot

One bot replacing **Dyno, Carl-bot, Lawliet, Sapphire and Double Counter** for a
single Discord server.

**Every setting is configured inside Discord.** There is no web dashboard, and
there is not going to be one. `?config` opens a panel with a button per category;
each one opens select menus, toggles and modals. Assume the moderator never opens
a browser.

---

## What is in here

| | |
|---|---|
| Commands | **239** — 188 from the command sheet, plus subcommands, operator tools and three extras |
| Slash commands | **98** of Discord's 100-per-guild cap (see below) |
| Cogs | 14 |
| Migrations | 16 |
| Dependencies | 4 |
| Resource budget | 0.5 GiB RAM · 35% of one core · 1 GiB storage |

Everything else is in [`docs/DESIGN.md`](docs/DESIGN.md): the architecture, the
permission model, the resource budget and the decisions worth arguing about.

---

## Setup

### 1. Python

Python **3.11 or newer**.

```bash
pip install -r requirements.txt
```

Four dependencies, deliberately: `discord.py`, `aiosqlite`, `python-dotenv`,
`Pillow`. Wispbyte reinstalls them on every restart, so each one costs boot time.

### 2. `.env`

Copy `.env.example` to `.env` and fill it in. `.env` is git-ignored and must
never be committed — it is the only place a secret lives, and nothing in the
database or in an export ever contains one.

```
DISCORD_TOKEN=...
GUILD_IDS=1464027421236920392
DEV_GUILD_ID=1464027421236920392
ROOT_OWNER_IDS=1218241705573351464
OWNER_IDS=1505333010646564924
DEFAULT_PREFIX=?
LOG_LEVEL=INFO
```

**`GUILD_IDS` is an allowlist.** The bot leaves any guild not on it, immediately,
on join. Being added elsewhere means an invite leaked.

**Owner IDs cannot be changed from inside Discord.** Editing `.env` and
restarting is the only way, so no sequence of commands exists that strips root
power. A root owner also cannot be targeted, denied or blacklisted by anything at
runtime — attempts are refused, recorded in `permission_audit`, and announced in
the mod-log naming whoever tried.

### 3. Privileged intents

All three are required. Enable them at
**Discord Developer Portal → your application → Bot → Privileged Gateway
Intents**:

| Intent | Needed for |
|---|---|
| **Server Members** | joins/leaves, autorole, alt detection, the member cache |
| **Message Content** | automod, autoresponses, prefix commands, `?import capture` |
| **Presence** | member counts, `?userinfo` status |

Without them the bot refuses to boot and says which one is missing.

### 4. Invite

Scopes: `bot` + `applications.commands`
Permission integer: **`1375895809270`** — 22 explicit permissions, deliberately
not Administrator.

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_APP_ID&permissions=1375895809270&scope=bot%20applications.commands
```

**Move the bot's role above every role it manages** — the mute role, level roles,
reaction-role targets. Discord enforces this server-side; nothing the bot can do
works around it. `?diagnose` tells you when this is the problem.

### 5. Run

```bash
python bot.py
```

Migrations apply automatically, in order, each in its own transaction. A shipped
migration is never edited — corrections go in a new file. That is what makes a
redeploy safe.

---

## Wispbyte (or any Pterodactyl-style panel)

No systemd, no Docker build, no root.

- **Startup command:** `python bot.py`
- **`data/` is the persistent volume.** SQLite, both domain lists and the fonts
  live there. Nothing important is in memory.
- **The process can be killed at any moment.** Every timed punishment, reminder
  and giveaway is a row in `scheduled_actions`, rehydrated on boot. A restart
  does not lose one.
- **Logs rotate at 5 MB, 3 files kept** — 15 MiB hard cap, so a free-tier disk
  cannot fill with logs.
- `bot.py` runs in the foreground and the panel supervises it. No `screen`, no
  daemonising.

### Going live

The live server is **commented out of `GUILD_IDS` on purpose** while the bot is
being tested. To go live, edit `.env`:

```
# GUILD_IDS=1510749518457471116,1464027421236920392   <- uncomment this
GUILD_IDS=1464027421236920392                          <- delete this
```

then restart. Run `?sync` afterwards if the slash commands do not appear.

---

## Why some commands are prefix-only

**Discord caps a guild at 100 top-level application commands.** This bot has 239.
That is not a limitation of the bot; it is a hard limit of the platform, and no
amount of grouping gets 239 flat commands under it.

So 98 commands are available both ways, and the rest are **prefix-only**. Every
command works with the prefix. The split is not arbitrary:

- **Slash** goes to commands typed in a hurry or by ordinary members — `/ban`,
  `/warn`, `/rank`, `/help`, `/poll`, `/remind`, `/userinfo`.
- **Prefix-only** goes to configuration you run once and never again —
  `?modroles`, `?welcome`, `?levelrole`, `?autorole`, `?starboard`,
  `?configexport`. Discoverability for those is served by `?config`, which
  reaches all of them through panels.

`?commandcount` shows the current split and how close the tree is to the cap.

Mentioning the bot always works as a prefix, so a bad `?prefix` can never lock
you out.

---

## The command sheet

`docs/Deezee_Command_Sheet.xlsx` lists all 188 commands with their source bot,
arguments, permissions and notes. **187 are built.** One is not, and it is worth
being specific about which:

> **VPN / proxy / Tor detection — NOT BUILT.**
> Discord never gives a bot a member's IP address. Double Counter obtains one by
> redirecting joiners to a web page it hosts, and the no-dashboard constraint
> rules that out. `?altcheck` implements the strongest signals a bot can actually
> see — account age, avatar, username similarity, join burst, invite
> attribution, Discord's own flags — and prints the full breakdown rather than
> pretending to a verdict it cannot reach.

---

## Layout

```
bot.py              entrypoint
core/               bot class, config, database, migrations, permissions,
                    scheduler, errors, mod-log
cogs/               one per category — 14 files
services/           logic with no Discord imports, unit-testable:
                    filters, safebrowsing, riskscore, captcha, levelcard,
                    levelcurve, tagvars, timeparse, importers/
ui/                 views, paginator, panels/ (the ?config tree)
migrations/         NNN_name.sql, applied in order, never edited once shipped
data/               persistent volume: deezee.db, domain lists, fonts
docs/               DESIGN.md and the command sheet
logs/               rotating, git-ignored
```

---

## Resource notes

The three settings that keep this inside 512 MiB are in `core/bot.py` and are
load-bearing, not stylistic:

```python
chunk_guilds_at_startup=False          # no member download on boot
MemberCacheFlags(joined=True, voice=False)
max_messages=500
```

The cost is one API fetch (~100 ms) for a member the bot has not seen since boot.
Commands that genuinely need the whole member list — `?roles`, `?massrole`,
`?deleterole` — stream it over HTTP instead of caching it, and say so in their
output rather than quietly reporting a partial count.

**Pillow** is the biggest single risk and gets its own rules in
`services/captcha.py` and `services/levelcard.py`: one render at a time behind an
`asyncio.Semaphore(1)`, fixed canvas sizes, fonts loaded once at import, and
every render on `asyncio.to_thread` so a 200 ms composite cannot stall the
gateway heartbeat.

Measured on the dev guild: **68 MiB resident with Pillow loaded**; 20 concurrent
captcha renders grew RSS by 2 MiB.

---

## Migrating off the old bots

`?import` stages everything into a draft table. **Nothing reaches live
configuration without an explicit per-section button press in `?import review`**,
and every apply snapshots what it replaced so `?import undo` can put it back for
seven days.

Deezee never touches the bots it replaces. It does not remove them, does not edit
their data, and does not ask them for anything they have not already posted in
public. Removing them stays a manual job — which is the right way round, because
it means an import going wrong costs nothing.

`?import status` gives a per-bot "safe to remove" verdict once its data has been
imported and applied.

---

## Things this bot deliberately does not do

- **No `eval` or `exec` command.** A bot that runs arbitrary Python from a chat
  message is one leaked account away from being a shell on the host.
- **No full scripting language in tags.** `services/tagvars.py` is a substituter
  over a fixed table of names plus two random pickers — no loops, no
  conditionals, no nesting. Carl-bot's TagScript is more powerful; it is also
  arbitrary execution inside your server, run by whoever can create a tag.
- **No CAPTCHA bypassing, and no defeating Cloudflare.** `?import levels arcane`
  reports the challenge page and points at the CSV path instead.
- **No permanent record of deleted messages.** `?snipe` is a five-message
  in-memory ring buffer per channel, cleared by a restart and never written to
  disk.
