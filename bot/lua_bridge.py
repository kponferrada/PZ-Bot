"""lua_bridge.py — SFTP-backed file bridge for the PZ Tambayan bot.

All file I/O goes through `sftp_client` so the bot can run on a VPS separate
from the game server. The Jeeves mods read/write these files in `Zomboid/Lua/`;
they cannot tell whether the bytes arrived from a local process or over SFTP.

Files:
  bot -> mod (commands) : jeeves_commands.txt, jeeves_chat.txt, siege_night_commands.txt,
                          blib2_discord_commands.txt, blib2_discord_chat.txt, blib2_discord_ranks.txt
  mod -> bot (status)   : jeeves_world_status.txt, jeeves_drops_status.txt, blib2_discord_status.txt,
                          jeeves_supply_event_status.txt, siege_night_status.txt,
                          JamiesFortune_JackpotLog.txt (tailed by jamies_fortune),
                          JamiesCollapse_discord.log (tailed by jamies_collapse)

BLib2 Admin's Discord bridge (blib2-admin `Discord.lua`, protocol 2) replaces
Jeeve's Integration: with GAME_BRIDGE=auto (default) chat, broadcasts, sounds,
ranks and the world status go to BLib2 while its status file is fresh, else to
the jeeves_* files; GAME_BRIDGE=blib2 / jeeves pins one.

> These are `.txt`, not `.lua` — Build 42.20 restricted which extensions
> `getFileWriter` accepts. Requires the current Workshop versions of the Jeeves mods.
"""

import os
import re
import time
import asyncio
from pathlib import PurePosixPath

import sftp_client

# ---- module-level state -------------------------------------------------------

_lua_dir: str | None = None   # remote path, e.g. /home/pz/Zomboid/Lua
_command_id: int = 0
_chat_id: int = 0
_siege_command_id: int = 0
_write_lock = asyncio.Lock()
_chat_lock = asyncio.Lock()
_siege_write_lock = asyncio.Lock()
_discord_command_id: int = 0
_discord_chat_id: int = 0
_discord_lock = asyncio.Lock()
_discord_chat_lock = asyncio.Lock()
_mode: str = "auto"            # GAME_BRIDGE: auto | blib2 | jeeves

COMMAND_FILE = "jeeves_commands.txt"
CHAT_FILE = "jeeves_chat.txt"
DROPS_STATUS_FILE = "jeeves_drops_status.txt"
WORLD_STATUS_FILE = "jeeves_world_status.txt"
SUPPLY_EVENT_STATUS_FILE = "jeeves_supply_event_status.txt"
SIEGE_STATUS_FILE = "siege_night_status.txt"
SIEGE_COMMAND_FILE = "siege_night_commands.txt"
JAMIES_JACKPOT_FILE = "JamiesFortune_JackpotLog.txt"
JAMIES_COLLAPSE_FILE = "JamiesCollapse_discord.log"
DISCORD_STATUS_FILE = "blib2_discord_status.txt"
DISCORD_COMMAND_FILE = "blib2_discord_commands.txt"
DISCORD_CHAT_FILE = "blib2_discord_chat.txt"
DISCORD_RANKS_FILE = "blib2_discord_ranks.txt"


def init(bot) -> str:
    """Initialize the bridge from bot.config.SFTP_LUA_DIR (a remote path)."""
    global _lua_dir, _mode
    _mode = str(getattr(bot.config, "GAME_BRIDGE", "auto") or "auto").lower()
    if _mode not in ("auto", "blib2", "jeeves"):
        print(f"[LuaBridge] WARNING: GAME_BRIDGE={_mode!r} unknown, using auto")
        _mode = "auto"
    _lua_dir = getattr(bot.config, "SFTP_LUA_DIR", None) or os.getenv("SFTP_LUA_DIR")
    if not _lua_dir:
        _lua_dir = "Lua"
        print("[LuaBridge] WARNING: SFTP_LUA_DIR not set, using remote 'Lua' (relative)")
    # Normalize to a POSIX remote path (forward slashes) regardless of the bot host OS.
    _lua_dir = str(PurePosixPath(_lua_dir))
    print(f"[LuaBridge] Initialized — remote Lua dir: {_lua_dir}, game bridge: {_mode}")
    return _lua_dir


