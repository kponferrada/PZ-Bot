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
            "old": {"displayName": "Old Champ",
                    "titles": [{"id": "weekly:Legendary Survivor", "name": "Legendary Survivor"},
                               {"id": "fweekly:Elite Faction", "name": "Elite Faction"}]},
        },
        "factions": {
            "f1": {"name": "Tondo Boys", "weeklyRep": 40, "members": ["p1", "p2"]},
            "f2": {"name": "Sari-Sari", "weeklyRep": 90, "members": ["old"]},
            "f3": {"name": "Idle", "weeklyRep": 0, "members": []},
            "f4": {"name": "Shunned", "weeklyRep": -20, "members": ["p3"]},
        },
        "weeklyRanking": {
            "weekId": "2026-W40",
            "top5": lb[:5],
            "leaderboard": lb,
            "history": {
                "2026-W38": {"top5": [{"rank": 1, "player": "x", "rp": 5, "title": "Legendary Survivor"}],
                             "finalizedAt": 1789000000},
                "2026-W39": {"top5": [
                    {"rank": 2, "player": "p1", "rp": 80, "title": "Elite Survivor"},
                    {"rank": 1, "player": "old", "rp": 90, "title": "Legendary Survivor"},
                ], "factionTop5": [
                    {"rank": 2, "faction": "f2", "rp": 30, "title": "Elite Faction"},
                    {"rank": 1, "faction": "f1", "rp": 50, "title": "Legendary Faction"},
                ], "finalizedAt": 1789600000},
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


def test_ladder_ranks_need_a_title_still_held():
    # p1 placed 2nd last week but no longer holds the title (expired): only Fuel.
    data = _sample()
    data["players"]["p1"]["titles"] = []
    assert btp.ladder_ranks(data)["p1"] == 1


def test_holder_without_rp_this_week_keeps_fire_rank():
    data = _sample()
    data["weeklyRanking"]["leaderboard"] = []
    assert btp.ladder_ranks(data) == {"old": 6, "p1": 5}


def test_fire_rank_matches_bt_notebook():
    assert [btp.fire_rank(p) for p in (1, 2, 3, 4, 5)] == [6, 5, 4, 3, 2]
    assert btp.fire_rank(None, 10) == 1
    assert btp.fire_rank(None, 0) == 0
    assert btp.fire_rank(6, 10) == 1


def test_leaderboard_rows_carry_fire_rank_and_last_week_holds():
    board = btp.weekly_leaderboard(_sample())
    assert [r["fire"] for r in board["rows"][:3]] == [5, 1, 1]   # p1 holds 2nd
    assert [r["holds"] for r in board["lastWeek"]["rows"]] == [True, True]


def test_last_week_by_finalized_at_not_week_id():
    data = _sample()
    data["weeklyRanking"]["history"]["2026-W38"]["finalizedAt"] = 1799999999
    assert btp.weekly_leaderboard(data)["lastWeek"]["weekId"] == "2026-W38"


def test_held_faction_title():
    assert btp.held_faction_title(_sample()["players"]["old"]) == "Elite Faction"
    assert btp.held_faction_title(_sample()["players"]["p1"]) is None


def test_faction_leaderboard_this_week_and_last_week():
    fb = btp.weekly_leaderboard(_sample())["factions"]
    assert [(r["rank"], r["name"], r["rp"], r["members"], r["title"]) for r in fb["rows"]] == [
        (1, "Sari-Sari", 90, 1, "Legendary Faction"),
        (2, "Tondo Boys", 40, 2, "Elite Faction"),
        (3, "Shunned", -20, 1, None),      # listed (rp != 0) but no title without positive RP
    ]
    assert fb["lastWeek"]["weekId"] == "2026-W39"
    assert [(r["rank"], r["name"], r["title"]) for r in fb["lastWeek"]["rows"]] == [
        (1, "Tondo Boys", "Legendary Faction"),
        (2, "Sari-Sari", "Elite Faction"),
    ]


def test_exported_faction_title_table_overrides_default():
    data = _sample()
    data["weeklyRanking"]["factionTitles"] = [{"rank": 1, "title": "Top Barangay"}]
    assert btp.faction_title_table(data) == ["Top Barangay"]
    assert btp.faction_title_table({}) == list(btp.DEFAULT_FACTION_TITLES)


class _Cfg:
    SFTP_LUA_DIR = "/srv/Lua/"
    BT_PROGRESSION_PATH = ""


class _Bot:
    config = _Cfg()


def test_export_paths_blib_first_then_legacy_until_blib_seen(monkeypatch):
    monkeypatch.setitem(btp._cache, "blib_seen", False)
    assert btp._paths(_Bot()) == ["/srv/Lua/BLib/mods/barangaytales/progression.json",
                                  "/srv/Lua/BarangayTales/progression.json"]
    monkeypatch.setitem(btp._cache, "blib_seen", True)
    assert btp._paths(_Bot()) == ["/srv/Lua/BLib/mods/barangaytales/progression.json"]


def test_export_path_override(monkeypatch):
    bot = _Bot()
    bot.config = type("C", (), {"SFTP_LUA_DIR": "/srv/Lua", "BT_PROGRESSION_PATH": " /x/p.json "})()
    assert btp._paths(bot) == ["/x/p.json"]


def test_rpleaderboard_embed_renders_fire_ranks_and_factions():
    import weekly_rp
    embed = weekly_rp.build_embed(btp.weekly_leaderboard(_sample(), limit=10))
    assert "\U0001fa75 p1**" in embed.description          # p1 holds 2nd -> Blaze
    names = [f.name for f in embed.fields]
    assert "Factions this week" in names
    assert any(n.startswith("Factions last week (2026-W39)") for n in names)
    assert all(len(f.value) <= 1024 for f in embed.fields)
