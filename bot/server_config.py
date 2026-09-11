"""server_config.py — read Project Zomboid server settings (the .ini) over SFTP.

The server's real config lives in <SFTP_ZOMBOID_ROOT>/Server/<name>.ini. This
module reads it (explicit path or auto-detect) and exposes helpers for settings
the bot needs dynamically — currently `MaxPlayers`.

Shared with jeeves_modmanager so the INI-read logic lives in one place.
"""

import sqlite3
import time

import sftp_client


def _server_dir(bot) -> str | None:
    """Derive the remote Server/ folder (sibling of Lua/)."""
    root = getattr(bot.config, "SFTP_ZOMBOID_ROOT", None)
    if root:
        return f"{root.rstrip('/')}/Server"
    lua = getattr(bot.config, "SFTP_LUA_DIR", None)
    if lua:
        parent = "/".join(lua.rstrip('/').split('/')[:-1])
        return f"{parent}/Server"
    return None


async def read_ini(bot) -> str | None:
    """Return the server .ini contents over SFTP, or None if unavailable."""
    sftp = sftp_client.get()
    # 1. Explicit path, if configured and present.
    ini = getattr(bot.config, "SFTP_SERVER_INI", None)
    if ini and await sftp.exists(ini):
        return await sftp.read_text(ini)
    # 2. Auto-detect: the main server ini is the .ini file in Server/.
    server_dir = _server_dir(bot)
    if server_dir:
        try:
            for name in await sftp.list_dir(server_dir):
                if name.endswith(".ini"):
                    path = f"{server_dir.rstrip('/')}/{name}"
                    return await sftp.read_text(path)
        except sftp_client.SftpError:
            pass
    return None


def ini_value(text: str, key: str) -> list[str]:
    """Parse a `key=value` line from the INI into a list of values."""
    for line in text.splitlines():
        if line.strip().startswith(f"{key}="):
            raw = line.split("=", 1)[1].strip()
            values = [v.strip() for v in raw.split(";") if v.strip()]
            if key == "Mods":
                values = [v.lstrip("\\") for v in values]  # b42 backslash-prefixed IDs
            return values
    return []


# --- MaxPlayers (cached) ----------------------------------------------------

_max_players_cache = {"value": None, "ts": 0.0}
_MAX_PLAYERS_TTL = 300  # seconds; MaxPlayers only changes on a server restart


async def read_max_players(bot, fallback: int = 32) -> int:
    """Read MaxPlayers from the server .ini (cached ~5 min), falling back if needed."""
    now = time.time()
    cached = _max_players_cache["value"]
    if cached is not None and (now - _max_players_cache["ts"]) < _MAX_PLAYERS_TTL:
        return cached

    value = fallback
    try:
        text = await read_ini(bot)
        if text:
            vals = ini_value(text, "MaxPlayers")
            if vals:
                value = int(vals[0])
                print(f"[ServerConfig] MaxPlayers = {value} (from server INI)")
    except (ValueError, sftp_client.SftpError) as e:
        print(f"[ServerConfig] Could not read MaxPlayers, using fallback {fallback}: {e}")

    _max_players_cache["value"] = value
    _max_players_cache["ts"] = now
    return value

# --- Server name (cached) ---------------------------------------------------

_server_name_cache = {"value": None, "ts": 0.0}


async def read_server_name(bot, fallback: str = "PZ TAMBAYAN") -> str:
    """Read the server's public name from the .ini (cached ~5 min)."""
    now = time.time()
    cached = _server_name_cache["value"]
    if cached is not None and (now - _server_name_cache["ts"]) < _MAX_PLAYERS_TTL:
        return cached

    value = fallback
    try:
        text = await read_ini(bot)
        if text:
            for key in ("PublicName", "Name", "ServerName"):
                vals = ini_value(text, key)
                if vals and vals[0]:
                    value = vals[0].strip('"').strip("'")
                    print(f"[ServerConfig] Server name = {value!r} (from server INI)")
                    break
    except sftp_client.SftpError as e:
        print(f"[ServerConfig] Could not read server name, using fallback {fallback!r}: {e}")

    _server_name_cache["value"] = value
    _server_name_cache["ts"] = now
    return value


# --- Known players (from the server's own player DB) -------------------------

def _extract_usernames(data: bytes) -> set:
    """Extract username-like values from a PZ SQLite DB, schema-agnostic."""
    found = set()
    try:
        conn = sqlite3.connect(":memory:")
        conn.deserialize(data)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    except Exception:
        return found
    for tbl in tables:
        try:
            cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{tbl}")').fetchall()]
        except sqlite3.OperationalError:
            continue
        user_cols = [c for c in cols if c.lower() in ("username", "name", "user", "user_name", "playername", "player")]
        for col in user_cols:
            try:
                for (v,) in conn.execute(f'SELECT "{col}" FROM "{tbl}"').fetchall():
                    v = str(v).strip()
                    if v:
                        found.add(v)
            except sqlite3.OperationalError:
                pass
    conn.close()
    return found


_known_players_cache = {"value": None, "ts": 0.0}
_KNOWN_PLAYERS_TTL = 300  # seconds


async def read_server_players(bot, force: bool = False) -> set:
    """Return the set of known player names from the server's player DB (cached).

    Reads SFTP_SERVER_DB (default <root>/db/pzserver.db) and falls back to the
    vanilla PZ world-player DBs (<root>/Saves/Multiplayer/<world>/players.db).
    """
    now = time.time()
    cached = _known_players_cache["value"]
    if cached is not None and not force and (now - _known_players_cache["ts"]) < _KNOWN_PLAYERS_TTL:
        return cached

    known = set()
    sftp = sftp_client.get()
    root = getattr(bot.config, "SFTP_ZOMBOID_ROOT", None) or "/server-data"
    db = getattr(bot.config, "SFTP_SERVER_DB", "") or f"{root.rstrip('/')}/db/pzserver.db"
    candidates = [db]
    try:
        mp = f"{root.rstrip('/')}/Saves/Multiplayer"
        for name in await sftp.list_dir(mp):
            candidates.append(f"{mp}/{name}/players.db")
    except sftp_client.SftpError:
        pass
    for pdb in candidates:
        try:
            if not await sftp.exists(pdb):
                continue
            data = await sftp.read_bytes(pdb)
        except sftp_client.SftpError as e:
            print(f"[ServerConfig] Cannot read {pdb}: {e}")
            continue
        found = _extract_usernames(data)
        if found:
            known |= found
    _known_players_cache["value"] = known
    _known_players_cache["ts"] = now
    return known

