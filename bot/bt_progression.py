"""bt_progression.py — read the Barangay Tales progression export.

Barangay Tales (server mod) writes its full progression state as JSON to
`Lua/BarangayTales/progression.json` (`JsonExport.lua`), refreshed at most every
5 minutes and at least every 10. This module reads it over SFTP, caches it
briefly, and shapes the parts the bot shows.

Weekly Survivor Ranking (BT `WeeklyRanking.lua`):
  - Weekly Reputation Points (RP) from wealth, zombie kills, quests and event
    wins; the week runs Monday 00:00 -> Sunday 23:59:59 in GMT+8.
  - When a week ends, places 1-5 get a weekly title for the following week.
  - `weeklyRanking` in the export holds `weekId`, `leaderboard` (every active
    player, ranked), `top5`, and `history` ({weekId: {top5: [...]}}, where each
    finalized row carries the `title` actually granted).

The export has no rank -> title table, so the current week's titles come from
`DEFAULT_WEEKLY_TITLES`, a copy of BT's `Config.WeeklyRanking.titles`. If a
future export adds `weeklyRanking.titles` ([{rank, title}]), that wins.

Config (see config.env.example):
  BT_PROGRESSION_PATH — full SFTP path to the export.
                        Default: {SFTP_LUA_DIR}/BarangayTales/progression.json
"""

from __future__ import annotations

import json
import time
from typing import Optional

import sftp_client

# Copy of Barangay Tales Config.WeeklyRanking.titles (BT 0.71.0). Update both
# together. Index 0 = 1st place.
DEFAULT_WEEKLY_TITLES = (
    "Legendary Survivor",
    "Elite Survivor",
    "Master Survivor",
    "Veteran Survivor",
    "Rising Survivor",
)

# Picker-id prefix of a weekly title in a player's `titles` list ("weekly:<name>").
_WEEKLY_TITLE_PREFIX = "weekly:"

_CATEGORIES = ("wealth", "kills", "quests", "events")

# The export changes every 5-10 minutes; a minute of caching is plenty.
_CACHE_TTL = 60.0

_cache: dict = {"data": None, "at": 0.0}


def default_path(bot) -> str:
    lua = getattr(bot.config, "SFTP_LUA_DIR", "") or ""
    return f"{lua.rstrip('/')}/BarangayTales/progression.json" if lua else ""


def _path(bot) -> str:
    return (getattr(bot.config, "BT_PROGRESSION_PATH", "") or "").strip() or default_path(bot)


async def read_progression(bot, force: bool = False) -> Optional[dict]:
    """Return the parsed export, or None if it can't be read (last good copy if any)."""
    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["at"] < _CACHE_TTL:
        return _cache["data"]
    path = _path(bot)
    if not path:
        return _cache["data"]
    try:
        text = await sftp_client.get().read_text(path)
        data = json.loads(text)
    except sftp_client.SftpError as e:
        print(f"[BTProgression] read failed ({path}): {e}")
        return _cache["data"]
    except ValueError as e:
        # A read that races the mod's write can see a half-written file.
        print(f"[BTProgression] bad JSON in {path}: {e}")
        return _cache["data"]
    if not isinstance(data, dict):
        return _cache["data"]
    _cache["data"] = data
    _cache["at"] = now
    return data


# ---- shaping (pure: no I/O, unit-tested) ------------------------------------

