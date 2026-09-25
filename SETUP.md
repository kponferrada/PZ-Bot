# PZ Tambayan Bot — Setup Guide

A step-by-step guide to standing up the **PZ Tambayan** Discord bot end-to-end:
Discord application, game-server prerequisites, VPS deployment, and systemd.

## How it works

The bot **does not run on the game server**. It runs on a separate Linux VPS and
talks to the Project Zomboid server over two channels:

| Channel | Purpose |
|---|---|
| **RCON** | server commands — `players`, `save`, `quit`, `servermsg`, `teleport`, … |
| **SFTP** | file bridge to the Jeeves mods (`Lua/`) + log tailing (`Logs/`) + player DB read |

---

## Prerequisites

- A Discord account with permission to add bots to your server.
- A Project Zomboid **dedicated server (Build 42)** with **RCON** and **SFTP** access.
- **Jeeve's Integration** mod — required for the world dashboard, chat relay, rank sync.
- (Optional) **Siege Night** (`3669589584`) + `siege-night-bridge`, **Jeeve's Drops**,
  **Death Log** (`2972685375`), **Aegis Panel** (`3766508989`) — for those features.
- A **Linux VPS** with Python 3.10+.

---

## 1. Create the Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** → name it (e.g. `PZ Tambayan`).
2. **Bot** → **Reset Token** → copy it (this is `DISCORD_TOKEN`).
3. Under **Privileged Gateway Intents**, enable **all three**: *Presence*, *Server Members*, *Message Content* (the bot uses `Intents.all()`).
4. **OAuth2 → URL Generator** → scopes **`bot`** and **`applications.commands`** → Bot Permissions: **Send Messages, Embed Links, Attach Files, Manage Messages, Read Message History** → copy and open the invite URL to add the bot to your server.
5. Enable **Developer Mode** (User Settings → Advanced) so you can right-click → **Copy ID** on channels and roles.

---

## 2. Game-server prerequisites

### RCON
In the server's `.ini` (`pzserver.ini`), set:
```ini
RCONPort=<game-port + 1>
RCONPassword=<your-password>
```
On **Indifferent Broccoli**, RCON port = game port + 1, and the RCON password is the same value as the SFTP/FTP password.

### SFTP
The bot needs SFTP read/write to the Zomboid data folder. On **Indifferent Broccoli** the layout is:

| Path | Purpose |
|---|---|
| `/server-data/` | SFTP root (`Lua/`, `Logs/`, `Server/`, `db/` beneath it) |
| `/server-data/Server/pzserver.ini` | server `.ini` (MaxPlayers, server name, mods) |
| `/server-data/db/pzserver.db` | server player database (known players) |
| `/server-data/Logs/` | `*_chat.txt` / `*_user.txt` / `*_DebugLog*.txt` (tracking + restart watch) |
| `/server-files/steamapps/workshop/content/108600` | Workshop mods (`/modlist`) |

### Mods
- **Jeeve's Integration** — writes `jeeves_world_status.txt` etc. to `Lua/` (dashboard/chat/rank bridge).
- **Death Log** (`2972685375`) — writes the death log the bot tails for death notifications.
- **Aegis Panel** (`3766508989`) — writes the player-stats ledger for `/stats` and `/leaderboard`.
- **Siege Night** (`3669589584`) + `siege-night-bridge` — siege-night notifications (optional).
- **Jeeve's Drops** — airdrop / supply-drop events (optional).

> **Restarts are bot-driven** — the bot stops the server over RCON (save → kick →
> quit) and the host applies updates and brings it back up. No PhunServer 2 required.

---

## 3. Deploy the bot to the VPS

```bash
# live (pinned to a release tag)
git clone https://github.com/kponferrada/PZ-Bot.git /opt/pz-tambayan-bot
cd /opt/pz-tambayan-bot && git checkout v0.4.1   # latest tag — see VERSIONING.md

# create + fill the config
cp bot/config.env.example bot/config.env
nano bot/config.env
```

