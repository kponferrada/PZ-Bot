"""bt_progression.py — read the Barangay Tales progression export.

Barangay Tales (server mod) writes its full progression state as JSON
(`JsonExport.lua`), refreshed at most every 5 minutes and at least every 10.
Since BT 0.83 every BT file goes through BLib.Files (scope "server"), so the
export is `Lua/BLib/mods/barangaytales/progression.json`; BT 0.82 and older
wrote `Lua/BarangayTales/progression.json`. This module reads it over SFTP,
caches it briefly, and shapes the parts the bot shows.

Weekly Survivor Ranking (BT `WeeklyRanking.lua`):
  - Weekly Reputation Points (RP) from wealth, zombie kills, quests and event
    wins; the week runs Monday 00:00 -> Sunday 23:59:59 in GMT+8.
  - When a week ends, places 1-5 get a weekly title for the following week,
    and so do the top 5 factions by weekly Faction Reputation (members wear
    it; BT `FactionWeekly.lua`).
  - `weeklyRanking` in the export holds `weekId`, `leaderboard` (every active
    player, ranked), `top5`, and `history` ({weekId: {top5, factionTop5,
    finalizedAt}}, where each finalized row carries the `title` granted).
  - A player's `titles` list holds `weekly:<title>` while their weekly title
    lasts and `fweekly:<title>` while their faction holds one. The id keeps the
    configured name even when the holder renames it.
  - The current faction standings are each faction's `weeklyRep`.

The export has no rank -> title table, so titles come from
`DEFAULT_WEEKLY_TITLES` / `DEFAULT_FACTION_TITLES`, copies of BT's
`Config.WeeklyRanking.titles` / `.factionTitles`. If a future export adds
`weeklyRanking.titles` / `.factionTitles` ([{rank, title}]), that wins.

Fire ranks (rank sync, leaderboard badges) follow BT's `Notebook.fireTier`:
the current weekly title holders (last finalized week's top 5 who still hold
the title) are Inferno..Spark for places 1..5; anyone else with RP this week
is Fuel.

Config (see config.env.example):
  BT_PROGRESSION_PATH — full SFTP path to the export. Default: the BLib path,
                        falling back to the pre-0.83 path until the BLib one
                        has been read once.
"""

from __future__ import annotations

import json
import time
from typing import Optional

import sftp_client

# Copy of Barangay Tales Config.WeeklyRanking.titles / .factionTitles
# (BT 0.83.0). Update both together. Index 0 = 1st place.
DEFAULT_WEEKLY_TITLES = (
    "Legendary Survivor",
    "Elite Survivor",
    "Master Survivor",
    "Veteran Survivor",
    "Rising Survivor",
)
DEFAULT_FACTION_TITLES = (
    "Legendary Faction",
    "Elite Faction",
    "Master Faction",
    "Veteran Faction",
    "Rising Faction",
)

# Title-id prefixes in a player's `titles` list (BT WeeklyRanking.TITLE_PREFIX,
# FactionWeekly.PREFIX).
_WEEKLY_TITLE_PREFIX = "weekly:"
_FACTION_TITLE_PREFIX = "fweekly:"

# Places that hold a weekly title (BT WeeklyRanking.TITLE_COUNT).
TITLE_COUNT = 5

_CATEGORIES = ("wealth", "kills", "quests", "events")

# The export changes every 5-10 minutes; a minute of caching is plenty.
_CACHE_TTL = 60.0

# `blib_seen`: the BLib path has been read once, so the pre-0.83 file (which
# stays on disk, stale, after the upgrade) is never used again.
_cache: dict = {"data": None, "at": 0.0, "blib_seen": False}

# Under SFTP_LUA_DIR: BT 0.83+ (BLib.Files, scope "server") and BT 0.82 and older.
BLIB_EXPORT = "BLib/mods/barangaytales/progression.json"
LEGACY_EXPORT = "BarangayTales/progression.json"


def default_path(bot) -> str:
    lua = getattr(bot.config, "SFTP_LUA_DIR", "") or ""
    return f"{lua.rstrip('/')}/{BLIB_EXPORT}" if lua else ""


def legacy_path(bot) -> str:
    lua = getattr(bot.config, "SFTP_LUA_DIR", "") or ""
    return f"{lua.rstrip('/')}/{LEGACY_EXPORT}" if lua else ""


def _paths(bot) -> list:
    """Paths to try, in order."""
    override = (getattr(bot.config, "BT_PROGRESSION_PATH", "") or "").strip()
    if override:
        return [override]
    paths = [default_path(bot)]
    if not _cache["blib_seen"]:
        paths.append(legacy_path(bot))
    return [p for p in paths if p]


