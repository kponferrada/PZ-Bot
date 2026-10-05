# PZ Tambayan Discord Bot

The Discord bot for the **PZ Tambayan** Project Zomboid (Build 42) dedicated server.
It is a refactor of [JeevesBot](https://github.com/StewBagger/Jeeves) (MIT © StewBagger).

This README is the project's only user and operator document. Maintainer notes
(architecture, invariants, known traps) are in [`CLAUDE.md`](CLAUDE.md). The
commented reference for every config key is [`bot/config.env.example`](bot/config.env.example).

## How it works

The bot does **not** run on the game server. It runs on a separate Linux VPS and
reaches the server two ways:

| Channel | Used for |
|---|---|
| **RCON** | `players`, `servermsg`, `save`, `quit`, `kickuser`, `adduser`, `teleport`, `setaccesslevel`, … |
| **SFTP** | the file bridge to the server mods (`Lua/`), log tailing (`Logs/`), the server `.ini`, the player DB (`db/pzserver.db`), the Workshop manifest |

The server mods only read and write small files in `Zomboid/Lua/`. They cannot
tell whether a file was written by a local process or over SFTP, so every
bridge feature works from a remote VPS.

The bot can **stop** the server (save → kick → quit over RCON). It cannot
**start** it. The host brings the process back up and applies Workshop updates
on boot.

## Features

| Feature | Module(s) | Needs (server side) | Toggle key |
|---|---|---|---|
| Join / leave / welcome banners, split-screen players | `player_tracker.py`, `player_banner.py` | `Logs/*_user.txt` | `join_leave` |
| Death certificates (portrait from the `/linkme` Discord avatar, injury diagram, cause, survival time, day/week counts) | `death_log.py`, `death_card.py`, `death_store.py` | Death Log mod (`2972685375`) | `deaths` |
| Auto-updating status dashboard (embed or rendered card) | `server_status.py`, `status_card.py`, `game_calendar.py` | Jeeve's Integration | `status_dashboard` |
| Server up / restarting / down banners | `main.py` (`monitor_server_state`) | RCON | `server_up_down` |
| In-game ↔ Discord chat relay | `chat_relay.py` | Jeeve's Integration | `chat_relay` |
| In-game name colours from the Barangay Tales reputation ladder (or Discord roles) | `rank_sync.py`, `bt_progression.py` | Jeeve's Integration, Barangay Tales | — |
| Siege Night warnings, start, per-wave, end; admin control | `siege_night.py` | Siege Night (`3669589584`) + `siege-night-bridge` | `siege_night` |
| Airdrop / supply-event notices and triggers | `jeeves_drops.py` | Jeeve's Drops | `airdrops` |
| Jamie's Fortune jackpot announcements | `jamies_fortune.py` | Jamie's Fortune | `jackpots` |
| Workshop update checker → controlled restart | `mod_checker.py`, `restart_watch.py` | Steam Web API (key for unlisted items) | `mod_updates` |
| Scheduled restarts (UTC hours) | `restart_watch.py` | RCON | `scheduled_restarts` |
| Restart notices (countdown, kick warning, deferral) | `restart_watch.py` | RCON | `restarts` |
| Whitelist applications, approval, admin | `whitelist.py` | RCON | `whitelist` |
| `/stats` and `/leaderboard` | `stats.py`, `aegis_stats.py` | Aegis Panel (`3766508989`) | — |
| `/rpleaderboard`: weekly RP standings and the title each top-5 place earns | `weekly_rp.py`, `bt_progression.py` | Barangay Tales (`BarangayTales/progression.json`) | — |
| `/modlist` | `jeeves_modmanager.py`, `server_config.py` | server `.ini` | — |
| Feature toggles | `features.py`, `feature_controls.py` | — | — |
| Cleanup of regenerable bot files | `cleanup.py` | — | — |

Toggle keys are switched at runtime with `/enable` and `/disable` and listed by
`/features`. The state survives restarts in `feature_state.json`
(override with `FEATURE_STATE_PATH`).

## Commands

Admin commands require the Discord role named by `DEFAULT_ROLE` (default `Admin`).
`/help` (admin) prints the same list in Discord.

**Everyone:** `/myrank`, `/linkme`, `/unlinkme`, `/stats <username>`,
`/leaderboard <kind>`, `/rpleaderboard [limit]`, `/features`, `/whitelist`.

**Admin:**

| Area | Commands |
|---|---|
| Status | `/hello`, `/online`, `/players`, `/playerlist`, `/modlist`, `/help` |
| Server control | `/msg`, `/announce`, `/playsound`, `/teleport`, `/setaccesslevel`, `/stop` |
| Restarts | `/restart` (countdown), `/restartnow` (kick in 30 s), `/deferrestart [minutes]` (default 15, max 720, auto-resumes), `/forcemodupdate` |
| Ranks and links | `/setrank`, `/syncranks`, `/linkname`, `/unlinkname`, `/listlinks` |
| Siege Night | `/siegestatus`, `/siegestart`, `/siegestop`, `/siegeschedule <day>` |
| Drops | `/airdrop [player] [crate]`, `/airdropstatus`, `/supplyevent`, `/supplyeventstatus` |
| Whitelist | `/whitelistsetup`, `/whitelistadd`, `/whitelistmodify`, `/whitelistlist`, `/whitelistremove` |
| Features | `/enable <feature>`, `/disable <feature>` |

### Restart flow

All restarts (scheduled, mod update, `/restart`, `/forcemodupdate`, a deferral
that expires) use one sequence:

- **No players online:** save, post the banner, quit.
- **Players online:** a countdown of `MOD_CHECK_RESTART_DELAY_SECONDS` (default 300 s).
  Kick warning at T-120 s, `save` at T-90 s, `kickuser` everyone at T-60 s, then save and `quit` at T-0.

`/restartnow` saves at once, kicks after 30 s, then quits. A restart is refused
when one is already running or when RCON does not answer. Scheduled hours come
from `RESTART_SCHEDULE_UTC` (default `4,10,16,22`, UTC).

### Weekly RP leaderboard

`/rpleaderboard [limit]` (default 10, max 25) reads the export that the Barangay
Tales mod writes every 5 to 10 minutes. It shows:

- this week's Reputation Points (RP) standings. RP comes from wealth, zombie kills, quests and event wins.
- the title each top-5 place earns when the week ends (Monday 00:00 GMT+8): Legendary, Elite, Master, Veteran, Rising Survivor. Winners hold the title for the following week.
- last week's winners and the titles they were actually granted.

The export doesn't contain the place → title table, so the bot keeps a copy in
`DEFAULT_WEEKLY_TITLES` (`bot/bt_progression.py`). If the titles in BT's
`Config.WeeklyRanking.titles` change, update that copy too.

### Ranks from the reputation ladder

With `RANK_SOURCE=bt_ladder` (the default), in-game ranks (chat name colours)
follow the same weekly ladder instead of Discord roles:

| Ladder position | Rank |
|---|---|
| Last week's 1st (Legendary Survivor) | 6 Inferno |
| 2nd (Elite) | 5 Blaze |
| 3rd (Master) | 4 Flame |
| 4th (Veteran) | 3 Cinder |
| 5th (Rising) | 2 Spark |
| Any RP earned this week | 1 Fuel |

Every player on the ladder gets a rank, linked to Discord or not. The bot
re-reads the export every 5 minutes and writes `jeeves_ranks.lua` only when a
rank changed; `/syncranks` forces it. `/setrank` still works but the next
ladder sync replaces it. With `RANK_LADDER_ROLES=true`, members linked with
`/linkme` also get the matching `RANK_n` Discord role and lose the other rank
roles (needs Manage Roles, bot role above the rank roles), so `/myrank` shows
the ladder rank too. `RANK_SOURCE=roles` restores the old role-based ranks.

## Setup

### 1. Discord application

1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. **Bot** → **Reset Token**. The token is `DISCORD_TOKEN`.
3. Turn on all three **Privileged Gateway Intents** (Presence, Server Members, Message Content). The bot uses `Intents.all()`.
4. **OAuth2 → URL Generator**: scopes `bot` **and** `applications.commands`; permissions Send Messages, Embed Links, Attach Files, Manage Messages, Read Message History. Open the URL to invite the bot.
5. Turn on Developer Mode in Discord (Settings → Advanced) to copy channel and role IDs.

### 2. Game server

- **RCON:** `RCONPort` and `RCONPassword` in the server `.ini`. On Indifferent Broccoli the RCON port is the game port + 1.
- **SFTP:** read and **write** on `Lua/` (bridge features), read on `Logs/`, `Server/`, `db/` and the Workshop folder. Without write access only the read-only features work (dashboard, tracking, roster).
- **Mods:** install the ones for the features you want (see the Features table).

Indifferent Broccoli layout:

| Path | Contents |
|---|---|
| `/server-data/` | `SFTP_ZOMBOID_ROOT`: `Lua/`, `Logs/`, `Server/`, `db/` |
| `/server-data/Server/pzserver.ini` | server `.ini` (name, MaxPlayers, mods) |
| `/server-data/db/pzserver.db` | known players |
| `/server-files/steamapps/workshop/content/108600` | Workshop mods (`SFTP_MODS_DIR`) |

### 3. Install on the VPS

Python 3.10 or newer. The code lives in `bot/`.

```bash
git clone https://github.com/kponferrada/PZ-Bot.git /opt/pz-tambayan-bot
cd /opt/pz-tambayan-bot
git checkout <release-tag>          # see "Releases" below
cp bot/config.env.example bot/config.env
nano bot/config.env
```

Required keys: `DISCORD_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_ID`,
`RCON_PASSWORD`, `SFTP_HOST`, `SFTP_USER`, and `SFTP_PASSWORD` or `SFTP_KEY_PATH`.
The bot refuses to start without them. Set `SFTP_ZOMBOID_ROOT` (and
`SFTP_SERVER_INI`, `SFTP_MODS_DIR` on Indifferent Broccoli). Everything else
is optional and documented in `config.env.example` (for example
`BT_PROGRESSION_PATH` for the Barangay Tales export).

Run once in the foreground to check it:

```bash
cd /opt/pz-tambayan-bot/bot && ./run.sh
```

`run.sh` (Linux) and `run_bot.bat` (Windows) create `.venv` and install
`requirements.txt` on first run. A good start prints
`Bot started up OK — server online/offline`.

### 4. systemd service

```bash
sudo /opt/pz-tambayan-bot/deploy/install-service.sh /opt/pz-tambayan-bot/bot pz-tambayan-bot
```

The script creates the venv, writes `/etc/systemd/system/<name>.service`
(`Restart=always`) and enables it. `deploy/pz-tambayan-bot.service` is a reference
copy of the unit.

```bash
systemctl status pz-tambayan-bot
journalctl -u pz-tambayan-bot -f
```

### 5. Channels and roles

Every `*_CHANNEL_ID` left at `0` falls back to `DISCORD_CHANNEL_ID`.
`STATUS_CHANNEL_ID` and `CHAT_RELAY_CHANNEL_ID` have no fallback: unset means the
feature is off.

| Key | Receives |
|---|---|
| `STATUS_CHANNEL_ID` | the status dashboard (refreshes every 30 s) |
| `CHAT_RELAY_CHANNEL_ID` | chat relay (only the `General` in-game channel) |
| `SERVER_NOTIFICATION_CHANNEL_ID` + `NOTIFY_ROLE_ID` | up/down banners and every restart / mod-update notice |
| `JOIN_LEAVE_CHANNEL_ID` | join / leave banners |
| `DEATH_LOGS_CHANNEL_ID` | death cards |
| `AIRDROP_CHANNEL_ID` / `AIRDROP_ROLE_ID` | drops and supply events |
| `SIEGE_CHANNEL_ID` / `SIEGE_ROLE_ID` | Siege Night |
| `JACKPOT_CHANNEL_ID` | Jamie's Fortune jackpots |
| `WHITELIST_CHANNEL_ID` / `WHITELIST_APPROVAL_CHANNEL_ID` | whitelist button / approval queue |

`WORKSHOP_UPDATE_CHANNEL_ID` and `WORKSHOP_UPDATE_ROLE_ID` are read but unused.

### Live and test instances

Run two checkouts with two Discord bots and two services. Each has its own
`config.env` (token, channel IDs) and its own local state files; both share the
game server's RCON and SFTP.

```bash
sudo <live-checkout>/deploy/install-service.sh <live-checkout>/bot pz-tambayan-bot-live
sudo <test-checkout>/deploy/install-service.sh <test-checkout>/bot pz-tambayan-bot-test
```

Live tracks a release tag. Test tracks `develop`.

## Releases

Semantic version tags `vX.Y.Z` on `main` (patch = fixes, minor = features,
major = breaking). Work lands on `develop` first.

1. Commit to `develop`, test on the test instance.
2. Merge `develop` into `main`, tag it, push `main` and the tag.
3. On the live VPS: `git fetch --tags && git checkout vX.Y.Z`, then restart the service.

State on 2026-10-04: the newest tag is **`v0.4.1`** (2026-09-13). `origin/main`
has since been fast-forwarded to `develop` without a tag, so it carries about
75 untagged commits (feature toggles, restart rework, death cards, banners,
jackpots, whitelist, …). The next tag should be `v0.5.0`.

## Customizing

| What | Where |
|---|---|
| Dashboard title, icon, banner, player cap, embed vs card | `DASHBOARD_*`, `MAX_PLAYERS`, `STATUS_MODE` in `config.env` |
| Banner images | `ANNOUNCE_*_IMAGE` in `config.env`; files in `bot/assets/` |
| Join / leave / death wording | `player_tracker.py`, `death_log.py` |
| Death certificate art | `bot/assets/death-certificate-source.webp` → `python scripts/build_death_certificate.py` (needs numpy + opencv) → `death-certificate.png`; field positions in `death_card.py` |
| Rank names and colours | `RANK_1..6` (config), `ROLE_TO_RANK` / `RANK_DISPLAY` in `rank_sync.py`, `ANSI_COLORS` in `chat_relay.py`, `_RANK_INFO` in `main.py` |
| Relayed chat channels | `RELAY_CHAT_TYPES` in `chat_relay.py` |
| Emoji | `EMOJI_*` in `config.env` |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Failed to load server_status: ModuleNotFoundError: PIL` | Pillow missing: `bot/.venv/bin/pip install -r requirements.txt` |
| `SFTP connect failed … port 22` | wrong `SFTP_HOST`/`SFTP_PORT`, or outbound port blocked |
| `Forbidden` / `50001` when syncing commands | bot invited without `applications.commands`; re-invite |
| Every player shows as new | `SFTP_SERVER_DB` wrong or unreadable; look for `[PlayerTracker] Cannot read …` / `Seeded N known player(s)` in the log |
| No @-mention on announcements | `NOTIFY_ROLE_ID` (or the event `*_ROLE_ID`) is `0` |
| `previous command not consumed after 5s` | the mod that reads that bridge file is missing or not polling |

## Security

- `bot/config.env` holds the Discord token, RCON password and SFTP credentials. `*.env` is gitignored; never commit it.
- SFTP host keys are not verified (`known_hosts=None` in `sftp_client.py`).
- The whitelist stores applicants' game passwords in plain text in `whitelist_requests.csv` and shows them in the approval channel. Restrict that channel and the bot host.
- The systemd unit runs as `root`. Change `User=` to run it unprivileged.

## License

MIT, derived from [StewBagger/Jeeves](https://github.com/StewBagger/Jeeves). See [`LICENSE`](LICENSE).
