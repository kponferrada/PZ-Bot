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
    assert embed.title == "Server status"
    assert embed.description == "🟢 Online · Day 1 · Restart at <t:1792728000:t> <t:1792728000:R>"
    assert "Juan" not in embed.description and all("Juan" not in f.value for f in embed.fields)
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Survivors"] == "2 / 32" and fields["Peak today"] == "2"   # never below who's on now
    assert fields["Next restart"] == "<t:1792728000:t>"
    # Three rows of three, then the siege (its real start only when known).
    assert [f.name for f in embed.fields] == ["Survivors", "Peak today", "Next restart", "In-game date",
                                              "In-game time", "Weather", "Next siege night"]
    assert all(f.inline for f in embed.fields)


def test_peak_today_and_siege_field():
    embed = server_status.build_embed(True, {"playerCount": 2, "month": 4, "day": 9, "year": 1995},
                                      {"phase": "idle", "nextSiegeDay": 686, "totalSiegesCompleted": 34},
                                      False, max_players=32, restart=None, peak_today=16)
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Peak today"] == "16" and fields["Next restart"] == "—"
    assert fields["Next siege night"].endswith("\n34 sieges survived")
    assert embed.description.endswith("No restart scheduled")


def test_restart_line_uses_discord_timestamps():
    assert server_status.restart_line((1_792_728_000, False)).endswith(
        "Restart at <t:1792728000:t> <t:1792728000:R>")
    assert "Restarting <t:1792728000:R>" in server_status.restart_line((1_792_728_000, True))
    assert "No restart scheduled" in server_status.restart_line(None)


def test_offline_embed_counts_no_players():
    embed = server_status.build_embed(False, {"playerCount": 5}, {}, False, max_players=32, restart=None)
    assert embed.description.startswith("🔴 Offline · ") and embed.fields[0].value == "0 / 32"


def test_no_world_data_no_server_age():
    embed = server_status.build_embed(False, {}, {}, False, max_players=32, restart=None)
    assert embed.description == "🔴 Offline · No restart scheduled"
    assert [f.name for f in embed.fields] == ["Survivors", "Peak today", "Next restart"]


def test_status_card_holds_graph_and_buttons():
    import asyncio

    import discord
    embed = server_status.build_embed(True, {"playerCount": 2, "month": 4, "day": 9, "year": 1995, "hour": 2,
                                             "minutes": 52, "isNight": True, "temperature": 10, "elapsedDays": 670},
                                      {"phase": "idle", "eventDay": 670, "nextSiegeDay": 686, "totalSiegesCompleted": 34},
                                      False, max_players=32, restart=(1_792_728_000, False), peak_today=16,
                                      siege_at=1_792_900_000)

    async def build():
        rows = [[discord.ui.Button(label=n, custom_id=f"x:{n}") for n in ("My rank", "Online players")]]
        return server_status.build_card(embed, rows=rows, image="attachment://players.png")

    card = asyncio.run(build())
    assert card.accent_colour == discord.Colour(server_status.ACCENT)
    kids = card.children
    kinds = [type(c).__name__ for c in kids]
    assert kinds == ["TextDisplay", "Separator", "TextDisplay", "TextDisplay", "TextDisplay", "Separator",
                     "TextDisplay", "Separator", "MediaGallery", "Separator", "ActionRow", "TextDisplay"]
    assert [c.visible for c in kids if isinstance(c, discord.ui.Separator)] == [False, True, False, True]
    head = kids[0].content.splitlines()
    assert head[1] == "### Server status" and head[2].startswith("🟢 Online · Day 670")
    assert kids[2].content == "-# Survivors\n**2 / 32** · peak today 16"
    assert kids[3].content == "-# In-game\n**May 10, 1995 · 2:52 AM · Night**"
    assert kids[4].content == "-# Weather\n**Clear · 10°C**"
    siege = kids[6].content.splitlines()
    assert siege[:2] == ["-# Next siege night", "**Day 686** · May 26 in-game"]
    assert siege[2].startswith("<t:1792900000:") and siege[2].endswith(" · <t:1792900000:R>")
    assert siege[3] == "34 sieges survived"
    assert [b.label for b in kids[10].children] == ["My rank", "Online players"]
    assert card.children[-1].content.startswith("-# Knox County never forgets")
