# PZ Tambayan Bot — Migration & Customization Guide

**From:** `StewBagger/Jeeves` (same-box server manager)
**To:** remote bot on a separate VPS, reading/writing over **SFTP**, commanding over **RCON**.
**Decision rule:** retain anything remote-viable (SFTP read/write + RCON); remove anything that needs same-server capability (process start/kill, SteamCMD).

---

## 0. Final Retain / Remove Decision (grounded in the code)

### RETAIN (remote-viable — no process control, no SteamCMD)

| Module | File I/O it does | Change needed |
|---|---|---|
| `lua_bridge.py` | **ALL** bridge I/O (central) | **Re-point to SFTP (the keystone)** |
| `horde_events.py` | none (uses `lua_bridge`) | **Zero changes** |
| `jeeves_drops.py` | none (uses `lua_bridge`) | **Zero changes** |
| `horde_leaderboard.py` | none (uses `lua_bridge`) | **Zero changes** |
| `server_status.py` | none (uses `lua_bridge` + RCON) | ~1 line (drop "next restart") |
| `player_tracker.py` | own log tail (`glob`/`getsize`/`open`) | Swap tail → SFTP; add deaths |
| `chat_relay.py` | own log tail + `lua_bridge.chat_relay` | Swap tail → SFTP |
| `rank_sync.py` | own `write_text` (ranks file) + `lua_bridge.rank_push` | Swap ranks-file write → SFTP |
| `jeeves_modsorter.py` | `read_text` on the `.ini` | Swap → SFTP |
| `mod_sorter.py` | `read_text`/`rglob` on workshop folder | Swap → SFTP |
| `jeeves_modmanager.py` | ini read/write + SteamCMD (partial) | Keep list/info/reorder; drop add/remove |
| `Jeeves.py` (main) | RCON + state + commands + process control | Rewrite: drop process/SteamCMD/ModChecker |

### REMOVE (same-server only — no remote path)

| Module / part | Why |
|---|---|
| `server_update.py` | `/update` runs SteamCMD locally |
| `mod_check_timer.py` | crash detection + auto-restart (needs process start) |
| `auto_restart.py` | scheduled restart (needs process start) |
| `Jeeves.py`: `start_server`/`restart_server`/`stop_server`/`discover_server_pid`/`monitor_until_online`/`_tasklist`/`_taskkill` | process launch + kill |
| `Jeeves.py`: `ModChecker` + `/mod` + `/cleanmods` | reads `.ini`/workshop locally, triggers restart |
| `jeeves_modmanager.py`: `/modadd`, `/modremove`, `_download_workshop_item` | SteamCMD |
| `workshop_acf.py` | local `appworkshop` manifest (only used by removed `ModChecker`) |

> **`/stop` is retained** — it becomes RCON `quit` only (graceful shutdown, no process kill).
> **Crash detection is retained** in spirit — the status dashboard already uses the mod's
> `OnTick`-written status-file freshness as the liveness signal, which is remote-viable. Only the
> *auto-restart recovery* is dropped.

---

## 1. The One New File: `sftp_client.py`

Everything else hangs off this. One reconnecting async SSH/SFTP client.

```python
# sftp_client.py — reconnecting async SFTP wrapper (asyncssh)
import asyncssh

class SftpClient:
    def __init__(self, host, port, user, password=None, key_path=None):
        ...

    async def connect(self):        # asyncssh.connect; called lazily, reconnects on drop
    async def read_text(self, path) -> str | None
    async def stat(self, path) -> tuple[int, int] | None     # (size, mtime) or None
    async def exists(self, path) -> bool                     # via stat
    async def list_dir(self, path) -> list[str]              # names only
    async def newest_matching(self, dir, pattern) -> str | None  # glob → newest by mtime
    async def tail(self, path, offset) -> tuple[str, int]    # read from offset → (text, new_offset)
    async def write_text_atomic(self, path, content) -> bool # write path.tmp then rename
```

Key points:
- **`tail`** replaces the `open()/seek()/read()` pattern used by `player_tracker` and `chat_relay`.
  `asyncssh.SFTPClientFile` supports `.seek()`, so the tail loop logic ports verbatim.
