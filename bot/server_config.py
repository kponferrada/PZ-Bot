"""server_config.py — read Project Zomboid server settings (the .ini) over SFTP.

The server's real config lives in <SFTP_ZOMBOID_ROOT>/Server/<name>.ini. This
module reads it (explicit path or auto-detect) and exposes helpers for settings
the bot needs dynamically — currently `MaxPlayers`.

Shared with jeeves_modmanager so the INI-read logic lives in one place.
"""

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