def get_lua_dir() -> str | None:
    return _lua_dir


def _path(name: str) -> str:
    return f"{_lua_dir}/{name}"


# ---- Lua string escaping (kept byte-identical to upstream) --------------------

def _escape_lua_string(s: str) -> str:
    """Escape a Python string for safe embedding in a Lua string literal.

    Non-ASCII is emitted as Lua decimal byte escapes (\\ddd) of its UTF-8 bytes,
    so the file handed to the game is pure ASCII. This sidesteps the engine bug
    where the mod re-reads the file with the JVM platform default charset and
    mangles multi-byte characters (Cyrillic, CJK, emoji).
    """
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif " " <= ch <= "~":
            out.append(ch)
        else:
            out.extend("\\%d" % b for b in ch.encode("utf-8"))
    return "".join(out)


def _build_lua_table(command: str, cmd_id: int, timestamp: int | None = None, **kwargs) -> str:
    lines = [
        "return {",
        f'    command = "{_escape_lua_string(command)}",',
        f"    id = {cmd_id},",
        f"    timestamp = {int(time.time()) if timestamp is None else int(timestamp)},",
    ]
    for key, value in kwargs.items():
        if value is None:
            continue
        if isinstance(value, bool):
            lines.append(f"    {key} = {str(value).lower()},")
        elif isinstance(value, (int, float)):
            lines.append(f"    {key} = {value},")
        elif isinstance(value, str):
            lines.append(f'    {key} = "{_escape_lua_string(value)}",')
        else:
            lines.append(f'    {key} = "{_escape_lua_string(str(value))}",')
    lines.append("}")
    return "\n".join(lines)


# ---- write helpers (SFTP) -----------------------------------------------------

async def _write_file(remote_path: str, lock: asyncio.Lock, content: str, label: str) -> bool:
    async with lock:
        try:
            sftp = sftp_client.get()
            # Wait for the mod to consume any previous command file. The PZ mod
            # "deletes" by overwriting with empty content (no os.remove), so an
            # empty or missing file means "consumed". Mod polls ~every 0.5-2s.
            for _ in range(10):
                st = await sftp.stat(remote_path)
                if st is None or st[0] == 0:
                    break
                await asyncio.sleep(0.5)
            else:
                print(f"[LuaBridge] WARNING: previous command not consumed after 5s, overwriting for {label}")

            await sftp.write_text(remote_path, content)
            print(f"[LuaBridge] Wrote {label}")
            await asyncio.sleep(0.6)
            return True
        except Exception as exc:
            print(f"[LuaBridge] Failed to write {label}: {exc}")
            return False


async def write_command(command: str, **kwargs) -> bool:
    if _lua_dir is None:
        print("[LuaBridge] ERROR: Not initialized! Call lua_bridge.init(bot) first.")
        return False
    global _command_id
    _command_id += 1
    content = _build_lua_table(command, _command_id, **kwargs)
    return await _write_file(_path(COMMAND_FILE), _write_lock, content,
                             f"command '{command}' (id={_command_id})")


async def write_chat(author: str, message: str) -> bool:
    if _lua_dir is None:
        print("[LuaBridge] ERROR: Not initialized! Call lua_bridge.init(bot) first.")
        return False
    global _chat_id
    _chat_id += 1
    content = _build_lua_table("chat", _chat_id, author=author, message=message)
    return await _write_file(_path(CHAT_FILE), _chat_lock, content,
                             f"chat relay '[{author}] {message}' (id={_chat_id})")