- **`write_text_atomic`** writes to `<path>.tmp` then `rename`s — atomic over SFTP — so the game's
  mod never reads a half-written command file. (The original local code writes directly; fine for
  a local disk, unsafe over a network.)
- **`newest_matching`** replaces `sorted(dir.glob(pattern))[0]` used for log-rotation detection.

---

## 2. Configuration Changes (`config.env`)

### Remote path keys (these move from "local path" to "path on the server")

| Old key (local) | New meaning | New key |
|---|---|---|
| `SERVER_INI_PATH` | server `.ini`, read over SFTP | `SFTP_SERVER_INI` |
| `USER_LOG_PATH` | `Zomboid/Logs/` over SFTP | `SFTP_LOGS_DIR` |
| `CHAT_LOG_PATH` | (same folder) | `SFTP_LOGS_DIR` |
| `MODS_FOLDER_PATH` | workshop `content/108600` over SFTP | `SFTP_MODS_DIR` |
| *(derived)* `…/Lua` | `Zomboid/Lua/` over SFTP | `SFTP_LUA_DIR` |

### New connection keys

```dotenv
SFTP_HOST=<server host or ip>
SFTP_PORT=22
SFTP_USER=<sftp username>
SFTP_PASSWORD=<password>          # or SFTP_KEY_PATH=/path/to/key
```

### Recommended simplification — one root instead of four paths

```dotenv
SFTP_ZOMBOID_ROOT=/home/pz/Zomboid      # contains Lua/, Logs/, Server/
SFTP_SERVER_INI=/home/pz/Zomboid/Server/pztambayan.ini
SFTP_MODS_DIR=/home/pz/pzserver/steamapps/workshop/content/108600
```
Then derive: `SFTP_LUA_DIR = {root}/Lua`, `SFTP_LOGS_DIR = {root}/Logs`. `lua_bridge.init()`
currently does `Path(ini_path).parent.parent / "Lua"` — change that one line to read `SFTP_LUA_DIR`
(or compute it from `SFTP_ZOMBOID_ROOT`).

### Dropped keys (no longer used)
`SERVER_BATCH`, `SERVER_PROCESS_NAME`, `STEAMCMD_PATH`, `UPDATE_LOG_PATH`, `STARTUP_WAIT`,
`CHECK_INTERVAL`, `MONITOR_RETRIES` (all serve process control / SteamCMD / mod update).

---

## 3. Per-Module Modification Notes

### 3.1 `lua_bridge.py` — THE KEYSTONE (the only file that touches the mods' files)

This file is the single choke-point: every command write and every status read goes through it.
Change **only** its filesystem primitives and the rest of the bridge features follow for free.

Locations to change (from the current source):

1. **`init(bot)`** (lines 45–61) — replace
   `_lua_dir = Path(ini_path).parent.parent / "Lua"` + `_lua_dir.mkdir(...)`
   with `_lua_dir = <SFTP_LUA_DIR from config>` (a remote path string, no `mkdir` — SFTP can't
   guarantee `mkdir`; the dir must already exist on the server, which it does once the mod runs).
2. **`_write_file`** (lines 131–160) — replace the consume-wait + write:
   - `filepath.exists()` → `await sftp.exists(path)`
   - `filepath.stat().st_size == 0` → `(await sftp.stat(path))[0] == 0`
   - `filepath.write_text(content)` → `await sftp.write_text_atomic(path, content)`
3. **The five status readers** (`read_horde_status`, `read_drops_status`, `read_world_status`,
   `read_supply_event_status`, `read_survivor_data`) — each does
   `filepath.exists()` + `filepath.read_text()`. Replace with `sftp.exists()` + `sftp.read_text()`.
   The Lua-table parser code stays exactly as-is.
4. **`read_world_status_with_age`** (lines 470–495) — replace `filepath.stat().st_mtime` with
   `sftp.stat(path)[1]`. Note: the comment there already explains this is the *correct* skew-immune
   signal — SFTP `stat` returns the server's mtime, so keep using mtime.
5. **`reset_player_survivor`** (lines 621–678) — its `filepath.write_text(...)` → `write_text_atomic`.
6. **`mkd​ir` removal** — there is no remote `mkdir`; the `Lua/` folder exists because the mod
   writes status files into it.

