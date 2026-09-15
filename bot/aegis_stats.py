"""aegis_stats.py — read player stats from Aegis Panel's ledger.

Aegis Panel (Steam Workshop 3766508989, mod id "AP") is the server's
authoritative player-stats source. It writes a pipe-delimited ledger to
`Lua/Aegis/Player/stats.txt` on the server:

    S|user|deaths|zkills|bandits|pvp|distM|bestHours|bestKills|totalHours|playedMin

This module reads that file over SFTP and caches it briefly, so the bot reports
kills / deaths / playtime straight from Aegis instead of keeping its own
duplicate counters.

Config (see config.env.example):
  AEGIS_STATS_PATH — full SFTP path to the ledger.
                     Default: {SFTP_LUA_DIR}/Aegis/Player/stats.txt
"""

from __future__ import annotations

import time
from typing import Optional

import sftp_client

# Field order of the stat columns that follow "S|user|" in each line.
_FIELDS = ("deaths", "zkills", "bandits", "pvp", "distM",
           "bestHours", "bestKills", "totalHours", "playedMin")

# How long a cached read is considered fresh (seconds). Aegis flushes its
# ledger at most once a minute, so this matches its own write cadence.
_CACHE_TTL = 60.0

_cache: dict = {"data": None, "at": 0.0}


def default_path(bot) -> str:
    lua = getattr(bot.config, "SFTP_LUA_DIR", "") or ""
    return f"{lua.rstrip('/')}/Aegis/Player/stats.txt" if lua else ""


def _path(bot) -> str:
    return (getattr(bot.config, "AEGIS_STATS_PATH", "") or "").strip() or default_path(bot)


def _num(s: str) -> float:
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def parse(text: str) -> dict:
    """Parse an Aegis stats.txt ledger into {username: {field: value}}."""
    out: dict = {}
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) < 3 or parts[0] != "S":
            continue
        user = parts[1].strip()
        if not user:
            continue
        row: dict = {}
        for i, field in enumerate(_FIELDS):
            raw = parts[i + 2] if i + 2 < len(parts) else ""
            if field in ("bestHours", "totalHours"):
                row[field] = _num(raw)          # keep one-decimal precision
            else:
                row[field] = int(_num(raw))      # whole-number counters
        out[user] = row
    return out


async def _refresh(bot, force: bool = False) -> dict:
    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["at"] < _CACHE_TTL:
        return _cache["data"]
    path = _path(bot)
    if not path:
        return _cache["data"] or {}
    try:
        text = await sftp_client.get().read_text(path)
    except sftp_client.SftpError as e:
        print(f"[AegisStats] read failed ({path}): {e}")
        return _cache["data"] or {}
    data = parse(text)
    _cache["data"] = data
    _cache["at"] = now
    return data


async def get_all(bot, force: bool = False) -> dict:
    """Return the full {username: stats} map."""
    return await _refresh(bot, force)


async def get(bot, username: str, force: bool = False) -> Optional[dict]:
    """Return one player's stats dict, or None if they have no ledger row."""
    data = await _refresh(bot, force)
    return data.get(username)


async def get_field(bot, username: str, field: str, force: bool = False):
    """Return a single stat for a player (0 when absent)."""
    data = await _refresh(bot, force)
    row = data.get(username) or {}
    return row.get(field, 0)


async def known_players(bot, force: bool = False) -> set:
    """Every username Aegis has a ledger row for (i.e. has played)."""
    data = await _refresh(bot, force)
    return set(data.keys())


async def top(bot, field: str, n: int = 10, force: bool = False) -> list:
    """Top `n` players by `field`, as a list of (username, value)."""
    data = await _refresh(bot, force)
    items = [(u, r.get(field, 0)) for u, r in data.items()]
    items.sort(key=lambda t: (-t[1], t[0].lower()))
    return items[:n]
