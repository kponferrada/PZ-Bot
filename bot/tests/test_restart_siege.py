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


def test_every_countdown_restart_waits():
    assert set(SIEGE_WAIT_KINDS) == {"mod_update", "scheduled", "manual", "forced_update"}


# ---- a siege due soon ---------------------------------------------------------

def test_siege_due_soon():
    from restart_watch import siege_due_soon
    now, lead = 10_000.0, 600
    assert siege_due_soon(now + 600, now, lead)          # exactly 10 min away
    assert siege_due_soon(now + 60, now, lead)
    assert not siege_due_soon(now + 601, now, lead)
    assert siege_due_soon(now - 200, now, lead)          # estimate just passed, not seen started yet
    assert not siege_due_soon(now - 301, now, lead)      # long past: the estimate was wrong
    assert not siege_due_soon(None, now, lead)
    assert not siege_due_soon(now + 60, now, 0)          # lead time off


# ---- a countdown pauses when a siege starts -------------------------------------

import asyncio  # noqa: E402
import types  # noqa: E402

import restart_watch  # noqa: E402


class _State:
    def __init__(self):
        self.expecting_restart = True
        self.restart_shutdown_started = False


def _watch(kind, waited=False, holds=True):
    w = restart_watch.RestartWatch.__new__(restart_watch.RestartWatch)
    w.bot = types.SimpleNamespace(state=_State(),
                                  features=types.SimpleNamespace(is_enabled=lambda key: True),
                                  config=types.SimpleNamespace(ANNOUNCE_RESTART_IMAGE=""))
    w._restart_delay, w._kick_notify_at, w._save_at, w._kick_at = 3, 2, 2, 1
    w._restart_kind, w._restart_slot, w._restart_waited = kind, 1234, waited
    w._restart_at = 0.0
    w._defer_until, w._defer_task, w._deferred_kind, w._deferred_slot = 0.0, None, None, None
    w._siege_hold = False
    w._siege_max_wait = 600
    w.calls = []

    async def rec(name, *a, **k):
        w.calls.append(name)

    async def holds_restart(timeout=None):
        return holds

    for name in ("_announce", "_servermsg", "_announce_kick_notification", "_save_world",
                 "_kick_players", "_quit_server"):
        setattr(w, name, (lambda n: (lambda *a, **k: rec(n, *a, **k)))(name))
    w._siege_holds_restart = holds_restart
    return w


def _run(w, finish=None):
    async def go():
        if finish is None:
            w._restart_task = asyncio.current_task()
            await w._countdown_guarded()
        else:
            await w._run_countdown(finish=finish)
        if w._defer_task is not None:
            w._defer_task.cancel()
    asyncio.run(go())


def test_countdown_pauses_into_a_siege_hold(monkeypatch):
    monkeypatch.setattr(restart_watch, "COUNTDOWN_SIEGE_CHECK", 0)
    w = _watch("scheduled")
    _run(w)
    assert "_quit_server" not in w.calls and "_kick_players" not in w.calls
    assert w._siege_hold and w._deferred_kind == "scheduled" and w._deferred_slot == 1234
    assert w.bot.state.expecting_restart is False
    assert w._restart_task is None


def test_waited_restart_and_maintenance_never_pause(monkeypatch):
    monkeypatch.setattr(restart_watch, "COUNTDOWN_SIEGE_CHECK", 0)
    w = _watch("mod_update", waited=True)
    _run(w)
    assert "_quit_server" in w.calls and not w._siege_hold

    w = _watch("manual")
    done = []

    async def finish():
        done.append(True)
    _run(w, finish=finish)
    assert done and not w._siege_hold


def test_no_siege_countdown_runs_through(monkeypatch):
    monkeypatch.setattr(restart_watch, "COUNTDOWN_SIEGE_CHECK", 0)
    w = _watch("scheduled", holds=False)
    _run(w)
    assert w.calls[-1] == "_quit_server" and "_kick_players" in w.calls and not w._siege_hold
