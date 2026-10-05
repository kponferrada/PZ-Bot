"""Tests for bt_progression.weekly_leaderboard (pure shaping, no SFTP).

Sample data follows Barangay Tales JsonExport.build() / WeeklyRanking.lua.
Run from bot/:  python -m pytest tests
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bt_progression as btp  # noqa: E402


def _row(rank, player, rp, kills=0, name=None):
    return {"rank": rank, "player": player, "rp": rp, "displayName": name or player,
            "categories": {"wealth": 0, "events": 0, "kills": kills, "quests": 0}}


def _sample():
    lb = [_row(i, f"p{i}", 100 - i * 10) for i in range(1, 8)]
    return {
        "generatedAt": 1790000000,
        "players": {
            "p1": {"displayName": "Player One",
                   "titles": [{"id": "survivor", "name": "Survivor"},
                              {"id": "weekly:Elite Survivor", "name": "weekly:Elite Survivor"}]},
            "old": {"displayName": "Old Champ", "titles": []},
        },
        "weeklyRanking": {
            "weekId": "2026-W40",
            "top5": lb[:5],
            "leaderboard": lb,
            "history": {
                "2026-W38": {"top5": [{"rank": 1, "player": "x", "rp": 5, "title": "Legendary Survivor"}]},
                "2026-W39": {"top5": [
                    {"rank": 2, "player": "p1", "rp": 80, "title": "Elite Survivor"},
                    {"rank": 1, "player": "old", "rp": 90, "title": "Legendary Survivor"},
                ], "factionTop5": []},
            },
        },
    }


def test_titles_follow_bt_config_for_top5_only():
    board = btp.weekly_leaderboard(_sample())
    titles = [r["title"] for r in board["rows"]]
    assert titles == list(btp.DEFAULT_WEEKLY_TITLES) + [None, None]
    assert board["weekId"] == "2026-W40"
    assert board["generatedAt"] == 1790000000


def test_held_title_comes_from_weekly_prefixed_title_id():
    rows = btp.weekly_leaderboard(_sample())["rows"]
    assert rows[0]["heldTitle"] == "Elite Survivor"
    assert rows[1]["heldTitle"] is None


def test_last_week_is_newest_finalized_week_sorted_by_rank():
    last = btp.weekly_leaderboard(_sample())["lastWeek"]
    assert last["weekId"] == "2026-W39"
    assert [(r["rank"], r["displayName"], r["title"]) for r in last["rows"]] == [
        (1, "Old Champ", "Legendary Survivor"),
        (2, "Player One", "Elite Survivor"),
    ]


def test_limit_and_empty_lua_tables():
    assert len(btp.weekly_leaderboard(_sample(), limit=3)["rows"]) == 3
    # Lua's encoder writes empty arrays as {}.
    data = {"weeklyRanking": {"weekId": "2026-W40", "leaderboard": {}, "history": {}}}
    board = btp.weekly_leaderboard(data)
    assert board["rows"] == [] and board["lastWeek"] is None


def test_exported_title_table_overrides_default():
    data = _sample()
    data["weeklyRanking"]["titles"] = [{"rank": 2, "title": "B"}, {"rank": 1, "title": "A"}]
    rows = btp.weekly_leaderboard(data)["rows"]
    assert [r["title"] for r in rows[:3]] == ["A", "B", None]


def test_missing_export_sections():
    board = btp.weekly_leaderboard({})
    assert board["rows"] == [] and board["weekId"] is None and board["lastWeek"] is None


def test_ladder_ranks_last_week_places_and_active_players():
    # Last week (W39): old 1st -> Inferno (6), p1 2nd -> Blaze (5).
    # This week everyone p1..p7 has RP -> Fuel (1), unless placed higher.
    assert btp.ladder_ranks(_sample()) == {
        "old": 6, "p1": 5, "p2": 1, "p3": 1, "p4": 1, "p5": 1, "p6": 1, "p7": 1,
    }


def test_ladder_ranks_skip_players_without_rp():
    data = _sample()
    data["weeklyRanking"]["leaderboard"][-1]["rp"] = 0
    assert "p7" not in btp.ladder_ranks(data)


def test_ladder_ranks_empty_export():
    assert btp.ladder_ranks({}) == {}