# ---- convenience wrappers -----------------------------------------------------

async def broadcast(message: str, sound_only: bool = False) -> bool:
    if await uses_blib2():
        if sound_only:
            # the RCON servermsg shows it; BLib2's client plays the countdown sound off that line itself
            return True
        return (await discord_command("broadcast", wait=0, message=message))["sent"]
    return await write_command("broadcast", message=message, soundOnly=sound_only)


async def chat_relay(author: str, message: str) -> bool:
    if await uses_blib2():
        return await write_discord_chat(author, message)
    return await write_chat(author, message)


async def playsound(sound_id: int, message: str | None = None) -> bool:
    if await uses_blib2():
        return (await discord_command("playsound", wait=0, sound=blib2_sound(sound_id),
                                      message=message or None))["sent"]
    return await write_command("playsound", sound=sound_id, message=message or "")


async def rank_push() -> bool:
    if await uses_blib2():
        return (await discord_command("ranks", wait=0))["sent"]
    return await write_command("rankpush")


# Siege Night commands go to BLib's bridge (blib-admin, BLib 0.8+), which
# answers each one in the status file's `bridge.results` (protocol 2). The old
# standalone siege-night-bridge mod reads the same file but never answers.
SIEGE_PROTOCOL = 2
SIEGE_RESULT_WAIT = 12.0      # seconds: one 2 s poll + the status write, with slack


def siege_bridge_info(status: dict | None) -> dict | None:
    """The status file's `bridge` section (BLib's bridge), or None (old mod / no file)."""
    bridge = (status or {}).get("bridge")
    return bridge if isinstance(bridge, dict) else None


def find_siege_result(status: dict | None, cmd_id: int, sent: int) -> dict | None:
    """The bridge's answer to our command, matched on id AND our timestamp.

    Ids restart at 1 when the bot restarts, so the id alone could match an
    answer to an older command; `sent` is the command's own timestamp echoed back.
    """
    bridge = siege_bridge_info(status)
    results = bridge.get("results") if bridge else None
    if not isinstance(results, dict):
        return None
    for key in sorted(results, key=lambda k: int(k[1:]) if k[1:].isdigit() else 0):
        r = results[key]
        if isinstance(r, dict) and r.get("id") == cmd_id and r.get("sent") == sent:
            return r
    return None


async def write_siege_command(command: str, by: str | None = None, by_id: str | None = None,
                              **kwargs) -> tuple[int, int] | None:
    """Write a Siege Night command; returns (id, timestamp) once written, else None."""
    if _lua_dir is None:
        print("[LuaBridge] ERROR: Not initialized! Call lua_bridge.init(bot) first.")
        return None
    global _siege_command_id
    _siege_command_id += 1
    cmd_id, sent = _siege_command_id, int(time.time())
    content = _build_lua_table(command, cmd_id, timestamp=sent, protocol=SIEGE_PROTOCOL,
                               by=(by or None) and by[:64], byId=by_id, **kwargs)
    ok = await _write_file(_path(SIEGE_COMMAND_FILE), _siege_write_lock, content,
                           f"siege command '{command}' (id={cmd_id})")
    return (cmd_id, sent) if ok else None


async def siege_command(command: str, by: str | None = None, by_id: str | None = None,
                        wait: float = SIEGE_RESULT_WAIT, **kwargs) -> dict:
    """Send a Siege Night command and wait for BLib's answer.

    Returns {"sent": bool, "result": dict | None, "bridge": dict | None}.
    `result` is None when nothing answered in time: the old bridge mod (it
    never answers), or the server is down / busy.
    """
    written = await write_siege_command(command, by=by, by_id=by_id, **kwargs)
    if not written:
        return {"sent": False, "result": None, "bridge": None}
    cmd_id, sent = written
    status = None
    deadline = time.monotonic() + wait
    while True:
        status = await read_siege_status()
        result = find_siege_result(status, cmd_id, sent)
        if result is not None or time.monotonic() >= deadline:
            return {"sent": True, "result": result, "bridge": siege_bridge_info(status)}
        await asyncio.sleep(1.5)