Everything else — `_escape_lua_string`, `_build_lua_table`, all the `write_command`/`write_chat`/
`broadcast`/`playsound`/`rank_push`/`horde*`/`airdrop*` wrappers — stays byte-identical.

### 3.2 `player_tracker.py` — SFTP tail + add deaths

- `_find_latest_user_log` (lines 125–131): replace `sorted(log_dir.glob("*_user.txt"), reverse=True)`
  with `await sftp.newest_matching(logs_dir, "*_user.txt")`.
- The tail loop `_tail_user_log` (lines 165–242): replace the `os.path.getsize` + `open/seek/read`
  block with `text, self._file_pos = await sftp.tail(log_file, self._file_pos)`. Keep the rotation
  logic (size shrink / filename change → reset offset to 0).
- Keep the `_ATTEMPTING_RE` / `_CONNECTED_RE` regexes and the SQLite `players` table unchanged.
- **Welcome broadcast** (line 159): keep `lua_bridge.write_command("display", ...)` (now SFTP-backed).
  Optional simpler alt: RCON `servermsg`. Keep the "display" path — the Integration mod is already
  required for the world dashboard, so nothing extra is needed.
- **ADD deaths**: in the tail loop, add a third regex for the death line once you confirm the format
  (see §6 of the review). On match → `upsert_player`, post a death embed to `DISCORD_CHANNEL_ID`, and
  optionally broadcast in-game.

### 3.3 `chat_relay.py` — SFTP tail

- `_find_latest_chat_log` (lines 124–139): → `await sftp.newest_matching(logs_dir, "*chat*.txt")`.
- `_tail_chat_log` (lines 141–213): replace `getsize` + `open/seek/read` with `sftp.tail(...)`.
- The Discord→game side (`on_message`, line 257, `lua_bridge.chat_relay(...)`) needs **no change**
  (already via `lua_bridge`, now SFTP).

### 3.4 `rank_sync.py` — SFTP write of the ranks file

- `_get_ranks_file_path` (lines 89–106): drop the `Path(ini).parent.parent / "Lua"` + `mkdir`;
  use `SFTP_LUA_DIR / "jeeves_ranks.lua"` (remote path).
