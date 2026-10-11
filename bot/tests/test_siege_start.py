"""siege_night.siege_target / start_description: the "Siege Night Has Begun!"
notice never says "0 zombies" (pure, no Discord/SFTP).
Run from bot/:  python -m pytest tests
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from siege_night import siege_target, start_description  # noqa: E402


def test_target_from_status():
    assert siege_target({"targetZombies": 240}, {}, 5) == 240
    assert siege_target({"targetZombies": "240"}, {}, 5) == 240


def test_target_from_complete_wave_records_only():
    waves = {f"wave{i}": {"expected": 40} for i in range(1, 6)}
    assert siege_target({"targetZombies": 0}, waves, 5) == 200
    del waves["wave5"]                                   # not every wave known yet
    assert siege_target({"targetZombies": 0}, waves, 5) == 0
    assert siege_target({}, {}, 0) == 0


def test_start_description_with_and_without_target():
    with_target = start_description(240, 15, "East", 5)
    assert with_target.startswith("**240** zombies are descending on **15** survivor(s).")
    assert "Horde direction: **East**." in with_target and "**5** surge waves" in with_target
    without = start_description(0, 15, "East", 5)
    assert "0 zombies" not in without
    assert without.startswith("The horde is descending on **15** survivor(s).")
