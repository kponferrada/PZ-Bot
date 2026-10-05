# CLAUDE.md — maintainer guide for PZ-Bot

Read this before changing the bot. User-facing setup, commands, config and the
release process are in [`README.md`](README.md). Do not repeat them here: update
the README instead. Open issues and decisions are tracked in the Hermes vault
(`D:\Dev\main\hermes-agent\secondary-brain\projects\pz-bot\`), not in this repo.

## Repo rules

- Branches: work on **`develop`**. `main` takes merges for releases; tags are `vX.Y.Z` (README → Releases).
- Commit locally as `Keym <keym@localhost>` with conventional messages (`feat(scope): …`, `fix(scope): …`, `docs: …`). **Never push or tag unless the user asks.** The live VPS deploys from GitHub.
- Secrets: `bot/config.env`, `bot/config live.env`, `bot/config test.env` exist locally and are gitignored. Do not read, print or commit them. Change config keys in `bot/config.env.example`.
- Runtime state files in `bot/` (`players.db`, `feature_state.json`, `deaths.json`, `whitelist_requests.csv`, `mod_update_state.json`, `dashboard_assets.json`, `status_card.png`, `jeeves.lock`) are gitignored. `rank_links.json` is the exception: it is tracked even though it is runtime data.
- Keep one user doc (README.md) and this file. Don't add planning or review docs to the repo; put them in the vault.

## Verify a change

Run from `bot/` (install test dependencies with `pip install -r requirements-dev.txt`):

```bash
python -m py_compile *.py scripts/*.py tests/*.py
python -m pytest tests
```

Tests cover the pure functions plus offline renders and command metadata (`tests/`). Add tests for anything new that can run without Discord or SFTP.

The bot can't run without real Discord, RCON and SFTP credentials. Behaviour is
checked on the **test instance** (a second Discord bot that runs `develop`). Say
which checks you ran and which you could not.

## Architecture

- `main.py` is the script entry point (`python main.py`, so the module is `__main__`). It holds `Config` (all env keys), `ServerState`, `RCONHelper`, `PZBot`, the up/down monitor and the core admin commands. It loads every other module as a cog in `setup_hook`. **A new cog must be added to that extension tuple.**
- **Never `import main` from a cog.** That runs main.py a second time and builds a second Config and bot. Reach shared things through `bot` (`bot.config`, `bot.state`, `bot.rcon`, `bot.features`, `bot.get_*_channel()`). Shared helpers go in their own module (for example `checks.require_role`).
- `sftp_client.py` is the only path to server files. It is a module singleton (`sftp_client.get()`). It reconnects lazily, every operation has a 30 s timeout, and a timeout drops the connection so the next call reconnects. It raises `SftpError`, and callers catch that.
- `lua_bridge.py` owns the mod files in `Lua/`:
  - bot → mod commands: `jeeves_commands.txt`, `jeeves_chat.txt`, `siege_night_commands.txt`. These are Lua tables (`return { command=…, id=…, … }`) built by `_build_lua_table`. Non-ASCII text is written as `\ddd` byte escapes because the game misreads multibyte text.
  - Handshake: before writing, the bridge waits up to 5 s for the mod to **empty** the previous file (the mod "deletes" a file by truncating it). Each file has its own lock and an id counter that resets when the bot restarts.
  - mod → bot status files: `jeeves_world_status.txt`, `jeeves_drops_status.txt`, `jeeves_supply_event_status.txt`, `siege_night_status.txt` (nested tables, parsed by `_parse_lua_nested`).
  - Files are `.txt` because Build 42.20 limits the extensions `getFileWriter` accepts. `rank_sync` writes `jeeves_ranks.lua` directly.
- Tailed files (2 s loops): `Logs/*_user.txt` (player_tracker, newest file), `Logs/*_chat.txt` (chat_relay), `Lua/player-death-logging.log` (death_log), `Lua/JamiesFortune_JackpotLog.txt` (jamies_fortune). On startup the tailers skip existing content, so events that happen while the bot is down are lost. When the logs rotate (new server session) the new file is read from its start. Tailers keep a trailing partial line in a buffer, and `sftp.tail` never splits a UTF-8 character. player_tracker and chat_relay re-list `Logs/` every 10 s (`_RESCAN_SECONDS`) and only `stat` the known file in between; `newest_matching` is a single `readdir`.
- Data sources: Aegis Panel's `Lua/Aegis/Player/stats.txt` is the source of truth for kills, deaths and playtime (`aegis_stats`, cached briefly). The local `players.db` (SQLite) only records sessions and who is "known". It is seeded from Aegis, falling back to the server's `db/pzserver.db`. `deaths.json` keeps per-death timestamps for the today/this-week counts, using Philippine time (UTC+8).
- Server online detection uses three signals. `monitor_server_state` (15 s) probes RCON for the banners and stores the result (`ServerState.recent_rcon_probe`), which the dashboard and the periodic restart checks reuse instead of opening their own connection; starting a restart still probes fresh. `poll_players` prefers the fresh bridge status file (≤ 90 s old; `lua_bridge._mtime_age` measures from when the bot saw the mtime change, on its own monotonic clock, so VPS/host clock skew can't fool it) over RCON `players`. The dashboard waits for 3 RCON failures and also counts the world file fresh within 120 s (same mtime age) or a siege file whose own timestamp is within 120 s as online.
- Restarts (`restart_watch.py`): `ServerState.expect_restart()` is the lock (it expires after 15 min). `_start_restart` claims it **before** its first `await` so two triggers can't race. It returns False when a restart is already running or RCON is dead. A failed countdown releases the lock. A deferral cancels the countdown and suppresses the triggers, then fires again when it ends if a restart was pending. When `restart_shutdown_started` is True, the up/down monitor treats the next outage as part of the restart, not a blip.
- Ranks (`rank_sync`): `RANK_SOURCE=bt_ladder` (default) ranks every ladder player from `bt_progression.ladder_ranks` on a 5-min loop and ignores role changes; `RANK_LADDER_ROLES` additionally sets linked members' rank roles. `RANK_SOURCE=roles` is the old role → rank flow. Read ranks through `RankSync`, not the roles, so both modes work. Rank names, colours, emoji and ANSI codes live in one table, `ranks.RANKS` (tested against the live server's `RankColor_n` palette in `tests/test_ranks.py`).
- Feature toggles: every notification is gated by `bot.features.is_enabled("<key>")`. When you add a key, add it to `features.FEATURES`. A renamed key goes into `_LEGACY_KEY_MAP` so stored "disabled" state carries over. Most toggles only silence messages; the work behind them (tailing, restart actions) keeps running. `mod_updates`, `scheduled_restarts` and `status_dashboard` stop the work itself.
- Permissions: cog admin commands use `@checks.admin_only()` (reads `DEFAULT_ROLE` at run time; failures go to the global error handler in `main.py`). `main.py` uses `checks.require_role`. `whitelist.py` still has its own `_is_admin` checks (left as is on purpose). When you add or move a command, update the `/help` table in `help.py` and the README command list. `[admin]` in help must match the real check (`tests/test_help.py` enforces this for every cog except whitelist).
- Text that comes from players (chat relay) is sent with `AllowedMentions.none()`; the bot's default also blocks @everyone/@here.
- Discord interactions must be answered within 3 s. Anything that touches SFTP, RCON or the bridge (whose writes can wait 5 s) must `defer()` first and reply with `followup.send`.
- RCON calls: `rcon.send_command` is async. `rcon.is_server_online` is a **blocking** socket call, so call it as `await asyncio.to_thread(...)`.
- Images are made with Pillow: `status_card.py`, `death_card.py`, `player_banner.py` (it inpaints the name into the template art; the backgrounds are cached once per kind). The art and fonts are in `bot/assets/`. `scripts/` holds one-off tools for cutting banner art.
- `death_card.py` types onto `assets/death-certificate.png`, a blank form that `scripts/build_death_certificate.py` makes from the filled-in source art. If you re-run the script or swap the art, re-check the field coordinates (`_FIELDS`, `_PHOTO_BOX`, doll anchors `_FRONT`/`_BACK`). The portrait is the avatar of the Discord user linked with `/linkme` (`RankSync.discord_id_for_pz_username`); unlinked players get a "no photo" placeholder. The left (certificate) side is the real-world record (linked Discord username, IRL time in PHT); the right (autopsy) side is the in-game record (PZ username, in-game time). Rendering runs in `asyncio.to_thread`.

## External contracts

The bot is one side of file protocols owned by other repos in
`D:\Dev\main\projectzomboid-modding\`. Change both sides together.

| Contract | Other side |
|---|---|
| `siege_night_status.txt` / `siege_night_commands.txt` | `siege-night-bridge` (vault: *siege-night-bridge File Protocol*) |
| `player-death-logging.log` block format | `player-death-logging-b42` (vault: *player-death-logging-b42 Log Format*) |
| `jeeves_*` files | Jeeve's Integration / Jeeve's Drops (Workshop, not local) |
| `Aegis/Player/stats.txt` | Aegis Panel (Workshop, not local) |
| `JamiesFortune_JackpotLog.txt` | Jamie's Fortune (Workshop, not local) |
| `BarangayTales/progression.json` (read by `bt_progression.py` → `/rpleaderboard`, and → `rank_sync` ranks via `ladder_ranks` when `RANK_SOURCE=bt_ladder`; the ladder's `player` id is used as the PZ username). `DEFAULT_WEEKLY_TITLES` copies BT `Config.WeeklyRanking.titles` | `barangaytales` `JsonExport.lua`, `WeeklyRanking.lua`, `Config.lua` |
| Not the bot's job: event wins are recorded inside Barangay Tales (decided 2026-10-05). The bot does not write `bt_event_wins.txt`. | `barangaytales` |

## Known traps

- The host's SFTP server rejects rename, so `write_text` writes directly. Don't reintroduce tmp+rename.
- When a player dies, PZ logs a leave and then a join. player_tracker holds a leave for 15 s (`_RESPAWN_WINDOW`) and drops both if the join follows.
- B42 logs animals as players. player_tracker reports deaths only for known players.
- `WORKSHOP_UPDATE_CHANNEL_ID` / `_ROLE_ID` are loaded but unused.