- `_write_ranks_file` (lines 110–135): replace `path.write_text(content)` with
  `await sftp.write_text_atomic(path, content)`. **Note:** this method is currently synchronous —
  make it `async` (it's awaited via `_update_rank_and_push`/`_startup_sync`; those are already async).
- `_push_ranks_to_server` (line 140): `lua_bridge.rank_push()` — no change.

### 3.5 `server_status.py` — one field to drop

- No file I/O of its own (reads `lua_bridge.read_world_status/read_horde_status` — now SFTP).
- **Drop the "Restart" field**: `build_embed` calls `_next_restart_str(skip_active)` (line 162) and
  uses `RESTART_HOURS_UTC`. Since the bot no longer restarts, either remove that field from the embed,
  or repurpose it to show Indifferent Broccoli's own restart schedule (display-only). Remove
  `skip_next_restart` handling accordingly.

### 3.6 `Jeeves.py` (main) — rewrite to a slim remote bot

Keep verbatim: `Config` (minus dropped keys), `ServerState`, `RCONHelper`, `Emojis`,
`poll_players`, `send_notification`/`get_notification_channel`, `require_role`, `_respond`,
`_send_error`, `on_app_command_error`, the single-instance lock (bot-local, safe).

Delete: `ModChecker`, `_tasklist`, `_taskkill`, `start_server`, `stop_server`, `restart_server`,
`discover_server_pid`, `monitor_until_online`, `handle_mod_updates`, `_mod_restart_sequence`,
`_restart_countdown`, `_immediate_restart`, `cancel_restart_task`, and the commands
`/start` `/restart` `/update` `/mod` `/cleanmods` `/skip` `/unskip` `/postpone`.

Change `/stop` (line 1317) to: `await bot.rcon.send_command("quit")` (drop the `_taskkill` calls).

`setup_hook` extension list — **the master switch**:
```python
# OLD (line 608):
# ('auto_restart','mod_check_timer','player_tracker','rank_sync','chat_relay',
#  'horde_events','jeeves_drops','jeeves_modsorter','jeeves_modmanager',
#  'server_update','server_status','horde_leaderboard')

# NEW:
for ext in ('player_tracker','rank_sync','chat_relay','horde_events',
            'jeeves_drops','jeeves_modsorter','jeeves_modmanager',
            'server_status','horde_leaderboard'):
    ...
```
(Removed: `auto_restart`, `mod_check_timer`, `server_update`.)

### 3.7 Event / leaderboard modules — ZERO changes

`horde_events.py`, `jeeves_drops.py`, `horde_leaderboard.py` do no file I/O of their own — their
docstrings literally say "The lua_bridge module handles all file I/O." Re-pointing `lua_bridge`
fixes all three. Leave them untouched.

### 3.8 Mod list/sort modules — SFTP + strip SteamCMD

- `jeeves_modsorter.py` + `mod_sorter.py`: replace `ini_path.read_text` / `read_text`/`rglob` with
  `sftp.read_text` / `sftp.list_dir`. Pure sort logic, no SteamCMD. **Retain.**
- `jeeves_modmanager.py`: keep `/modlist`, `/modreorder` (and their `read_text`/`write_text` ini
  helpers → SFTP). **Delete** `/modadd`, `/modremove`, and `_download_workshop_item` (all SteamCMD).
  The `_get_mod_names`/`_get_map_names` helpers that `rglob` the workshop folder for `mod.info` →
  re-point to `sftp.list_dir` + `sftp.read_text`.

---

## 4. Customization Checklist for PZ Tambayan

| What | Where | Change to |
|---|---|---|
| **Dashboard title** | `server_status.py` `set_author(name="SERVER STATUS")` | `"PZ TAMBAYAN"` |
| **Dashboard icon/banner** | `server_status.py` `ICON_URL` / `IMAGE_URL` | Your own Discord-hosted PNGs |
| **Leaderboard icon** | `horde_leaderboard.py` `ICON_URL` | Your own |
| **Welcome message** | `player_tracker.py` `_delayed_rcon_broadcast` | e.g. `"Welcome to PZ Tambayan, {name}! "` |
| **Welcome-back** | same function | `"Welcome back, {name}!"` (customize) |
| **Death message (new)** | new code in `player_tracker.py` | e.g. `"☠️ {name} has died. Rest in pieces."` |
| **Notification channel** | `DISCORD_CHANNEL_ID` | Tambayan's announcements channel ID |
| **Status dashboard channel** | `STATUS_CHANNEL_ID` | dedicated channel ID |
| **Chat relay channel** | `CHAT_RELAY_CHANNEL_ID` | chat channel ID |
| **Leaderboard channel** | `HORDE_LEADERBOARD_CHANNEL_ID` | channel ID |
| **Admin role** | `DEFAULT_ROLE` | Tambayan's admin role name |
| **Rank role names** | `config` `RANK_1..RANK_6` + `rank_sync.py` `ROLE_TO_RANK` | Tambayan's 6 rank role names (if different from Fuel/Spark/…/Inferno) |
| **Rank chat colours** | `chat_relay.py` `ANSI_COLORS` | match your 6 tiers |
| **Custom emoji** | `config` `EMOJI_*` | your server's emoji (`<:name:id>`) |
| **Chat channels to relay** | `chat_relay.py` `RELAY_CHAT_TYPES = {'General'}` | add `'Faction'`, `'Whisper'`, etc. if wanted |
| **Death-line regex** | new in `player_tracker.py` | from your live `Zomboid/Logs/` sample |

---

## 5. Build Order (recommended)

1. **Phase 0** — confirm SFTP host/user/auth + **write** permission on `Zomboid/Lua/`, remote
   `Zomboid/` path, which Jeeves mods are installed, and grab a raw `Logs/` listing + one death line.
2. **Phase 1** — `sftp_client.py` + new `config.env` + slim `main.py` (RCON/state/notifications).
3. **Phase 2** — re-point `lua_bridge.py` (keystone). This alone re-enables chat relay, rank sync,
   horde events, drops, playsound, and the world dashboard.
4. **Phase 3** — swap the tails in `player_tracker.py` + `chat_relay.py`, and `rank_sync.py` write.
5. **Phase 4** — add death tracking; wire customization values; systemd unit; README.
