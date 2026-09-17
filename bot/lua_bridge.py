"""lua_bridge.py — SFTP-backed file bridge for the PZ Tambayan bot.

All file I/O goes through `sftp_client` so the bot can run on a VPS separate
from the game server. The Jeeves mods read/write these files in `Zomboid/Lua/`;
they cannot tell whether the bytes arrived from a local process or over SFTP.

Files:
  bot -> mod (commands) : jeeves_commands.txt, jeeves_chat.txt, siege_night_commands.txt
  mod -> bot (status)   : jeeves_world_status.txt, jeeves_drops_status.txt,
                          jeeves_supply_event_status.txt, siege_night_status.txt

> These are `.txt`, not `.lua` — Build 42.20 restricted which extensions
> `getFileWriter` accepts. Requires the current Workshop versions of the Jeeves mods.
"""

import os
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

COMMAND_FILE = "jeeves_commands.txt"
CHAT_FILE = "jeeves_chat.txt"
DROPS_STATUS_FILE = "jeeves_drops_status.txt"
WORLD_STATUS_FILE = "jeeves_world_status.txt"
SUPPLY_EVENT_STATUS_FILE = "jeeves_supply_event_status.txt"
SIEGE_STATUS_FILE = "siege_night_status.txt"
SIEGE_COMMAND_FILE = "siege_night_commands.txt"


def init(bot) -> str:
    """Initialize the bridge from bot.config.SFTP_LUA_DIR (a remote path)."""
    global _lua_dir
    _lua_dir = getattr(bot.config, "SFTP_LUA_DIR", None) or os.getenv("SFTP_LUA_DIR")
    if not _lua_dir:
        _lua_dir = "Lua"
        print("[LuaBridge] WARNING: SFTP_LUA_DIR not set, using remote 'Lua' (relative)")
    # Normalize to a POSIX remote path (forward slashes) regardless of the bot host OS.
    _lua_dir = str(PurePosixPath(_lua_dir))
    print(f"[LuaBridge] Initialized — remote Lua dir: {_lua_dir}")
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


def _build_lua_table(command: str, cmd_id: int, **kwargs) -> str:
    lines = [
        "return {",
        f'    command = "{_escape_lua_string(command)}",',
        f"    id = {cmd_id},",
        f"    timestamp = {int(time.time())},",
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
        sftp = sftp_client.get()
        # Wait for the mod to consume any previous command file. The PZ mod
        # "deletes" by overwriting with empty content (no os.remove), so an empty
        # or missing file means "consumed". Mod polls ~every 0.5-2s.
        for _ in range(10):
            st = await sftp.stat(remote_path)
            if st is None or st[0] == 0:
                break
            await asyncio.sleep(0.5)
        else:
            print(f"[LuaBridge] WARNING: previous command not consumed after 5s, overwriting for {label}")

        try:
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
    return await write_command("broadcast", message=message, soundOnly=sound_only)


async def chat_relay(author: str, message: str) -> bool:
    return await write_chat(author, message)


async def playsound(sound_id: int, message: str | None = None) -> bool:
    return await write_command("playsound", sound=sound_id, message=message or "")


async def rank_push() -> bool:
    return await write_command("rankpush")


async def write_siege_command(command: str, **kwargs) -> bool:
    if _lua_dir is None:
        print("[LuaBridge] ERROR: Not initialized! Call lua_bridge.init(bot) first.")
        return False
    global _siege_command_id
    _siege_command_id += 1
    content = _build_lua_table(command, _siege_command_id, **kwargs)
    return await _write_file(_path(SIEGE_COMMAND_FILE), _siege_write_lock, content,
                             f"siege command '{command}' (id={_siege_command_id})")


async def siege_start() -> bool:
    return await write_siege_command("siegestart")


async def siege_stop() -> bool:
    return await write_siege_command("siegestop")


async def siege_schedule(day: int) -> bool:
    return await write_siege_command("siegeschedule", targetDay=day)


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
    """Parse a flat Lua table of the form `return { key = value, ... }` into a dict."""
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
    """Parse a Lua double-quoted string (with \\n \\r \\t \\\" \\\\ \\ddd escapes)."""
    j = i + 1
    out = []
    while j < len(s):
        c = s[j]
        if c == "\\":
            nxt = s[j + 1] if j + 1 < len(s) else ""
            if nxt == "n":
                out.append("\n"); j += 2; continue
            if nxt == "r":
                out.append("\r"); j += 2; continue
            if nxt == "t":
                out.append("\t"); j += 2; continue
            if nxt == '"':
                out.append('"'); j += 2; continue
            if nxt == "\\":
                out.append("\\"); j += 2; continue
            if nxt.isdigit():
                num = 0
                k = j + 1
                while k < len(s) and k < j + 4 and s[k].isdigit():
                    num = num * 10 + int(s[k]); k += 1
                out.append(chr(num & 0xFF)); j = k; continue
            out.append(nxt); j += 2; continue
        if c == '"':
            return "".join(out), j + 1
        out.append(c); j += 1
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


async def _read_status(filename: str) -> dict | None:
    if _lua_dir is None:
        return None
    sftp = sftp_client.get()
    path = _path(filename)
    if not await sftp.exists(path):
        return None
    try:
        text = (await sftp.read_text(path)).strip()
        if not text:
            return None
        return _parse_lua_table(text)
    except Exception as exc:
        print(f"[LuaBridge] Failed to read {filename}: {exc}")
        return None


async def _read_status_nested(filename: str) -> dict | None:
    if _lua_dir is None:
        return None
    sftp = sftp_client.get()
    path = _path(filename)
    if not await sftp.exists(path):
        return None
    try:
        text = (await sftp.read_text(path)).strip()
        if not text:
            return None
        return _parse_lua_nested(text)
    except Exception as exc:
        print(f"[LuaBridge] Failed to read {filename}: {exc}")
        return None


async def read_siege_status() -> dict | None:
    return await _read_status_nested(SIEGE_STATUS_FILE)


async def read_drops_status() -> dict | None:
    return await _read_status(DROPS_STATUS_FILE)


async def read_world_status() -> dict | None:
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
    path = _path(WORLD_STATUS_FILE)
    st = await sftp.stat(path)
    if st is None:
        return None, None
    age = max(0.0, time.time() - st[1])
    return await read_world_status(), age
