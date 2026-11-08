"""A restart waits for a siege night: when the siege status file counts as one
under way (restart_watch.siege_in_progress).

Run from bot/:  python -m pytest tests
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lua_bridge  # noqa: E402
from restart_watch import SIEGE_WAIT_KINDS, siege_in_progress  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return lua_bridge._parse_lua_nested(f.read())


def _status(phase="idle", ts=1000.0, night_open=None):
    status = {"schedule": {"phase": phase, "timestamp": ts}}
    if night_open is not None:
        status["night"] = {"open": night_open}
    return status


def test_barangay_siege_night_holds_and_after_does_not():
    night = _load("siege_night_status_bsiege_night.txt")
    after = _load("siege_night_status_bsiege_after.txt")
    assert siege_in_progress(night, now=night["schedule"]["timestamp"] + 10)
    assert not siege_in_progress(after, now=after["schedule"]["timestamp"] + 10)


def test_phase_or_open_night():
    assert siege_in_progress(_status("active"), now=1010)
    assert siege_in_progress(_status("ACTIVE"), now=1010)
    assert siege_in_progress(_status("warning", night_open=True), now=1010)   # countdown under way
    assert not siege_in_progress(_status("warning", night_open=False), now=1010)
    assert not siege_in_progress(_status("idle"), now=1010)
    assert not siege_in_progress(_status("ended"), now=1010)


def test_stale_or_broken_file_never_holds():
    assert not siege_in_progress(_status("active", ts=1000), now=1000 + 121)
    assert siege_in_progress(_status("active", ts=1000), now=1000 + 119)
    assert not siege_in_progress({"schedule": {"phase": "active"}}, now=1000)
    assert not siege_in_progress({"schedule": {"phase": "active", "timestamp": "x"}}, now=1000)
    assert not siege_in_progress(None)
    assert not siege_in_progress({})
    assert not siege_in_progress({"schedule": "active"})


def test_only_automatic_restarts_wait():
    assert set(SIEGE_WAIT_KINDS) == {"mod_update", "scheduled"}