async def _read_json(path: str) -> Optional[dict]:
    try:
        text = await sftp_client.get().read_text(path)
        data = json.loads(text)
    except sftp_client.SftpError as e:
        print(f"[BTProgression] read failed ({path}): {e}")
        return None
    except ValueError as e:
        # A read that races the mod's write can see a half-written file.
        print(f"[BTProgression] bad JSON in {path}: {e}")
        return None
    return data if isinstance(data, dict) else None


async def read_progression(bot, force: bool = False) -> Optional[dict]:
    """Return the parsed export, or None if it can't be read (last good copy if any)."""
    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["at"] < _CACHE_TTL:
        return _cache["data"]
    paths = _paths(bot)
    for i, path in enumerate(paths):
        data = await _read_json(path)
        if data is None:
            continue
        if i == 0 and path == default_path(bot):
            _cache["blib_seen"] = True
        _cache["data"] = data
        _cache["at"] = now
        return data
    return _cache["data"]


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


def _exported_titles(data: dict, key: str, default: tuple) -> list:
    exported = _as_list((data.get("weeklyRanking") or {}).get(key))
    by_rank = {}
    for t in exported:
        if isinstance(t, dict) and t.get("title") and _int(t.get("rank")) > 0:
            by_rank[_int(t["rank"])] = str(t["title"])
    if by_rank:
        return [by_rank.get(r) for r in range(1, max(by_rank) + 1)]
    return list(default)


def title_table(data: dict) -> list:
    """Weekly title per place as [title for 1st, 2nd, ...]."""
    return _exported_titles(data, "titles", DEFAULT_WEEKLY_TITLES)


def faction_title_table(data: dict) -> list:
    """Faction weekly title per place as [title for 1st, 2nd, ...]."""
    return _exported_titles(data, "factionTitles", DEFAULT_FACTION_TITLES)


def title_for_rank(titles: list, rank: int) -> Optional[str]:
    return titles[rank - 1] if 1 <= rank <= len(titles) else None


def _held(player: dict, prefix: str) -> Optional[str]:
    for t in _as_list(player.get("titles")):
        tid = t.get("id") if isinstance(t, dict) else None
        if isinstance(tid, str) and tid.startswith(prefix):
            return tid[len(prefix):]
    return None


def held_weekly_title(player: dict) -> Optional[str]:
    """The weekly title a player holds right now (won last week), or None."""
    return _held(player, _WEEKLY_TITLE_PREFIX)


def held_faction_title(player: dict) -> Optional[str]:
    """The faction weekly title a player's faction holds right now, or None."""
    return _held(player, _FACTION_TITLE_PREFIX)


def fire_rank(holder_place: Optional[int], rp: int = 0) -> int:
    """BT Notebook.fireTier: title holder places 1-5 -> 6 (Inferno) .. 2 (Spark),
    anyone else with RP this week -> 1 (Fuel), else 0."""
    if holder_place and 1 <= holder_place <= TITLE_COUNT:
        return 7 - holder_place
    return 1 if rp > 0 else 0


def last_finalized(data: dict) -> Optional[tuple]:
    """(weekId, history entry) of the last finalized week, or None.

    BT picks the entry with the newest `finalizedAt` (WeeklyRanking.lastFinalized);
    week ids ("YYYY-Wnn") break ties and cover entries without it.
    """
    wr = data.get("weeklyRanking") or {}
    history = wr.get("history") if isinstance(wr.get("history"), dict) else {}
    past = [(wid, h) for wid, h in history.items()
            if isinstance(h, dict) and wid != wr.get("weekId")]
    if not past:
        return None
    return max(past, key=lambda e: (_int(e[1].get("finalizedAt")), e[0]))


def title_holders(data: dict) -> dict:
    """{player: place} for last week's top 5 who still hold their weekly title
    (BT WeeklyRanking.GetTitleHolders)."""
    data = data or {}
    players = data.get("players") if isinstance(data.get("players"), dict) else {}
    last = last_finalized(data)
    holders = {}
    for r in _as_list((last[1] if last else {}).get("top5")):
        if not isinstance(r, dict) or not r.get("player") or not r.get("title"):
            continue
        pid = str(r["player"])
        if held_weekly_title(players.get(pid) or {}) == r["title"]:
            holders[pid] = _int(r.get("rank"))
    return holders


