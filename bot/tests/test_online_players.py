"""/onlineplayers answer text and the status embed's player count.

Run from bot/:  python -m pytest tests
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server_status  # noqa: E402


def test_online_list_text():
    assert "offline" in server_status.online_list_text(["Juan"], offline=True)
    assert server_status.online_list_text([], offline=False) == "*No survivors online.*"
    assert server_status.online_list_text(["Ana", "juan"], offline=False) == "• Ana\n• juan"
    long = server_status.online_list_text([f"player{i:04d}" for i in range(600)], offline=False)
    assert len(long) <= 4000 and long.endswith("…")


def test_status_embed_shows_a_count_not_names():
    embed = server_status.build_embed(True, {"playerCount": 2, "players": "Ana,Juan", "month": 6, "day": 8},
                                      {}, False, max_players=32)
    last = embed.fields[-1]
    assert last.name.endswith("Players Online") and last.value == "2 / 32" and not last.inline
    assert all("Juan" not in f.value for f in embed.fields)
    inline = [f for f in embed.fields if f.inline]
    assert len(inline) == 9   # three rows of three columns