async def siege_start(by: str | None = None, by_id: str | None = None) -> dict:
    return await siege_command("siegestart", by=by, by_id=by_id)


async def siege_stop(by: str | None = None, by_id: str | None = None) -> dict:
    return await siege_command("siegestop", by=by, by_id=by_id)


async def siege_schedule(day: int, by: str | None = None, by_id: str | None = None) -> dict:
    return await siege_command("siegeschedule", by=by, by_id=by_id, targetDay=day)


async def airdrop(target_player: str | None = None, crate_type: str | None = None) -> bool:
    kwargs = {}
    if target_player:
        kwargs["targetPlayer"] = target_player
    if crate_type:
        kwargs["crateType"] = crate_type
    return await write_command("airdrop", **kwargs)


async def airdrop_status() -> bool:
    return await write_command("airdropstatus")


async def supply_event() -> bool:
    return await write_command("supplyevent")


async def supply_event_status() -> bool:
    return await write_command("supplyeventstatus")


# ---- Lua table parser (shared, deduplicated) ----------------------------------

def _parse_lua_table(text: str) -> dict | None:
    """Parse a flat Lua table of the form `return { key = value, ... }` into a dict.

    Uses the full parser (string escapes, UTF-8 byte escapes) and falls back
    to the simple line-based one if that finds nothing.
    """
    try:
        parsed = _parse_lua_nested(text)
    except Exception:
        parsed = None
    if parsed:
        return parsed
    return _parse_lua_table_lines(text)


def _parse_lua_table_lines(text: str) -> dict | None:
    """Line-based fallback for `_parse_lua_table` (one `key = value` per line)."""
    inner = text.strip()
    if inner.startswith("return"):
        inner = inner[6:].strip()
    if inner.startswith("{"):
        inner = inner[1:]
    if inner.endswith("}"):
        inner = inner[:-1]

    result = {}
    for line in inner.split("\n"):
        line = line.strip().rstrip(",")
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()

        if val.startswith('"') and val.endswith('"'):
            result[key] = val[1:-1]
        elif val == "true":
            result[key] = True
        elif val == "false":
            result[key] = False
        else:
            try:
                result[key] = int(val)
            except ValueError:
                try:
                    result[key] = float(val)
                except ValueError:
                    result[key] = val
    return result if result else None


# ---- nested Lua table parser (for the siege status file's schedule/siege sections) ----

def _parse_lua_nested(text: str) -> dict | None:
    """Parse a Lua `return { … }` table, INCLUDING nested tables, into a dict.

    Used for the siege status file, which now writes `schedule = { … }` and
    `siege = { … }` sections. Values may be strings, numbers, booleans, or
    nested tables.
    """
    s = text.strip()
    if s.startswith("return"):
        s = s[len("return"):].lstrip()
    value, _ = _parse_nested_value(s, 0)
    return value if isinstance(value, dict) else None


def _skip_lua_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _parse_nested_value(s: str, i: int):
    i = _skip_lua_ws(s, i)
    if i >= len(s):
        return None, i
    c = s[i]
    if c == "{":
        return _parse_nested_table(s, i)
    if c == '"':
        return _parse_lua_string(s, i)
    j = i
    while j < len(s) and s[j] not in " \t\r\n,}":
        j += 1
    token = s[i:j]
    if token == "true":
        return True, j
    if token == "false":
        return False, j
    if token == "nil":
        return None, j
    try:
        return int(token), j
    except ValueError:
        try:
            return float(token), j
        except ValueError:
            return token, j


