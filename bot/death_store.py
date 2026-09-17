"""death_store.py — record death events with timestamps for day/week/all-time counts.

The death card's "DEATH COUNT" panel shows TODAY / THIS WEEK counts plus a big
total number. The all-time total is authoritative from Aegis Panel's ledger
(see aegis_stats); this module keeps a local timestamped log of deaths so the
bot can answer "how many times did THIS survivor die today / this week" — the
day/week counts are per-character, keyed on the Steam username.

The log is a small JSON file (deaths.json) with one event per death:
    {"survivor": "foo", "ts": 1726600000.0, "game_dt": "1993-7-22 14:32"}

Day/week boundaries use Philippine time (UTC+8), matching the community.
"""

from __future__ import annotations

import datetime
import json
import time
from pathlib import Path

_STORE_PATH = Path(__file__).parent / "deaths.json"
_TZ = datetime.timezone(datetime.timedelta(hours=8))  # GMT+8 (Philippines)


def _load() -> list:
    try:
        data = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        return data.get("events", [])
    return data


def _save(events: list) -> None:
    try:
        _STORE_PATH.write_text(json.dumps({"events": events}), encoding="utf-8")
    except OSError as e:
        print(f"[DeathStore] failed to write deaths.json: {e}")


def record_death(survivor: str, game_date_time: str = "") -> None:
    """Append a death event (timestamped now).

    Deduplicated on (survivor, game_date_time): if the Death Log file is
    re-read after a server restart, the same block must not double-count.
    """
    events = _load()
    for e in events:
        if e.get("survivor") == survivor and e.get("game_dt") == game_date_time:
            return  # already recorded this death
    events.append({
        "survivor": survivor,
        "ts": time.time(),
        "game_dt": game_date_time,
    })
    _save(events)


def _day_start(ts: float) -> float:
    dt = datetime.datetime.fromtimestamp(ts, tz=_TZ)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _week_start(ts: float) -> float:
    dt = datetime.datetime.fromtimestamp(ts, tz=_TZ)
    monday = dt - datetime.timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def count_since(ts: float, survivor: str = "") -> int:
    """Deaths with a timestamp >= `ts`.

    When `survivor` is given, only that survivor's deaths are counted — the
    death card shows per-character day/week counts, not the whole server's.
    """
    events = _load()
    if survivor:
        return sum(
            1 for e in events
            if e.get("survivor") == survivor and e.get("ts", 0) >= ts
        )
    return sum(1 for e in events if e.get("ts", 0) >= ts)


def count_today(survivor: str = "") -> int:
    return count_since(_day_start(time.time()), survivor)


def count_week(survivor: str = "") -> int:
    return count_since(_week_start(time.time()), survivor)


def count_all() -> int:
    """Total deaths in the local log (NOT authoritative — Aegis is)."""
    return len(_load())