def faction_leaderboard(data: dict, limit: Optional[int] = None) -> dict:
    """This week's faction standings and last week's faction title holders.

    Returns:
      {
        "titles": ["Legendary Faction", ...],
        "rows": [{rank, faction, name, rp, members, title}],   # every faction with weekly RP != 0
        "lastWeek": {"weekId", "rows": [{rank, faction, name, rp, title}]} | None,
      }
    """
    data = data or {}
    factions = data.get("factions") if isinstance(data.get("factions"), dict) else {}
    titles = faction_title_table(data)

    def name_of(fid):
        f = factions.get(fid) or {}
        return str(f.get("name") or fid)

    # BT WeeklyRanking.computeFactionLeaderboard: rp != 0, rp desc, id asc.
    standing = []
    for fid, f in factions.items():
        if isinstance(f, dict) and _int(f.get("weeklyRep")) != 0:
            standing.append((str(fid), _int(f.get("weeklyRep")), f))
    standing.sort(key=lambda e: (-e[1], e[0]))
    rows = []
    for i, (fid, rp, f) in enumerate(standing, 1):
        rows.append({
            "rank": i,
            "faction": fid,
            "name": name_of(fid),
            "rp": rp,
            "members": len(_as_list(f.get("members"))),
            # Only a faction with positive weekly RP is handed a title.
            "title": title_for_rank(titles, i) if rp > 0 else None,
        })
    if limit is not None:
        rows = rows[:max(0, limit)]

    last_week = None
    last = last_finalized(data)
    if last:
        lw_rows = []
        for r in _as_list(last[1].get("factionTop5")):
            if not isinstance(r, dict) or not r.get("faction"):
                continue
            lw_rows.append({
                "rank": _int(r.get("rank")),
                "faction": str(r["faction"]),
                "name": name_of(str(r["faction"])),
                "rp": _int(r.get("rp")),
                "title": r.get("title"),
            })
        lw_rows.sort(key=lambda row: row["rank"])
        if lw_rows:
            last_week = {"weekId": last[0], "rows": lw_rows}

    return {"titles": titles, "rows": rows, "lastWeek": last_week}


def weekly_leaderboard(data: dict, limit: Optional[int] = None) -> dict:
    """Shape the weekly RP leaderboard with the title each place earns.

    Returns:
      {
        "weekId": "2026-W40" | None,
        "generatedAt": epoch seconds (0 if unknown),
        "titles": ["Legendary Survivor", ...],         # place 1..N
        "rows": [{rank, player, displayName, rp, categories{wealth,kills,quests,events},
                  title,          # title this place earns if the week ended now (top 5)
                  heldTitle,      # weekly title the player holds now (from last week)
                  fire}],         # fire rank 0-6 (BT Notebook.fireTier)
        "lastWeek": {"weekId", "rows": [{rank, player, displayName, rp, title, holds}]} | None,
        "factions": faction_leaderboard(data),
      }
    """
    data = data or {}
    wr = data.get("weeklyRanking") or {}
    players = data.get("players") if isinstance(data.get("players"), dict) else {}
    titles = title_table(data)
    holders = title_holders(data)

    rows = []
    for r in _as_list(wr.get("leaderboard")):
        if not isinstance(r, dict) or not r.get("player"):
            continue
        pid = str(r["player"])
        rank = _int(r.get("rank")) or len(rows) + 1
        cats = r.get("categories") if isinstance(r.get("categories"), dict) else {}
        rp = _int(r.get("rp"))
        rows.append({
            "rank": rank,
            "player": pid,
            "displayName": str(r.get("displayName") or pid),
            "rp": rp,
            "categories": {c: _int(cats.get(c)) for c in _CATEGORIES},
            "title": title_for_rank(titles, rank),
            "heldTitle": held_weekly_title(players.get(pid) or {}),
            "fire": fire_rank(holders.get(pid), rp),
        })
    rows.sort(key=lambda row: row["rank"])
    if limit is not None:
        rows = rows[:max(0, limit)]

    last_week = None
    last = last_finalized(data)
    if last:
        wid, entry = last
        lw_rows = []
        for r in _as_list(entry.get("top5")):
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
                "holds": pid in holders,
            })
        lw_rows.sort(key=lambda row: row["rank"])
        last_week = {"weekId": wid, "rows": lw_rows}

    return {
        "weekId": wr.get("weekId"),
        "generatedAt": _int(data.get("generatedAt")),
        "titles": titles,
        "rows": rows,
        "lastWeek": last_week,
        "factions": faction_leaderboard(data, limit),
    }


def title_name(player: dict, title_id) -> Optional[str]:
    """Display name of one of a player's title ids (from their `titles` list)."""
    if not isinstance(title_id, str) or not title_id:
        return None
    for t in _as_list(player.get("titles")):
        if isinstance(t, dict) and t.get("id") == title_id and t.get("name"):
            name = str(t["name"])
            # A weekly title without its own definition is named by its id.
            for prefix in (_WEEKLY_TITLE_PREFIX, _FACTION_TITLE_PREFIX):
                if name.startswith(prefix):
                    name = name[len(prefix):]
            return name
    for prefix in (_WEEKLY_TITLE_PREFIX, _FACTION_TITLE_PREFIX):
        if title_id.startswith(prefix):
            return title_id[len(prefix):]
    return None