def _parse_lua_string(s: str, i: int):
    """Parse a Lua double-quoted string (with \\n \\r \\t \\\" \\\\ \\ddd escapes).

    `\\ddd` escapes are bytes; a run of them is decoded as UTF-8, so a name
    the mod wrote as `\\195\\177` comes back as "ñ" rather than "Ã±".
    """
    j = i + 1
    out = []
    pending = bytearray()  # consecutive \ddd bytes, decoded together

    def _flush():
        if pending:
            out.append(pending.decode("utf-8", errors="replace"))
            pending.clear()

    while j < len(s):
        c = s[j]
        if c == "\\":
            nxt = s[j + 1] if j + 1 < len(s) else ""
            if nxt.isdigit():
                num = 0
                k = j + 1
                while k < len(s) and k < j + 4 and s[k].isdigit():
                    num = num * 10 + int(s[k]); k += 1
                pending.append(num & 0xFF); j = k; continue
            _flush()
            if nxt == "n":
                out.append("\n")
            elif nxt == "r":
                out.append("\r")
            elif nxt == "t":
                out.append("\t")
            else:
                out.append(nxt)
            j += 2; continue
        _flush()
        if c == '"':
            return "".join(out), j + 1
        out.append(c); j += 1
    _flush()
    return "".join(out), j


def _parse_nested_table(s: str, i: int):
    """Parse a `{ key = value, ... }` body (s[i] == '{') into a dict."""
    i += 1
    result = {}
    i = _skip_lua_ws(s, i)
    if i < len(s) and s[i] == "}":
        return result, i + 1
    while i < len(s):
        i = _skip_lua_ws(s, i)
        if i >= len(s):
            break
        if s[i] == "}":
            return result, i + 1
        if s[i] == '"':
            key, i = _parse_lua_string(s, i)
        else:
            j = i
            while j < len(s) and s[j] not in " \t\r\n=":
                j += 1
            key = s[i:j]
            i = j
        i = _skip_lua_ws(s, i)
        if i < len(s) and s[i] == "=":
            i += 1
            value, i = _parse_nested_value(s, i)
            result[key] = value
        i = _skip_lua_ws(s, i)
        if i < len(s) and s[i] == ",":
            i += 1
            continue
        if i < len(s) and s[i] == "}":
            return result, i + 1
    return result, i


async def _read_status_text(filename: str) -> str | None:
    """The status file's text, or None if it's missing/empty/unreadable.

    Reads directly instead of `exists()` + read: these files are polled every
    few seconds, and a missing file just fails the open.
    """
    if _lua_dir is None:
        return None
    try:
        text = (await sftp_client.get().read_text(_path(filename))).strip()
    except sftp_client.SftpError as exc:
        if not exc.not_found:
            print(f"[LuaBridge] Failed to read {filename}: {exc}")
        return None
    return text or None


async def _read_status(filename: str) -> dict | None:
    text = await _read_status_text(filename)
    return _parse_lua_table(text) if text else None


async def _read_status_nested(filename: str) -> dict | None:
    text = await _read_status_text(filename)
    try:
        return _parse_lua_nested(text) if text else None
    except Exception as exc:
        print(f"[LuaBridge] Failed to parse {filename}: {exc}")
        return None


async def read_siege_status() -> dict | None:
    return await _read_status_nested(SIEGE_STATUS_FILE)


async def read_drops_status() -> dict | None:
    return await _read_status(DROPS_STATUS_FILE)


async def read_world_status() -> dict | None:
    if await uses_blib2():
        status = await read_discord_status()
        world = (status or {}).get("world")
        return world if isinstance(world, dict) else None
    return await _read_status(WORLD_STATUS_FILE)


async def read_supply_event_status() -> dict | None:
    return await _read_status(SUPPLY_EVENT_STATUS_FILE)


