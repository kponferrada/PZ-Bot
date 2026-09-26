# PZ Tambayan Discord Bot

A remote Discord bot for the **PZ Tambayan** Project Zomboid (Build 42) server,
refactored from [JeevesBot](https://github.com/StewBagger/Jeeves) (MIT © StewBagger).

The key difference from upstream Jeeves: **this bot does not run on the game
server.** It runs on a separate VPS and talks to the server over two channels:

- **RCON** — server commands (`players`, `servermsg`, `save`, `quit`, `teleport`, …)
- **SFTP** — the mod file bridge (`Zomboid/Lua/`), log tailing (`Zomboid/Logs/`),
  and the player DB (`db/pzserver.db`)

The server mods cannot tell — and do not care — whether the bytes in `Zomboid/Lua/`
arrived from a local process or over SFTP, so every file-bridge feature works
unchanged from a remote VPS.

---

## Features

| Feature | Module |
|---|---|
| Player tracking — join/leave, welcome / welcome-back, split-screen/co-op support | `player_tracker.py` |
| Death notifications — rendered injury cards + per-character death counts | `death_log.py`, `death_card.py`, `death_store.py` |
| Server status dashboard — auto-updating (embed or rendered card) | `server_status.py`, `status_card.py` |
| In-game ↔ Discord chat relay | `chat_relay.py` |
| Rank sync — Discord roles → in-game name colours | `rank_sync.py` |
| Siege Night — start / per-wave / ended notifications + control | `siege_night.py` |
| Airdrop / supply-drop notifications (server-wide only) | `jeeves_drops.py` |
| Workshop mod-update checker (chunked Steam API, retries) | `mod_checker.py` |
| Bot-driven scheduled + mod-update restarts | `restart_watch.py` |
| Whitelist system — application, approval, manual admin | `whitelist.py` |
| Aegis Panel stats + public leaderboards | `stats.py` |
| Runtime feature toggles (11 switchable functions) | `features.py`, `feature_controls.py` |
| Server admin commands | `main.py` |
| Automatic cleanup of bot-generated files | `cleanup.py` |

---

## Requirements

- A Project Zomboid **dedicated server (Build 42)** with **RCON** + **SFTP** access.
- Server mods — each enables the corresponding feature:
  - **Jeeve's Integration** — dashboard, chat relay, rank sync, sound alerts.
  - **Siege Night** (Workshop `3669589584`) + the `siege-night-bridge` companion.
  - **Jeeve's Drops** — airdrop / supply-drop events.
  - **Death Log** (Workshop `2972685375`) — death notifications + injury cards.
  - **Aegis Panel** (Workshop `3766508989`) — player stats / leaderboards.
- A separate Linux VPS for the bot, with Python 3.10+.
- SFTP access to the game server — `Zomboid/Lua/` **read+write** for the bridge
  features; `Zomboid/Logs/` read for tracking; `db/pzserver.db` read for the roster.

> **Restarts.** The bot stops the server gracefully over RCON (save → kick →
> quit) so the host applies updates and brings it back up. It does **not** start
> a stopped server — that remains a host-panel action. The old **PhunServer 2**
> restart scheduler is retired; restarts are now fully bot-driven.

---

## Install

```bash
git clone <your-repo> pz-tambayan-bot && cd pz-tambayan-bot/bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.env.example config.env    # then edit it
.venv/bin/python main.py
```

`run.sh` (Linux) and `run_bot.bat` (Windows) do the venv setup automatically on
first run. See [`../SETUP.md`](../SETUP.md) for the full end-to-end guide (Discord
app, game-server prerequisites, VPS deploy, systemd).

---

## Configuration (`config.env`)

```dotenv
# Discord
DISCORD_TOKEN=...
DISCORD_GUILD_ID=...
DISCORD_CHANNEL_ID=...        # default notification channel

# RCON
RCON_HOST=your.server.host
RCON_PORT=27015
RCON_PASSWORD=...

# SFTP (the game server's file access)
SFTP_HOST=your.server.host
SFTP_PORT=22
SFTP_USER=...
SFTP_PASSWORD=...             # or SFTP_KEY_PATH=/path/to/key

# Remote Zomboid data root — Lua/ and Logs/ are derived from this.
SFTP_ZOMBOID_ROOT=/server-data
# Optional overrides:
# SFTP_LUA_DIR=/server-data/Lua
# SFTP_LOGS_DIR=/server-data/Logs
# SFTP_SERVER_INI=/server-data/Server/pzserver.ini   (for /modlist)
# SFTP_SERVER_DB=/server-data/db/pzserver.db         (for the roster)
# SFTP_MODS_DIR=/server-files/steamapps/workshop/content/108600
# AEGIS_STATS_PATH=/server-data/Lua/Aegis/Player/stats.txt
```

See `config.env.example` for the **full commented reference** (roles, ranks, emoji,
every optional channel, mod-update/restart timings, scheduled restarts, cleanup,
feature-state path).

---

## Commands

Admin commands require the role named by `DEFAULT_ROLE` (default `Admin`).

### Everyone

| Command | Description |
|---|---|
| `/myrank` | Show your in-game rank and chat colour |
| `/linkme`, `/unlinkme` | Link/unlink your Discord to your PZ username |
| `/stats <username>` | Show a player's Aegis Panel stats |
| `/leaderboard <kind>` | Top players by a stat |
| `/features` | List feature toggles and their on/off state |
| `/whitelist` | Apply for the server whitelist (button) |

### Admin (`DEFAULT_ROLE`)

| Command | Description |
|---|---|
| `/hello` | Health check |
| `/online` | Is the game server online? |
| `/players` | Currently connected players |
| `/playerlist` | Everyone who has ever joined (+ deaths) |
| `/modlist` | Show configured mods / Workshop items |
| `/teleport` | Teleport player1 to player2 |
| `/setaccesslevel` | Set a player's access level |
| `/msg` | Broadcast a message to the server |
| `/announce` | Post a server announcement banner |
| `/playsound` | Trigger a sound on all connected players |
| `/stop` | Graceful shutdown (RCON save + quit) |
| `/restart` | Force a restart now (announce + countdown) |
| `/restartnow` | Immediate restart (announce + save now, kick in 30s) |
| `/deferrestart` | Defer/cancel an upcoming/scheduled restart (auto-resumes after N minutes) |
| `/forcemodupdate` | Force a mod-update restart now |
| `/setrank`, `/syncranks` | Set a player's rank / rebuild the rank file |
| `/linkname`, `/unlinkname`, `/listlinks` | Manage Discord↔PZ name links |
| `/siegestatus`, `/siegestart`, `/siegestop`, `/siegeschedule` | Siege Night control |
| `/airdrop`, `/supplyevent` | Trigger a drop / supply event |
| `/airdropstatus`, `/supplyeventstatus` | View drop / supply status |
| `/whitelistsetup`, `/whitelistadd`, `/whitelistmodify`, `/whitelistlist`, `/whitelistremove` | Whitelist admin |
| `/enable`, `/disable` | Toggle a feature on/off at runtime |
| `/help` | Full command reference |

---

## Feature toggles

Every notification function can be switched on/off at runtime with
`/enable <feature>` / `/disable <feature>`, listed by `/features`. State persists
across restarts in `feature_state.json` (override with `FEATURE_STATE_PATH`).

| Key | Function |
|---|---|
| `join_leave` | Join & leave notifications |
| `deaths` | Death notifications |
| `siege_night` | Siege night notifications |
| `airdrops` | Airdrop & supply notifications |
| `restarts` | Restart notifications |
| `scheduled_restarts` | Scheduled restarts |
| `mod_updates` | Workshop mod update checker |
| `server_up_down` | Server up/down notifications |
| `chat_relay` | In-game ↔ Discord chat relay |
| `status_dashboard` | Status dashboard (auto-updating panel) |
| `whitelist` | Whitelist application notifications |

---

## Customizing for PZ Tambayan

- **Dashboard branding** — `server_status.py` / `config.env` `DASHBOARD_TITLE`,
  `DASHBOARD_ICON_IMAGE`, `DASHBOARD_BANNER_IMAGE` (or `_URL`), `MAX_PLAYERS`.
- **Join / leave / death wording** — `player_tracker.py` (custom "signal"-themed
  strings) and `death_log.py`.
- **Rank names & colours** — `config.env` `RANK_1..RANK_6` + `rank_sync.py`
  `ROLE_TO_RANK` + `chat_relay.py` `ANSI_COLORS`.
- **Chat channels to relay** — `chat_relay.py` `RELAY_CHAT_TYPES`.
- **Emoji** — `config.env` `EMOJI_*` overrides.

---

## Project layout

```
bot/
├── main.py               # bot core: config, RCON, state, admin commands
├── sftp_client.py        # reconnecting async SFTP client
├── lua_bridge.py         # SFTP-backed file bridge to the server mods
├── server_config.py      # read server .ini (dynamic server name / MaxPlayers)
├── player_tracker.py     # join/leave/welcome (SFTP log tail + SQLite)
├── player_banner.py      # rendered join/leave signal banners
├── death_log.py          # death notifications (Death Log mod bridge)
├── death_card.py         # rendered death-card image (paper-doll injuries)
├── death_store.py        # per-character death counts (daily/weekly)
├── server_status.py      # auto-updating dashboard
├── status_card.py        # rendered status-card image
├── game_calendar.py      # world-day → calendar date (live in-game date)
├── chat_relay.py         # in-game <-> Discord chat
├── rank_sync.py          # Discord roles -> in-game colours
├── siege_night.py        # Siege Night control + notifications
├── jeeves_drops.py       # airdrop/supply events
├── jeeves_modmanager.py  # /modlist (SFTP ini read)
├── mod_checker.py        # Workshop update checker (chunked Steam API)
├── restart_watch.py      # scheduled + mod-update + forced restarts
├── whitelist.py          # whitelist application + admin
├── stats.py              # /stats + /leaderboard (Aegis Panel)
├── features.py           # feature-flag store
├── feature_controls.py   # /features, /enable, /disable
├── aegis_stats.py        # Aegis Panel stats parsing
├── cleanup.py            # auto-cleanup of bot-generated files
├── help.py               # /help
├── config.env.example
├── requirements.txt
├── run.sh / run_bot.bat
└── LICENSE               # MIT (upstream Jeeves)
```

---

## Security notes

- `config.env` holds the Discord token, RCON password and SFTP credentials — never
  commit it (it is gitignored).
- SFTP host-key checking is disabled (`known_hosts=None`) in `sftp_client.py`. For
  production, pin the server's host key instead.
- The SFTP account must have **write** access to `Zomboid/Lua/` for chat relay, rank
  sync, siege/drop events and sound alerts. Without it, only the read-only features
  (dashboard, tracking, roster) work.

---

## License

MIT — derived from [StewBagger/Jeeves](https://github.com/StewBagger/Jeeves).
