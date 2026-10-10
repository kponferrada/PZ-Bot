"""rank_sync.link_check: when whitelist approval may link a requester.
Run from bot/:  python -m pytest tests
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rank_sync import link_check  # noqa: E402


def test_unlinked_user_and_free_username_links():
    assert link_check({}, 1, "Juan") == "ok"
    assert link_check({"2": "Pedro"}, 1, "Juan") == "ok"


def test_already_linked_to_the_same_username():
    assert link_check({"1": "Juan"}, 1, "juan") == "same"


def test_user_linked_to_another_username_is_kept():
    assert link_check({"1": "Pedro"}, "1", "Juan") == "has_link"


def test_username_held_by_another_discord_user():
    assert link_check({"2": "JUAN"}, 1, "Juan") == "taken"