async def read_world_status_with_age() -> tuple[dict | None, float | None]:
    """read_world_status(), plus how many seconds ago the file was written.

    Age is taken from the file's mtime (the game server's clock). This is the
    correct, skew-immune reading: the SFTP `stat` returns the server's own mtime,
    so a clock offset between VPS and host cannot make a fresh file look stale.
    """
    if _lua_dir is None:
        return None, None
    sftp = sftp_client.get()
    path = _path(DISCORD_STATUS_FILE if await uses_blib2() else WORLD_STATUS_FILE)
    st = await sftp.stat(path)
    if st is None:
        return None, None
    return await read_world_status(), _mtime_age(path, st[1])


# path -> (last mtime seen, local monotonic time it was first seen)
_mtime_seen: dict = {}


def _mtime_age(path: str, mtime: float, now_mono: float | None = None,
               now_wall: float | None = None) -> float:
    """Seconds since `path` was last written, immune to VPS/host clock skew.

    The mtime comes from the game host's clock, so `time.time() - mtime` is off
    by however far the two clocks disagree. Instead, note (on the bot's own
    monotonic clock) when the mtime last changed and measure from there. The
    first sighting has nothing to compare against, so it falls back to the
    wall-clock difference.
    """
    now_mono = time.monotonic() if now_mono is None else now_mono
    now_wall = time.time() if now_wall is None else now_wall
    prev = _mtime_seen.get(path)
    if prev is None:
        # First sighting: back-date the "seen" time by the wall-clock age.
        age = max(0.0, now_wall - mtime)
        _mtime_seen[path] = (mtime, now_mono - age)
        return age
    if mtime != prev[0]:
        _mtime_seen[path] = (mtime, now_mono)
        return 0.0
    return max(0.0, now_mono - prev[1])


# ---- BLib2 Admin's Discord bridge (protocol 2) ----------------------------------
# blib2-admin Discord.lua: channel "discord" (status + commands, answered in
# bridge.results like Siege Night) and "discordchat" (chat only, never waits).
# Sample: tests/fixtures/blib2_discord_status.txt (blib-v2 tools/dump_discord_status.lua).

DISCORD_FRESH_SECONDS = 120.0   # auto mode: BLib2 is "there" while its status file is this fresh
DISCORD_RESULT_WAIT = 12.0
BLIB2_SOUNDS = 6                # A.DISCORD_SOUNDS in blib2-admin Shared.lua
_blib2_seen: tuple[float, bool] | None = None   # (monotonic time checked, answer)
_SPOT_RE = re.compile(r"safe spot (-?\d+),(-?\d+),(-?\d+)")


def mode() -> str:
    return _mode


def blib2_sound(sound_id: int) -> int:
    """Jeeve's 11 sound ids onto BLib2's 6 (1-4 are the countdown alarms in both)."""
    n = int(sound_id)
    return n if 1 <= n <= BLIB2_SOUNDS else (n - 1) % BLIB2_SOUNDS + 1


async def uses_blib2() -> bool:
    """Send to BLib2's Discord bridge? GAME_BRIDGE pins it; auto asks whether
    BLib2's status file is fresh (checked at most every 30 s)."""
    global _blib2_seen
    if _mode != "auto":
        return _mode == "blib2"
    if _lua_dir is None:
        return False
    now = time.monotonic()
    if _blib2_seen is not None and now - _blib2_seen[0] < 30:
        return _blib2_seen[1]
    answer = False
    try:
        path = _path(DISCORD_STATUS_FILE)
        st = await sftp_client.get().stat(path)
        answer = st is not None and st[0] > 0 and _mtime_age(path, st[1]) <= DISCORD_FRESH_SECONDS
    except Exception as exc:
        print(f"[LuaBridge] BLib2 bridge check failed: {exc}")
    _blib2_seen = (now, answer)
    return answer


async def read_discord_status() -> dict | None:
    return await _read_status_nested(DISCORD_STATUS_FILE)


