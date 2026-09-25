# PZ Tambayan Discord Bot — Jeeves Review & Remote-Refactor Plan

> **Note:** This is a historical planning document from the original refactor
> (September 2026). It records the review *plan* and the state of the code at
> that time. For the **current** feature set, commands, and setup, see
> [`bot/README.md`](bot/README.md) and [`SETUP.md`](SETUP.md).

**Project:** Project Zomboid Discord bot for the PZ Tambayan server
**Source reviewed:** `StewBagger/Jeeves` (JeevesBot v42, cloned 2026-09-10)
**Constraint:** Game server is hosted on **Indifferent Broccoli**; the bot runs on a **separate VPS**, reading/writing server files over **SFTP** and issuing commands over **RCON**.
**Author:** Draconica / The Orchestrator (AgentOS)

---

## 1. Executive Summary

JeevesBot is a capable but **same-box-coupled** server manager: its backbone is *local
process control* (it spawns and kills the game server, inspects the process table, and runs
SteamCMD) plus a *file bridge* to the Jeeves mods (it reads/writes small files in
`Zomboid/Lua/`).

The critical distinction for a remote refactor:

- **Process control + SteamCMD are the ONLY truly local-only parts.** Nothing over
  RCON or SFTP can launch a stopped server process, and SteamCMD must run on the host.
- **The file bridge is fully remote-viable.** The Jeeves mods cannot tell — and do not
  care — whether the bytes in `Zomboid/Lua/` arrived from a local process or over SFTP.
  Every feature built on the bridge (chat relay, rank sync, horde/drop events, sound
  alerts, world/horde/drop status) works unchanged from a separate VPS, given SFTP
  **read+write** access and the corresponding mod installed.

So the refactor is a *narrowing* only where it must be: delete process-start and SteamCMD,
keep everything bridge-based over SFTP, keep RCON. The retained/optional feature split is
now a **product decision** (what Keym wants), not a technical limit.

---

## 2. Module Inventory & Remote Verdict

| Module | Purpose | Same-server dependency | Remote? |
|---|---|---|---|
| `Jeeves.py` (1624 ln) | Main bot: config, RCON, state, process lifecycle, 43 slash cmds | `subprocess` launch/kill, process table, SteamCMD | **REWRITE → slim remote bot** |
| `player_tracker.py` | Welcome / welcome-back from `*_user.txt` + SQLite DB | Tails local `USER_LOG_PATH` | ✅ SFTP read (tail) |
| `server_status.py` | Auto-updating dashboard embed | Reads `lua_bridge` status files + RCON | ✅ SFTP read + RCON |
| `lua_bridge.py` | File bridge to Jeeves mods (write cmds, read status) | Local `Zomboid/Lua/` | ✅ SFTP read+write |
| `chat_relay.py` | Bidirectional Discord↔game chat | Tail `CHAT_LOG_PATH` (read) + write `jeeves_chat.txt` | ✅ SFTP read (tail) + SFTP write — needs Integration mod |
| `rank_sync.py` | Discord roles → in-game name colours | Write `jeeves_ranks.lua` + `rankpush` cmd | ✅ SFTP write — needs Integration mod |
| `auto_restart.py` | Scheduled UTC restarts + countdown | Process restart | ⚠️ **partial** — stop works via RCON `quit`; start does not |
| `mod_check_timer.py` | Heartbeat, crash detection, mod-update restart | Process table (detect) + restart (recover) | ⚠️ detect ✅ (mod status file), recover ❌ |
| `server_update.py` | `/update` via SteamCMD | Local SteamCMD | ❌ **local-only** |
| `jeeves_modmanager.py` | Mod add/remove/reorder | SteamCMD (download) + `.ini` edit | ⚠️ reorder ✅ (SFTP ini edit), download/remove ❌ |
| `jeeves_modsorter.py` / `mod_sorter.py` | Load-order sort | Reads/writes local `.ini` | ✅ SFTP ini read+write (apply needs restart) |
| `workshop_acf.py` | Read local `appworkshop` manifest | Local workshop folder | ❌ local-only (or SFTP read if perms allow) |
| `horde_events.py` | Horde event control | Write command files + read status | ✅ SFTP write + read — needs Hordes mod |
| `horde_leaderboard.py` | Horde survivor leaderboard | Read survivor status file | ✅ SFTP read — needs Hordes mod |
| `jeeves_drops.py` | Airdrop / supply event control | Write command files + read status | ✅ SFTP write + read — needs Drops mod |

---

## 3. The Real Boundary: What Is Truly Local-Only?

After reviewing every mechanism, the local-only list is short:

| Capability | Mechanism | Remote via SFTP/RCON? |
|---|---|---|
| **Start the server process** | `subprocess.Popen(SERVER_BATCH)` | ❌ No remote equivalent — RCON can't start a stopped process. Needs host panel or a tiny on-host agent. |
| **SteamCMD** (`/update`, mod download/remove) | local binary | ❌ Must run on the host |
| **Stop the server** | RCON `quit` (graceful) | ✅ Fully remote |
| **Restart** | stop (✅) + start (❌) | ⚠️ Stop is remote; start is not. Needs host panel/auto-restart. |
| **Crash detection** | process table (❌) **or** mod status-file freshness (✅) | ✅ via `jeeves_world_status.txt` mtime — the *better* signal (only `OnTick` writes it) |
| **Crash recovery** | auto-restart | ❌ same as "start" |
| **File bridge** (chat/rank/horde/drops/sounds/status) | read/write `Zomboid/Lua/` files | ✅ Fully remote over SFTP — mods are transport-agnostic |

**Bottom line:** only **"start the process"** and **"SteamCMD"** are genuinely same-box-bound.
Everything else — including chat relay, rank sync, horde/drop events, and even graceful
stop — is remote-viable over **RCON + SFTP (read+write)**, gated on two things: (a) SFTP
write access to `Zomboid/Lua/`, and (b) the corresponding Jeeves mods installed.

---

## 4. Retain / Remove / Optional Matrix (vs. requested features)

| Requested feature | Jeeves source | Disposition |
|---|---|---|
| **Player monitoring** | `poll_players()` (RCON `players` + world-status `playerCount`), `/players`, `/playerlist` | **Keep** — RCON + SFTP read |
| **Player-state dashboard** | `server_status.py` embed | **Keep** — SFTP world-status read + RCON liveness |
| **Welcome notes** | `player_tracker.py` | **Keep** — SFTP tail + RCON `servermsg` |
| **Player deaths** | *(absent from Jeeves)* | **ADD** — SFTP tail a deaths source (see §6) |
| **World-state dashboard** | `server_status.py` + `lua_bridge.read_world_status` | **Keep** — needs Jeeve's Integration mod |

**Optional (remote-viable, gated on SFTP write + mods) — your call:**
- **Chat relay** — needs Jeeve's Integration; SFTP tail (game→Discord) + SFTP write (Discord→game)
- **Rank sync** — needs Jeeve's Integration; SFTP write `jeeves_ranks.lua` + `rankpush`
- **Horde events + leaderboard** — needs Jeeve's Hordes; SFTP write commands + read status
- **Drops / supply events** — needs Jeeve's Drops; SFTP write commands + read status
- **`/playsound`** — needs Jeeve's Integration; SFTP write command
- **Mod reorder / `modlist` / `modinfo`** — SFTP read+write of the `.ini` (apply needs restart)
- **Graceful `/stop`** — RCON `quit` (no local process kill needed)

**Removed (genuinely local-only, no remote path):**
- **`/start`** and any auto-start/recovery — cannot launch a stopped process remotely
- **`/restart`** / scheduled auto-restart — start half is impossible remotely
- **`/update`** and mod download/remove — SteamCMD must run on the host

---

## 5. Target Architecture

```
                        ┌───────────────────────────┐
                        │   Indifferent Broccoli    │
                        │   (Project Zomboid host)  │
                        │                           │
                        │  PZ server  ── RCON ──┐   │
                        │  Zomboid/Lua/  ◄─SFTP─┼─┐ │   (read + write)
                        │  Zomboid/Logs/ ◄─SFTP─┼─┼─┐  (read)
                        └───────────────────────┼─┼─┼───┐
                                                 │ │ │   │
                        ┌────────────────────────┼─┼─┼───┼───────┐
                        │   Bot VPS              │ │ │   │       │
                        │  ┌──────────────────┐  │ │ │   │       │
                        │  │  RCONHelper      │◄─┘ │ │   │       │
                        │  │  (players,msg,   │    │ │   │       │
                        │  │   quit,save)     │    │ │   │       │
                        │  ├──────────────────┤    │ │   │       │
                        │  │  SftpClient      │◄───┘ │   │       │
                        │  │  read/tail/stat  │      │   │       │
                        │  │  write/rename    │◄─────┘   │       │
                        │  ├──────────────────┤          │       │
                        │  │  world-status    │◄─────────┘       │
                        │  │  log-tail (user) │                  │
                        │  │  deaths          │◄───── (new)      │
                        │  │  bridge cmds     │ (optional)       │
                        │  └──────────────────┘                  │
                        │   ── Discord gateway ──────────────────┼──► Discord
                        └────────────────────────────────────────┘

  Data plane:   SFTP read+write  (world/status files, log tail, bridge command files)
  Command plane: RCON ONLY       (players, servermsg, save, quit)
```

### SFTP layer (new, ~150 lines)
- Library: **`asyncssh`** (async-native, fits the Discord.py event loop). Fallback:
  `paramiko` wrapped in `asyncio.to_thread`.
- Single reconnecting client exposing:
  - `read_text(path) -> str`
  - `stat(path) -> (size, mtime)`
  - `tail(path, offset) -> (bytes, new_offset)`
  - `newest_matching(glob) -> path` (via `listdir` + `stat`)
  - `write_text_atomic(path, content)` — **write to `path.tmp`, then `rename`** so the mod
    never reads a half-written command file (rename is atomic over SFTP; the original local
    code writes directly and relies on tiny file size, which is not safe to assume remotely).