The code lives in the `bot/` subdirectory (that's where `config.env` and `run.sh` are).

### Fill in `config.env`

The minimum required values:

```dotenv
# Discord
DISCORD_TOKEN=...
DISCORD_GUILD_ID=...
DISCORD_CHANNEL_ID=...          # default notification channel

# RCON
RCON_HOST=your.server.host
RCON_PORT=26996
RCON_PASSWORD=...

# SFTP
SFTP_HOST=your.server.host
SFTP_PORT=22
SFTP_USER=...
SFTP_PASSWORD=...

# Indifferent Broccoli paths
SFTP_ZOMBOID_ROOT=/server-data
SFTP_SERVER_INI=/server-data/Server/pzserver.ini
SFTP_SERVER_DB=/server-data/db/pzserver.db
SFTP_MODS_DIR=/server-files/steamapps/workshop/content/108600
```

See `bot/config.env.example` for the **full commented reference** (ranks, emoji, every optional channel).

### First run (foreground, to verify)

```bash
cd /opt/pz-tambayan-bot/bot
./run.sh
```

`run.sh` creates `.venv` + installs `requirements.txt` on first run, then launches the bot. You should see `Bot started up OK — server online/offline`.

---

## 4. Run as a systemd service

```bash
sudo /opt/pz-tambayan-bot/deploy/install-service.sh /opt/pz-tambayan-bot/bot pz-tambayan-bot
```

This pre-creates the venv, writes the unit, then `enable --now`s it.

```bash
systemctl status   pz-tambayan-bot      # check status
systemctl restart  pz-tambayan-bot      # restart
systemctl stop     pz-tambayan-bot      # stop
journalctl -u pz-tambayan-bot -f        # live logs
```

The service auto-restarts on crash (`Restart=always`) and auto-starts on reboot (`enable`).

---

## 5. Channels & roles

Create the channels/roles you want, copy their IDs (Developer Mode → right-click → Copy ID), and set them in `config.env`. Any `*_CHANNEL_ID` left `0` falls back to `DISCORD_CHANNEL_ID`.

| Config | Notification it routes |
|---|---|
| `STATUS_CHANNEL_ID` | auto-updating server-status dashboard |
| `CHAT_RELAY_CHANNEL_ID` | in-game ↔ Discord chat relay |
| `SERVER_NOTIFICATION_CHANNEL_ID` | server up/down + restart/mod-update banners |
| `WORKSHOP_UPDATE_CHANNEL_ID` / `WORKSHOP_UPDATE_ROLE_ID` | mod-update restart relay |
| `DEATH_LOGS_CHANNEL_ID` | player death logs |
| `AIRDROP_CHANNEL_ID` / `AIRDROP_ROLE_ID` | air-drop / supply-drop events |
| `SIEGE_CHANNEL_ID` / `SIEGE_ROLE_ID` | siege-night events |
| `JOIN_LEAVE_CHANNEL_ID` | join/leave notifications |
| `WHITELIST_CHANNEL_ID` / `WHITELIST_APPROVAL_CHANNEL_ID` | whitelist application + approval |
| `NOTIFY_ROLE_ID` | @-mentioned in every server up/down banner |

---

## 6. Verify

- **Status panel** should appear in `STATUS_CHANNEL_ID` and refresh every 30 s.
- **Join/leave** — a player connecting/disconnecting posts to `DISCORD_CHANNEL_ID`.
- **Restart** — at the scheduled time, the `server-restarting.png` banner posts, then `Server restart complete!` after it comes back.
- `/modlist`, `/players`, `/playerlist` should respond in Discord.

---

## Running a live + test bot side by side

You can run two instances on the same VPS with two Discord bots:

```bash
# live (tag) and test (develop) checkouts
git clone https://github.com/kponferrada/PZ-Bot.git /opt/pz-tambayan-bot       # -> checkout v0.4.1
git clone https://github.com/kponferrada/PZ-Bot.git /opt/pz-tambayan-bot-test  # -> checkout develop

# each gets its own config.env (different token + channel IDs)
sudo ~/Live/PZ-Tambayan-Bot/deploy/install-service.sh    ~/Live/PZ-Tambayan-Bot/bot   pz-tambayan-bot-live
sudo ~/Test/PZ-Tambayan-Bot/deploy/install-service.sh    ~/Test/PZ-Tambayan-Bot/bot   pz-tambayan-bot-test
```

Keep **live → tag**, **test → `develop`**. They share SFTP/RCON but have separate tokens, channels, and state.

---

## Versioning

Semantic version tags (`v0.1.0`, `v0.2.0`, …) on `main`; feature work on `develop`. See [`VERSIONING.md`](VERSIONING.md).

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Failed to load server_status: ModuleNotFoundError: PIL` | Pillow missing — `bot/.venv/bin/pip install Pillow` |
| No @-mention in announcements | `NOTIFY_ROLE_ID` (or the event `*_ROLE_ID`) is `0` — set the real role ID |
| `SFTP connect failed … port 22` | SFTP host/port wrong, or outbound 22 blocked from the VPS |
| `/modlist` `Embed size exceeds 6000` | running stale code — restart the bot |
| Every player shows as "new" | `SFTP_SERVER_DB` wrong, or the DB read failed — check the `[ServerConfig]` log lines |
| `Forbidden` / `50001` on commands | missing permissions or `applications.commands` scope — re-invite the bot |

---

## Security

- `config.env` holds the Discord token + RCON/SFTP credentials — it is **gitignored**, never commit it.
- SFTP host-key checking is disabled (`known_hosts=None`); for production, pin the host key in `sftp_client.py`.
- The SFTP account needs **write** to `Lua/` for chat relay / rank sync / horde / drops; read-only still works for dashboard + tracking.