def player_title(player: dict) -> Optional[str]:
    """The title a player wears in game, else their Reputation rank."""
    return title_name(player, player.get("equippedTitle")) or (
        str(player["repTier"]) if player.get("repTier") and player.get("repTier") != "Unknown" else None)


BOARD_PERIODS = ("week", "alltime")
BOARD_SIZE = 5


def reputation_board(data: dict, kind: str = "personal", period: str = "week") -> dict:
    """Top 5 for the reputation board image (rep_board.py).

    kind:   "personal" (players) or "faction".
    period: "week" (weekly RP, the ranking that hands out titles) or
            "alltime" (lifetime Reputation).

    Returns {"kind", "period", "weekId", "rows": [{place, name, title, value,
    players}]}: `name` is the PZ username (BT player id) or the faction name,
    `title` the player's worn title (else Reputation rank) or the faction's
    weekly title (else its rank), and `players` the PZ usernames whose Discord
    avatars stand for the row (the player, or a faction's top members).
    """
    data = data or {}
    players = data.get("players") if isinstance(data.get("players"), dict) else {}
    factions = data.get("factions") if isinstance(data.get("factions"), dict) else {}
    weekly = period != "alltime"
    rows = []

    if kind == "faction":
        held = {}
        last = last_finalized(data)
        for r in _as_list((last[1] if last else {}).get("factionTop5")):
            if isinstance(r, dict) and r.get("faction") and r.get("title"):
                held[str(r["faction"])] = str(r["title"])
        key = "weeklyRep" if weekly else "rep"
        ranked = [(str(fid), _int(f.get(key)), f) for fid, f in factions.items() if isinstance(f, dict)]
        ranked = [e for e in ranked if e[1] != 0] if weekly else ranked
        ranked.sort(key=lambda e: (-e[1], e[0]))
        for place, (fid, value, f) in enumerate(ranked[:BOARD_SIZE], 1):
            members = [str(m) for m in _as_list(f.get("members"))]
            members.sort(key=lambda m: (-_int((players.get(m) or {}).get("rep")), m))
            tier = f.get("repTier") if f.get("repTier") != "Unknown" else None
            rows.append({"place": place, "name": str(f.get("name") or fid),
                         "title": held.get(fid) or tier, "value": value, "players": members})
    else:
        if weekly:
            ranked = [(r["player"], r["rp"]) for r in weekly_leaderboard(data)["rows"]]
        else:
            ranked = sorted(((str(pid), _int(p.get("rep"))) for pid, p in players.items()
                             if isinstance(p, dict)), key=lambda e: (-e[1], e[0]))
        for place, (pid, value) in enumerate(ranked[:BOARD_SIZE], 1):
            rows.append({"place": place, "name": pid, "title": player_title(players.get(pid) or {}),
                         "value": value, "players": [pid]})

    return {"kind": "faction" if kind == "faction" else "personal",
            "period": "week" if weekly else "alltime",
            "weekId": (data.get("weeklyRanking") or {}).get("weekId"),
            "rows": rows}


def ladder_ranks(data: dict) -> dict:
    """{BT player id (PZ username): rank} — BT's fire ranks (`fire_rank`) for
    every current title holder and every player with RP this week."""
    data = data or {}
    holders = title_holders(data)
    ranks = {pid: fire_rank(place) for pid, place in holders.items()}
    for r in _as_list((data.get("weeklyRanking") or {}).get("leaderboard")):
        if isinstance(r, dict) and r.get("player") and _int(r.get("rp")) > 0:
            pid = str(r["player"])
            ranks[pid] = max(ranks.get(pid, 0), fire_rank(holders.get(pid), _int(r.get("rp"))))
    return {pid: rank for pid, rank in ranks.items() if rank > 0}


async def get_ladder_ranks(bot, force: bool = False) -> Optional[dict]:
    """Read the export and return `ladder_ranks(...)`, or None if unavailable."""
    data = await read_progression(bot, force)
    if data is None:
        return None
    return ladder_ranks(data)


async def get_weekly_leaderboard(bot, limit: Optional[int] = None,
                                 force: bool = False) -> Optional[dict]:
    """Read the export and return `weekly_leaderboard(...)`, or None if unavailable."""
    data = await read_progression(bot, force)
    if data is None:
        return None
    return weekly_leaderboard(data, limit)
