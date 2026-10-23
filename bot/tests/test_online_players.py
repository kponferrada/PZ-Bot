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
                                      {}, False, max_players=32, restart=(1_792_728_000, False))
    first = embed.description.split("\n")[0]
    assert "**2** / 32 survivors online" in first and "Online" in first
    assert "Juan" not in embed.description and all("Juan" not in f.value for f in embed.fields)
    assert [f.name for f in embed.fields] == ["Server age", "In-game date", "In-game time",
                                              "Weather", "Wind", "Siege Night"]
    assert all(f.inline for f in embed.fields)   # two rows of three columns


def test_restart_line_uses_discord_timestamps():
    assert server_status.restart_line((1_792_728_000, False)) == \
        "Next restart **<t:1792728000:t>** · <t:1792728000:R>"
    assert "Restarting <t:1792728000:R>" in server_status.restart_line((1_792_728_000, True))
    assert "none scheduled" in server_status.restart_line(None)


def test_offline_embed_counts_no_players():
    embed = server_status.build_embed(False, {"playerCount": 5}, {}, False, max_players=32, restart=None)
    assert embed.description.startswith("🔴 **Offline** · **0** / 32")
