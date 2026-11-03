"""Siege Night news from Barangay Siege (the bridge's source in Siege Night's place).

BLib2 Admin's Siege Night bridge reads Barangay Siege when it runs (BLib2Admin.SiegeNight.SetSource; Barangay Siege's
Bot.lua), in the same keys and sections the bot already reads. The fixtures are the status file as the server writes
it, one siege night played headless by `mods/bsiege/tools/dump_bot_status.lua` (BLib2 + blib2-admin's bridge +
Barangay Siege):
  siege_night_status_bsiege_night.txt   the night under way (wave 2 of 3)
  siege_night_status_bsiege_after.txt   after it: the last night's board

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


def test_the_poller_reads_an_active_night():
    status = _status("siege_night_status_bsiege_night.txt")
    sched, siege, waves = siege_night.SiegeNightCog._split_status(status)
    assert sched["phase"] == "active" and sched["eventDay"] == 10
    assert sched["siegeCount"] == 0                      # "Siege #1" in the begun card
    assert waves["sessionId"] and waves["waveCount"] == 3 and waves["currentWave"] == 2
    assert waves["wave1"]["status"] == "completed" and waves["wave2"]["status"] == "started"
    assert waves["wave2"]["expected"] > 0
    target = siege_night.siege_target(siege, waves, waves["waveCount"])
    assert target >= waves["wave1"]["expected"] + waves["wave2"]["expected"]
    assert lua_bridge.siege_bridge_info(status) is not None


def test_night_participants_in_dead_and_left():
    status = _status("siege_night_status_bsiege_night.txt")
    still, out = siege_night.night_participants(status)
    assert [r["name"] for r in still][:1] == ["Jamiebear"]
    assert [r["kills"] for r in still] == sorted([r["kills"] for r in still], reverse=True)
    assert "Peña" in [r["name"] for r in still]          # \ddd bytes read back as UTF-8
    assert {r["name"]: r["status"] for r in out} == {"Ghost": "dead", "Nomad": "left"}


def test_status_fields_this_siege_and_all_sieges():
    fields = dict(siege_night.status_fields(_status("siege_night_status_bsiege_night.txt")))
    assert "Spawned" in fields["\U0001f9df This Siege"]
    assert any(name.startswith("\U0001f9cd Still Standing (3)") for name in fields)
    assert "\U0001f3c6 All Sieges — Top Killers" in fields     # (empty before the first night ends)


def test_after_the_night_the_results_card():
    status = _status("siege_night_status_bsiege_after.txt")
    sched, siege, _ = siege_night.SiegeNightCog._split_status(status)
    assert sched["phase"] == "idle" and sched["totalSiegesCompleted"] == 1      # the "ended" card fires once
    assert siege["killsThisSiege"] > 0                                           # its numbers, kept after the night
    assert siege_night.night_participants(status) is None
    last = siege_night.last_siege(status)
    assert last["no"] == 1 and last["day"] == 10 and last["players"] == 5 and last["survived"] == 3
    statuses = {r["name"]: r["status"] for r in last["rows"]}
    assert statuses["Ghost"] == "dead" and statuses["Nomad"] == "left" and statuses["Jamiebear"] == "survived"
    embed = siege_night.results_embed(last)
    assert embed.title == "\U0001f3c6 Siege Night #01 Results"
    assert any(f.name.endswith("Survived the Whole Night (3)") for f in embed.fields)


def test_records_every_siege():
    killers, survivors, rec = siege_night.all_sieges(_status("siege_night_status_bsiege_after.txt"))
    assert rec["sieges"] == 1 and rec["worldKills"] > 0
    assert killers[0]["name"] == "Jamiebear" and killers[0]["kills"] >= killers[-1]["kills"]
    assert {r["name"] for r in survivors} == {"Jamiebear", "Peña", "Raven"}


def test_the_source_is_named_siege_night_is_not():
    sched, _, _ = siege_night.SiegeNightCog._split_status(_status("siege_night_status_bsiege_after.txt"))
    assert siege_night.source_line(sched).startswith("Siege data: Barangay Siege")
    blib2, _, _ = siege_night.SiegeNightCog._split_status(_status("siege_night_status_blib2.txt"))
    assert siege_night.source_line(blib2) == ""