- Reuse `lua_bridge`'s Lua-table parser and `player_tracker`'s regexes verbatim; only the
  filesystem calls move behind `SftpClient`.

### Config surface (remote `config.env`)
```dotenv
# Discord
DISCORD_TOKEN=...
DISCORD_GUILD_ID=...
DISCORD_CHANNEL_ID=...        # notifications (join/death/welcome)
STATUS_CHANNEL_ID=...         # auto-updating world/player dashboard

# RCON (unchanged from Jeeves)
RCON_HOST=<server host or ip>
RCON_PORT=27015
RCON_PASSWORD=...

# SFTP (NEW)
SFTP_HOST=<server host or ip>
SFTP_PORT=22
SFTP_USER=...
SFTP_PASSWORD=...             # or SFTP_KEY_PATH=/path/to/key
SFTP_LUA_PATH=/home/pz/Zomboid/Lua      # read+write (optional features)
SFTP_LOGS_PATH=/home/pz/Zomboid/Logs    # read (tail)
SFTP_WRITE_ENABLED=true       # gate: turn off to stay read-only
```

---

## 6. Player-Death Tracking — Gap Analysis

Jeeves **does not track deaths**. `player_tracker.py` handles only `attempting to join` and
`fully connected`; a grep for `death|died|dead` returns only incidental "process died"
strings. This feature must be added.

Death-event sources, in order of preference:

1. **Tiny server-side Lua mod (recommended).** Mirror the existing
   `jeeves_world_status.txt` pattern: a small mod writes `jeeves_deaths.txt` on
   `OnCharacterDeath`/`OnPlayerDeath` with `{ name, timestamp, cause }`. The bot tails it
   over SFTP exactly like `player_tracker` tails `*_user.txt`.
2. **Parse the vanilla server log.** B42 writes connection activity to `*_connections.txt`
   (and `*_user.txt` for join/leave/disconnect), but the death line's exact file and format
   vary by build — verify against the live `Zomboid/Logs/` before hardcoding.
3. **Jeeve's Journals mod**, if installed, may expose a deaths file; unverified.

**Action item:** pull a raw `Zomboid/Logs/` listing (and a death occurrence) from the live
Tambayan server, confirm the exact death line, then wire the regex. The SFTP-tail
scaffolding is source-agnostic, so this can proceed independently.

---

## 7. Deliberate Simplifications (and their trade-offs)

| Jeeves behaviour | Remote simplification | Trade-off |
|---|---|---|
| Welcome via Lua `display` cmd | Welcome via RCON `servermsg` | Red banner vs chat-panel text. *If SFTP write is enabled, the Lua `display` path can be kept instead.* |
| Player roster prefers mod `playerCount` | Prefer RCON `players`; SFTP world-status secondary | RCON prose parsing is the fallback Jeeves deprecated; fine for a monitor |
| Horde row in dashboard | Read `jeeves_horde_status.txt` if present | Only populates with Jeeve's Hordes installed |
| Crash detection / auto-restart | RCON + status-file liveness only; **no restart** | Restart needs the host panel |
| Mod update → restart | Removed | Mod management stays on the host |

---

## 8. Phased Implementation Plan

**Phase 0 — Confirm access (blocking).** From Indifferent Broccoli: SFTP host/port/user/
auth, whether the SFTP account has **write** access to `Zomboid/Lua/`, the remote
`Zomboid/` path, and which Jeeves mods are installed. Grab a raw `Logs/` listing + one
death line.

**Phase 1 — Skeleton.** `requirements.txt` (`discord.py`, `python-dotenv`, `rcon`,
`asyncssh`). `config.env.example` per §5. `sftp_client.py` (reconnecting client: read /
stat / tail / newest_matching / write_atomic). Port `RCONHelper` + `ServerState`.

**Phase 2 — Core features (read-only + RCON).** `player_tracker.py` (SFTP tail + SQLite),
`server_status.py` (SFTP world-status + RCON), death tailing, and the
`/players` `/playerlist` `/online` `/hello` commands.

**Phase 3 — Optional bridge features (requires SFTP write + mods).** Chat relay, rank
sync, `/playsound`, horde/drop events + leaderboard — all ported behind `SftpClient.write_text_atomic`.

**Phase 4 — Hardening.** Reconnect/backoff, stale-data grace, log-rotation over SFTP,
systemd unit, README.

---

## 9. Risks / Open Questions

1. **SFTP write permission + path layout** on Indifferent Broccoli — confirm in Phase 0
   (this gates every optional bridge feature).
2. **Which Jeeves mods are installed** — Integration (chat/rank/sound), Hordes, Drops.
   Features are inert without their mod.
3. **Death-log format** — must be confirmed live (see §6).
4. **Clock skew** — world-status freshness uses the file *mtime* (server clock), so SFTP
   `stat().st_mtime` is the correct, skew-immune reading.
5. **No remote start** — the bot can detect and report a down server but cannot start or
   restart it; recovery stays a host-panel action unless a panel API becomes available.
