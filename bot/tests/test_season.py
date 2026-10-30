"""/seasonreset: the season's files are archived first, then emptied in their own
format; the season state records the ended season and stops known-player seeding.

Run from bot/:  python -m pytest tests
"""

import datetime
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import season  # noqa: E402
import whitelist  # noqa: E402

_NOW = datetime.datetime(2026, 10, 30, 21, 5, 0)


def _season_files(tmp_path):
    deaths = tmp_path / "deaths.json"
    deaths.write_text(json.dumps({"events": [{"survivor": "Juan", "ts": 1.0, "game_dt": "x"},
                                             {"survivor": "Ana", "ts": 2.0, "game_dt": "y"}]}),
                      encoding="utf-8")
    db = tmp_path / "players.db"
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("CREATE TABLE players (username TEXT PRIMARY KEY, first_seen TEXT NOT NULL, "
                     "last_seen TEXT NOT NULL, join_count INTEGER NOT NULL DEFAULT 1)")
        conn.executemany("INSERT INTO players VALUES (?, 'a', 'b', 1)", [("Juan",), ("Ana",), ("Pedro",)])
    conn.close()
    csv_path = tmp_path / "whitelist_requests.csv"
    csv_path.write_text(",".join(whitelist._CSV_HEADERS) + "\r\n" + "1,Juan\r\n", encoding="utf-8")
    return deaths, db, csv_path


def test_archive_then_reset(tmp_path):
    deaths, db, csv_path = _season_files(tmp_path)
    dest = tmp_path / "archive" / season.archive_name("Season 3", _NOW)
    counts = season.archive_files(dest, deaths, db, csv_path)
    assert counts == {"deaths.json": 2, "players.db": 3, "whitelist_requests.csv": 1}
    assert dest.name == "20261030-210500-season-3"

    season.reset_files(deaths, db, csv_path, whitelist._CSV_HEADERS)
    assert json.loads(deaths.read_text(encoding="utf-8")) == {"events": []}
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 0
    conn.close()
    assert csv_path.read_text(encoding="utf-8").splitlines() == [",".join(whitelist._CSV_HEADERS)]

    # the archive still holds last season
    assert len(json.loads((dest / "deaths.json").read_text(encoding="utf-8"))["events"]) == 2
    conn = sqlite3.connect(dest / "players.db")
    assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 3
    conn.close()
    assert "Juan" in (dest / "whitelist_requests.csv").read_text(encoding="utf-8")


def test_archive_refuses_an_existing_folder(tmp_path):
    deaths, db, csv_path = _season_files(tmp_path)
    dest = tmp_path / "archive" / "same"
    season.archive_files(dest, deaths, db, csv_path)
    try:
        season.archive_files(dest, deaths, db, csv_path)
    except FileExistsError:
        pass
    else:
        raise AssertionError("a second archive into the same folder must fail")


def test_missing_files_are_skipped(tmp_path):
    counts = season.archive_files(tmp_path / "a", tmp_path / "deaths.json", tmp_path / "players.db",
                                  tmp_path / "wl.csv")
    assert counts == {}
    season.reset_files(tmp_path / "deaths.json", tmp_path / "players.db", tmp_path / "wl.csv", ["x"])
    assert json.loads((tmp_path / "deaths.json").read_text(encoding="utf-8")) == {"events": []}
    assert not (tmp_path / "players.db").exists() and not (tmp_path / "wl.csv").exists()


def test_state_history_and_fresh_flag(tmp_path):
    path = tmp_path / "season_state.json"
    assert not season.fresh_start(path) and season.load_state(path) == {}
    s1 = season.write_state("Season 3", "20261001-000000-previous", _NOW, {}, path)
    assert season.fresh_start(path) and s1["season"] == "Season 3"
    later = _NOW + datetime.timedelta(days=30)
    s2 = season.write_state("Season 4", "20261129-000000-season-3", later, season.load_state(path), path)
    assert s2["season"] == "Season 4"
    assert [h["season"] for h in s2["history"]] == ["", "Season 3"]
    assert s2["history"][1]["started"] == s1["started"]


def test_slug():
    assert season.slug("Season 3: Tag-ulan!") == "season-3-tag-ulan"
    assert season.slug("") == "season" and season.archive_name("", _NOW).endswith("-previous")