def _as_list(value) -> list:
    """BT's Lua JSON encoder writes an empty array as `{}`; accept either."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        # Rare: a sparse Lua array becomes {"1": ..., "2": ...}.
        try:
            return [value[k] for k in sorted(value, key=int)]
        except (TypeError, ValueError):
            return []
    return []


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def title_table(data: dict) -> list:
    """Weekly title per place as [title for 1st, 2nd, ...]."""
    exported = _as_list((data.get("weeklyRanking") or {}).get("titles"))
    by_rank = {}
    for t in exported:
        if isinstance(t, dict) and t.get("title") and _int(t.get("rank")) > 0:
            by_rank[_int(t["rank"])] = str(t["title"])
    if by_rank:
        return [by_rank.get(r) for r in range(1, max(by_rank) + 1)]
    return list(DEFAULT_WEEKLY_TITLES)


def title_for_rank(titles: list, rank: int) -> Optional[str]:
    return titles[rank - 1] if 1 <= rank <= len(titles) else None


def held_weekly_title(player: dict) -> Optional[str]:
    """The weekly title a player holds right now (won last week), or None."""
    for t in _as_list(player.get("titles")):
        tid = t.get("id") if isinstance(t, dict) else None
        if isinstance(tid, str) and tid.startswith(_WEEKLY_TITLE_PREFIX):
            return tid[len(_WEEKLY_TITLE_PREFIX):]
    return None


def weekly_leaderboard(data: dict, limit: Optional[int] = None) -> dict:
    """Shape the weekly RP leaderboard with the title each place earns.

    Returns:
      {
        "weekId": "2026-W40" | None,
        "generatedAt": epoch seconds (0 if unknown),
        "titles": ["Legendary Survivor", ...],         # place 1..N
        "rows": [{rank, player, displayName, rp, categories{wealth,kills,quests,events},
                  title,          # title this place earns if the week ended now (top 5)
                  heldTitle}],    # weekly title the player holds now (from last week)
        "lastWeek": {"weekId", "rows": [{rank, player, displayName, rp, title}]} | None,
      }
    """
    data = data or {}
    wr = data.get("weeklyRanking") or {}
    players = data.get("players") if isinstance(data.get("players"), dict) else {}
    titles = title_table(data)

    rows = []
    for r in _as_list(wr.get("leaderboard")):
        if not isinstance(r, dict) or not r.get("player"):
            continue
        pid = str(r["player"])
        rank = _int(r.get("rank")) or len(rows) + 1
        cats = r.get("categories") if isinstance(r.get("categories"), dict) else {}
        rows.append({
            "rank": rank,
            "player": pid,
            "displayName": str(r.get("displayName") or pid),
            "rp": _int(r.get("rp")),
            "categories": {c: _int(cats.get(c)) for c in _CATEGORIES},
            "title": title_for_rank(titles, rank),
            "heldTitle": held_weekly_title(players.get(pid) or {}),
        })
    rows.sort(key=lambda row: row["rank"])
    if limit is not None:
        rows = rows[:max(0, limit)]

    last_week = None
    history = wr.get("history") if isinstance(wr.get("history"), dict) else {}
    # Week ids are "YYYY-Wnn", so string order is time order.
    past = sorted(w for w in history if w != wr.get("weekId"))
    if past:
        wid = past[-1]
        lw_rows = []
        for r in _as_list((history.get(wid) or {}).get("top5")):
            if not isinstance(r, dict) or not r.get("player"):
                continue
            pid = str(r["player"])
            rank = _int(r.get("rank"))
            lw_rows.append({
                "rank": rank,
                "player": pid,
                "displayName": str((players.get(pid) or {}).get("displayName") or pid),
                "rp": _int(r.get("rp")),
                # The title actually granted; fall back to the table for old rows.
                "title": r.get("title") or title_for_rank(titles, rank),
            })
        lw_rows.sort(key=lambda row: row["rank"])
        last_week = {"weekId": wid, "rows": lw_rows}

    return {
        "weekId": wr.get("weekId"),
        "generatedAt": _int(data.get("generatedAt")),
        "titles": titles,
        "rows": rows,
        "lastWeek": last_week,
    }


async def get_weekly_leaderboard(bot, limit: Optional[int] = None,
                                 force: bool = False) -> Optional[dict]:
    """Read the export and return `weekly_leaderboard(...)`, or None if unavailable."""
    data = await read_progression(bot, force)
    if data is None:
        return None
    return weekly_leaderboard(data, limit)