async def write_discord_command(command: str, by: str | None = None, by_id: str | None = None,
                                **kwargs) -> tuple[int, int] | None:
    """Write a command for BLib2's Discord bridge; (id, timestamp) once written, else None."""
    if _lua_dir is None:
        print("[LuaBridge] ERROR: Not initialized! Call lua_bridge.init(bot) first.")
        return None
    global _discord_command_id
    _discord_command_id += 1
    cmd_id, sent = _discord_command_id, int(time.time())
    content = _build_lua_table(command, cmd_id, timestamp=sent, protocol=SIEGE_PROTOCOL,
                               by=(by or None) and by[:64], byId=by_id, **kwargs)
    ok = await _write_file(_path(DISCORD_COMMAND_FILE), _discord_lock, content,
                           f"discord command '{command}' (id={cmd_id})")
    return (cmd_id, sent) if ok else None


async def discord_command(command: str, by: str | None = None, by_id: str | None = None,
                          wait: float = DISCORD_RESULT_WAIT, **kwargs) -> dict:
    """Send a command to BLib2's Discord bridge and (wait > 0) wait for its answer.

    Returns {"sent": bool, "result": dict | None, "status": dict | None}.
    `result` None: no answer in time (server down or busy, or blib2-admin off).
    """
    written = await write_discord_command(command, by=by, by_id=by_id, **kwargs)
    if not written:
        return {"sent": False, "result": None, "status": None}
    if wait <= 0:
        return {"sent": True, "result": None, "status": None}
    cmd_id, sent = written
    deadline = time.monotonic() + wait
    while True:
        status = await read_discord_status()
        result = find_siege_result(status, cmd_id, sent)
        if result is not None or time.monotonic() >= deadline:
            return {"sent": True, "result": result, "status": status}
        await asyncio.sleep(1.5)


async def write_discord_chat(author: str, message: str) -> bool:
    if _lua_dir is None:
        print("[LuaBridge] ERROR: Not initialized! Call lua_bridge.init(bot) first.")
        return False
    global _discord_chat_id
    _discord_chat_id += 1
    content = _build_lua_table("chat", _discord_chat_id, protocol=SIEGE_PROTOCOL,
                               author=(author or "Discord")[:64], message=message[:300])
    return await _write_file(_path(DISCORD_CHAT_FILE), _discord_chat_lock, content,
                             f"discord chat (id={_discord_chat_id})")


def ranks_file_text(entries: dict) -> str:
    """blib2_discord_ranks.txt: "username<TAB>rank<TAB>r,g,b" (0..1) per line.
    entries: username -> (rank, (r, g, b) 0..255)."""
    lines = ["# Written by PZ-Bot (rank_sync). username, rank, name colour r,g,b (0..1)."]
    for name in sorted(entries, key=str.lower):
        rank, rgb = entries[name]
        if not name or any(c in name for c in "\t\n|"):
            continue
        r, g, b = (max(0.0, min(1.0, c / 255.0)) for c in rgb)
        lines.append(f"{name}\t{int(rank)}\t{r:.3f},{g:.3f},{b:.3f}")
    return "\n".join(lines) + "\n"


async def write_discord_ranks(entries: dict) -> bool:
    if _lua_dir is None:
        return False
    try:
        await sftp_client.get().write_text(_path(DISCORD_RANKS_FILE), ranks_file_text(entries))
        return True
    except Exception as exc:
        print(f"[LuaBridge] Failed to write {DISCORD_RANKS_FILE}: {exc}")
        return False


def keyed(section: dict | None, prefix: str) -> list:
    """A status list keyed p1.. / i1.. (with `count`) as a Python list."""
    if not isinstance(section, dict):
        return []
    out = []
    i = 1
    while f"{prefix}{i}" in section:
        out.append(section[f"{prefix}{i}"])
        i += 1
    return out


def unstuck_spot(message: str | None) -> tuple[int, int, int] | None:
    """The (x, y, z) in an `unstuck` answer ("safe spot 101,199,0 for Ann")."""
    m = _SPOT_RE.search(message or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
