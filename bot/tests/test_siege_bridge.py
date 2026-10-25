"""Siege Night through BLib's bridge (blib-admin, protocol 2).

The fixture `siege_night_status_blib.txt` is the status file exactly as BLib
writes it after a /siegestart and a refused /siegeschedule (made by
`mods/blib/tools/dump_siege_status.lua` with BLib's headless stubs).

Run from bot/:  python -m pytest tests
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lua_bridge  # noqa: E402
import siege_night  # noqa: E402

_FIXTURE = Path(__file__).parent / "fixtures" / "siege_night_status_blib.txt"


def _blib_status() -> dict:
    return lua_bridge._parse_lua_nested(_FIXTURE.read_text(encoding="utf-8"))


def test_blib_status_keeps_the_old_sections():
    status = _blib_status()
    sched, siege, waves = siege_night.SiegeNightCog._split_status(status)
    assert sched["phase"] == "active"
    assert sched["playerCount"] == 2
    assert isinstance(sched["timestamp"], int)
    assert siege["currentPhase"] == "SURGE"
    assert waves["waveCount"] == 5
    assert waves["wave1"] == {"number": 1, "expected": 11, "startedAt": 1700000000, "status": "started"}


def test_find_result_matches_id_and_our_timestamp():
    status = _blib_status()
    started = lua_bridge.find_siege_result(status, 1, 1700000000)
    assert started["ok"] is True and started["command"] == "siegestart"
    refused = lua_bridge.find_siege_result(status, 2, 1700000010)
    assert refused["ok"] is False and refused["code"] == "not_idle"
    # Same id from before a bot restart (another timestamp): not ours.
    assert lua_bridge.find_siege_result(status, 1, 1600000000) is None
    assert lua_bridge.find_siege_result(status, 9, 1700000000) is None


def test_old_bridge_status_has_no_results():
    old = {"schedule": {"phase": "idle"}, "siege": {}, "waves": {}}
    assert lua_bridge.siege_bridge_info(old) is None
    assert lua_bridge.find_siege_result(old, 1, 1) is None
    assert lua_bridge.find_siege_result(None, 1, 1) is None
    assert siege_night.bridge_line(None).startswith("Bridge: old")
    assert "blibadmin" in siege_night.bridge_line(lua_bridge.siege_bridge_info(_blib_status()))


def test_blib2_status_reads_the_same_and_names_blib2():
    # BLib2 (v2 fork, the test server) writes the same file; only the version
    # key is `blib2` (mods/blib-v2/tools/dump_siege_status.lua).
    status = lua_bridge._parse_lua_nested(
        (Path(__file__).parent / "fixtures" / "siege_night_status_blib2.txt").read_text(encoding="utf-8"))
    sched, siege, waves = siege_night.SiegeNightCog._split_status(status)
    assert sched["phase"] == "active" and siege["currentPhase"] == "SURGE" and waves["waveCount"] == 5
    assert lua_bridge.find_siege_result(status, 1, 1700000000)["ok"] is True
    line = siege_night.bridge_line(lua_bridge.siege_bridge_info(status))
    assert line == "Bridge: BLib2 2.0.0-dev (blib2admin), protocol 2"
    assert siege_night.bridge_line(lua_bridge.siege_bridge_info(_blib_status())).startswith("Bridge: BLib 0.7.0 ")


def test_command_file_carries_protocol_and_who_asked():
    text = lua_bridge._build_lua_table("siegeschedule", 3, timestamp=1700000000, protocol=2,
                                       by="Peña", byId="1234", targetDay=12)
    assert text.isascii()
    parsed = lua_bridge._parse_lua_nested(text)
    assert parsed == {"command": "siegeschedule", "id": 3, "timestamp": 1700000000, "protocol": 2,
                      "by": "Peña", "byId": "1234", "targetDay": 12}


def test_siege_command_waits_for_blibs_answer(monkeypatch):
    written = []
    reads = iter([None, {"schedule": {}}, None])

    async def fake_write(command, by=None, by_id=None, **kwargs):
        written.append((command, by, by_id, kwargs))
        return (7, 1700000000)

    async def fake_read():
        try:
            return next(reads)
        except StopIteration:
            return {"bridge": {"protocol": 2, "results": {
                "r1": {"id": 7, "sent": 1700000000, "command": "siegeschedule", "ok": True,
                       "code": "ok", "message": "next siege on day 12"}}}}

    async def no_sleep(_):
        return None

    monkeypatch.setattr(lua_bridge, "write_siege_command", fake_write)
    monkeypatch.setattr(lua_bridge, "read_siege_status", fake_read)
    monkeypatch.setattr(lua_bridge.asyncio, "sleep", no_sleep)
    out = asyncio.run(lua_bridge.siege_schedule(12, by="Keym", by_id="1"))
    assert written == [("siegeschedule", "Keym", "1", {"targetDay": 12})]
    assert out["sent"] is True and out["result"]["message"] == "next siege on day 12"
    assert out["bridge"]["protocol"] == 2


def test_siege_command_gives_up_without_an_answer(monkeypatch):
    async def fake_write(command, by=None, by_id=None, **kwargs):
        return (1, 1)

    async def fake_read():
        return {"schedule": {"phase": "idle"}}

    async def no_sleep(_):
        return None

    monkeypatch.setattr(lua_bridge, "write_siege_command", fake_write)
    monkeypatch.setattr(lua_bridge, "read_siege_status", fake_read)
    monkeypatch.setattr(lua_bridge.asyncio, "sleep", no_sleep)
    out = asyncio.run(lua_bridge.siege_command("siegestop", wait=0))
    assert out == {"sent": True, "result": None, "bridge": None}


def test_describe_outcome():
    title, desc, ok = siege_night.describe_siege_outcome(
        "start", {"sent": True, "result": {"ok": True, "code": "ok", "message": "siege #4 starting"}}, "Keym")
    assert ok is True and "Keym" in desc and "siege #4 starting" in desc and "Started" in title

    title, desc, ok = siege_night.describe_siege_outcome(
        "stop", {"sent": True, "result": {"ok": False, "code": "not_active", "message": "no siege is running (IDLE)"}}, "Keym")
    assert ok is False and title == "Could not stop the siege" and desc.startswith("No siege is running.")

    _, desc, ok = siege_night.describe_siege_outcome(
        "schedule", {"sent": True, "result": {"ok": False, "code": "weird", "message": "something new"}}, "Keym")
    assert ok is False and desc == "something new"

    title, _, ok = siege_night.describe_siege_outcome("start", {"sent": True, "result": None}, "Keym")
    assert ok is None and "Sent" in title

    _, desc, ok = siege_night.describe_siege_outcome("start", {"sent": False}, "Keym")
    assert ok is False and "command file" in desc
