"""Siege Night with Barangay Tales 2's sections (tonight, night, records, last).

The fixtures are the status file as the server writes it with BT2 running, one
siege night played headless by `mods/bt-v2/tools/tests/dump_siege_bot_status.lua`
(BLib2 + blib2-admin's bridge + BLib2's Siege Night fixture + BT2):
  siege_night_status_bt2_night.txt   the night under way
  siege_night_status_bt2_after.txt   after dawn: the last siege's leaderboard

Run from bot/:  python -m pytest tests
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lua_bridge  # noqa: E402
import siege_night  # noqa: E402

_FIX = Path(__file__).parent / "fixtures"


def _status(name: str) -> dict:
    return lua_bridge._parse_lua_nested((_FIX / name).read_text(encoding="utf-8"))


def test_the_bridges_own_sections_are_unchanged():
    status = _status("siege_night_status_bt2_night.txt")
    sched, siege, waves = siege_night.SiegeNightCog._split_status(status)
    assert sched["phase"] == "active"
    assert "currentPhase" in siege and "sessionId" in waves
    assert lua_bridge.siege_bridge_info(status) is not None


def test_night_participants_split_in_and_out():
    status = _status("siege_night_status_bt2_night.txt")
    still, out = siege_night.night_participants(status)
    assert [r["name"] for r in still][:1] == ["jamiebear"]
    assert [r["kills"] for r in still] == sorted([r["kills"] for r in still], reverse=True)
    assert "peña" in [r["name"] for r in still]          # \ddd bytes read back as UTF-8
    assert {r["name"]: r["status"] for r in out} == {"ghost": "dead", "raven": "left"}
    late = [r for r in still if r.get("late")]
    assert [r["name"] for r in late] == ["echo"]


def test_no_night_and_no_last_without_them():
    assert siege_night.night_participants(_status("siege_night_status_bt2_after.txt")) is None
    assert siege_night.last_siege(_status("siege_night_status_bt2_night.txt")) is None
    blib2 = _status("siege_night_status_blib2.txt")              # BLib2 without BT2
    assert siege_night.night_participants(blib2) is None
    assert siege_night.last_siege(blib2) is None
    assert siege_night.status_fields(blib2) == []


def test_last_siege_and_its_results_card():
    last = siege_night.last_siege(_status("siege_night_status_bt2_after.txt"))
    assert last["day"] == 10 and last["no"] == 1 and last["survived"] == 3 and last["players"] == 6
    assert len(last["rows"]) == 6 and last["rows"][0]["status"] == "survived"
    embed = siege_night.results_embed(last)
    assert embed.title.endswith("Siege Night #01 Results")
    fields = {f.name: f.value for f in embed.fields}
    top = next(v for k, v in fields.items() if "Top Killers" in k)
    assert top.splitlines()[0].endswith("**jamiebear** — 49 kills")
    surv = next(v for k, v in fields.items() if "Survived the Whole Night (3)" in k)
    assert "nomad" in surv and "ghost" not in surv
    fell = next(v for k, v in fields.items() if "Fell or Left (2)" in k)
    assert "~~ghost~~" in fell and "(dead)" in fell and "~~raven~~" in fell and "(left)" in fell
    assert any("Came Late" in k for k in fields)                 # echo: came late, still standing
    assert all(len(v) <= 1024 for v in fields.values())


def test_all_sieges_board_and_status_fields():
    status = _status("siege_night_status_bt2_after.txt")
    killers, survivors, rec = siege_night.all_sieges(status)
    assert killers[0]["name"] == "jamiebear" and rec["sieges"] == 1 and rec["worldKills"] > 0
    assert [r["name"] for r in survivors] == ["jamiebear", "nomad", "peña"]
    names = [n for n, _ in siege_night.status_fields(status)]
    assert any("Top Killers" in n for n in names) and any("Most Nights Survived" in n for n in names)
    assert any("Last Siege" in n for n in names)
    during = [n for n, _ in siege_night.status_fields(_status("siege_night_status_bt2_night.txt"))]
    assert any(n.startswith("\U0001f9cd Still Standing (4)") for n in during)
    assert any(n.startswith("\U0001f480 Left / Dead (2)") for n in during)
    fields = dict(siege_night.status_fields(_status("siege_night_status_bt2_night.txt")))
    assert fields["\U0001f9df This Siege"] == "Spawned **160/250** · Killed **112/250**"


def test_long_lists_stay_under_the_field_limit():
    rows = [{"name": f"Survivor_{i:03d}_with_a_long_name", "kills": 100 - i, "status": "dead"} for i in range(80)]
    value = siege_night._fit(siege_night.out_lines(rows))
    assert len(value) <= 1024 and "more" in value.splitlines()[-1]


def test_markdown_in_names_is_escaped():
    lines = siege_night.leaderboard_lines([{"name": "*bold*_x", "kills": 3}])
    assert r"\*bold\*\_x" in lines[0]
