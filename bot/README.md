# PZ Tambayan Discord Bot

A remote Discord bot for the **PZ Tambayan** Project Zomboid server, refactored
from [JeevesBot](https://github.com/StewBagger/Jeeves) (MIT © StewBagger).

The key difference from upstream Jeeves: **this bot does not run on the game
server.** It runs on a separate VPS and talks to the server over two channels:

- **RCON** — server commands (`players`, `servermsg`, `save`, `quit`, `teleport`, …)
- **SFTP** — the Jeeves mod file bridge (`Zomboid/Lua/`) and log tailing (`Zomboid/Logs/`)

The Jeeves mods cannot tell — and do not care — whether the bytes in `Zomboid/Lua/`
arrived from a local process or over SFTP, so every file-bridge feature works
unchanged from a remote VPS.

---

## What's retained vs. removed

### Retained (remote-viable over RCON + SFTP)

| Feature | Commands / cogs |
|---|---|
| Player tracking, welcome / welcome-back notes | `player_tracker.py` |
| Player death notifications | `player_tracker.py` (see "Death tracking") |
| World & player status dashboard | `server_status.py` |
| Chat relay (in-game ↔ Discord) | `chat_relay.py` |
| Rank sync (Discord roles → in-game name colours) | `rank_sync.py` |
| Horde events | `horde_events.py` |
| Horde survivor leaderboard | `horde_leaderboard.py` |
| Airdrop / supply events | `jeeves_drops.py` |
| Sound alerts | `/playsound` |
| Mod list (read-only) | `/modlist` |
| Server admin (players, teleport, access level, broadcast, stop) | `main.py` |

### Removed (same-server only — no remote path)

- Server **start / restart / auto-restart** — RCON cannot launch a stopped process.
- **Crash recovery** (auto-restart on death) — needs process control.
- **`/update`** and mod **add/remove** — require SteamCMD on the host.
- **Mod load-order analysis** (`/modorder`, `/modsort`, `/modinfo`, `/modreorder`) —
  requires recursively reading the entire workshop content folder over SFTP.
- `mod_check_timer.py`, `auto_restart.py`, `server_update.py`, `workshop_acf.py`,
  `mod_sorter.py`, `jeeves_modsorter.py`.

> **`/stop` is kept** but now issues RCON `save` + `quit` (graceful). Starting the
> server again is a host-panel action.

---

## Requirements

- A Project Zomboid dedicated server (Build 42) with **RCON enabled** and **Jeeve's
  Integration** installed (required for the world dashboard, chat relay, rank sync).
- Jeeve's Hordes / Jeeve's Drops (only if you want those features).
- A separate Linux VPS for the bot, with Python 3.10+.
- SFTP access to the game server (`Zomboid/Lua/` read+write for the bridge features;
  `Zomboid/Logs/` read for tracking).

---

## Install

```bash
git clone <your-repo> pz-tambayan-bot && cd pz-tambayan-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.env.example config.env    # then edit it
.venv/bin/python main.py
```

`run.sh` (Linux) and `run_bot.bat` (Windows) do the venv setup automatically on first run.

---

## Configuration (`config.env`)

```dotenv
# Discord
DISCORD_TOKEN=...
DISCORD_GUILD_ID=...
DISCORD_CHANNEL_ID=...        # announcements (joins, deaths, status)

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
SFTP_ZOMBOID_ROOT=/home/pz/Zomboid
# Optional overrides:
# SFTP_LUA_DIR=/home/pz/Zomboid/Lua
# SFTP_LOGS_DIR=/home/pz/Zomboid/Logs
# SFTP_SERVER_INI=/home/pz/Zomboid/Server/pztambayan.ini   (for /modlist)
```

See `config.env.example` for the full commented reference (roles, ranks, emoji,
optional channel IDs).

---

## Commands

Admin commands require the role named by `DEFAULT_ROLE` (default `Admin`).

| Command | Description |
|---|---|
| `/hello` | Health check |
| `/online` | Is the server online? |
| `/players` | Currently connected players |
| `/playerlist` | Everyone who has ever joined (+ deaths) |
| `/teleport` | Teleport player1 to player2 |
| `/setaccesslevel` | Set a player's access level |
| `/msg` | Broadcast a message to the server |
| `/stop` | Graceful shutdown (RCON save + quit) |
| `/playsound` | Trigger a Jeeves Alerts sound |
| `/modlist` | Show configured mods / Workshop items |
| `/myrank` | Show your rank (everyone) |
| `/linkme`, `/unlinkme` | Link your Discord to your PZ name |
| `/setrank`, `/syncranks`, `/linkname`, `/unlinkname` | Rank admin |
| `/horde`, `/hordeoff`, `/hordestatus`, … | Horde events (needs Jeeve's Hordes) |
| `/airdrop`, `/airdropstatus`, … | Airdrops (needs Jeeve's Drops) |

---

## Death tracking — action required

Jeeves has **no death tracking**; this refactor adds the framework but the exact
death-log line varies by build. Before going live:

1. Pull a raw `Zomboid/Logs/` listing from the live Tambayan server and find where
   deaths are written.
2. Set `_DEATH_RE` in `player_tracker.py` to match that line.

The recommended robust alternative is a small server-side Lua mod that writes a
`jeeves_deaths.txt` (mirroring `jeeves_world_status.txt`); point the tail loop at it.

---

## Customizing for PZ Tambayan

- **Dashboard branding** — `server_status.py`: `set_author(name=...)`, `ICON_URL`, `IMAGE_URL`.
- **Welcome / death / welcome-back strings** — `player_tracker.py`.
- **Rank names & colours** — `config.env` `RANK_1..RANK_6` + `rank_sync.py` `ROLE_TO_RANK`
  + `chat_relay.py` `ANSI_COLORS`.
- **Chat channels to relay** — `chat_relay.py` `RELAY_CHAT_TYPES`.
- **Emoji** — `config.env` `EMOJI_*` overrides.

---

## Running as a service (systemd)

```ini
[Unit]
Description=PZ Tambayan Discord bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pztambayan
WorkingDirectory=/opt/pz-tambayan-bot
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONIOENCODING=utf-8
ExecStart=/opt/pz-tambayan-bot/.venv/bin/python /opt/pz-tambayan-bot/main.py
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
```

> No `KillMode=process` is needed here — the bot no longer owns the game server as a
> child process, so a bot restart cannot take the server down.

---

## Security notes

- `config.env` holds the Discord token, RCON password and SFTP credentials — never
  commit it (it is gitignored).
- SFTP host-key checking is disabled (`known_hosts=None`) in `sftp_client.py`. For
  production, pin the server's host key instead.
- The SFTP account must have **write** access to `Zomboid/Lua/` for chat relay, rank
  sync, horde/drop events and sound alerts. Without it, only the read-only features
  (dashboard, tracking) work.

---

## Project layout

```
bot/
├── main.py               # slim bot: config, RCON, state, slash commands
├── sftp_client.py        # reconnecting async SFTP client (the new abstraction)
├── lua_bridge.py         # SFTP-backed file bridge to the Jeeves mods
├── player_tracker.py     # joins, welcome/death notes (SFTP log tail + SQLite)
├── server_status.py      # world/player dashboard (SFTP status reads + RCON)
├── chat_relay.py         # in-game <-> Discord chat (SFTP tail + bridge write)
├── rank_sync.py          # Discord roles -> in-game colours (SFTP write)
├── horde_events.py       # horde event control (via lua_bridge)
├── horde_leaderboard.py  # survivor leaderboard (via lua_bridge)
├── jeeves_drops.py       # airdrop/supply events (via lua_bridge)
├── jeeves_modmanager.py  # /modlist (SFTP ini read)
├── config.env.example
├── requirements.txt
├── run.sh / run_bot.bat
└── LICENSE               # MIT (upstream Jeeves)
```

---

## License

MIT — derived from [StewBagger/Jeeves](https://github.com/StewBagger/Jeeves).
