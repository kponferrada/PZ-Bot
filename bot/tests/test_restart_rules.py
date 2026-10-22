"""Restart rules: next schedule slot, skipping a slot after a mod-update restart,
and which restarts can be cancelled.

Run from bot/:  python -m pytest tests
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from restart_watch import cancel_refusal, next_slot, skip_for_recent_update  # noqa: E402

UTC = datetime.timezone.utc


def _t(h, m=0, day=23):
    return datetime.datetime(2026, 10, day, h, m, tzinfo=UTC)


def test_next_slot():
    hours = [4, 10, 16, 22]
    assert next_slot(_t(3, 59), hours) == _t(4)
    assert next_slot(_t(4), hours) == _t(10)          # strictly after
    assert next_slot(_t(23, 30), hours) == _t(4, day=24)
    assert next_slot(_t(4), hours) == next_slot(_t(4, 0), hours)


def test_skip_slot_after_recent_update_restart():
    slot = _t(10).timestamp()
    hour = 3600
    assert skip_for_recent_update(slot, _t(9, 20).timestamp(), hour)       # 40 min before
    assert skip_for_recent_update(slot, _t(9).timestamp(), hour)           # exactly the window
    assert not skip_for_recent_update(slot, _t(8, 30).timestamp(), hour)   # too long ago
    assert not skip_for_recent_update(slot, _t(10, 5).timestamp(), hour)   # after the slot
    assert not skip_for_recent_update(slot, 0, hour)                       # none recorded
    assert not skip_for_recent_update(slot, _t(9, 50).timestamp(), 0)      # rule off


def test_only_mod_check_restarts_refuse_cancel():
    assert cancel_refusal("mod_update", False)
    assert "deferred" in cancel_refusal("mod_update", False)
    for kind in ("scheduled", "manual", "forced_update"):
        assert cancel_refusal(kind, False) == ""
    # Once players are being kicked nothing stops it.
    assert cancel_refusal("scheduled", True)
