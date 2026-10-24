"""Illustrated status card (STATUS_MODE=image): renders a JPEG on the template,
every value has a pill, the embed and the card share their values, and an
unchanged card isn't re-rendered or re-posted.

Run from bot/:  python -m pytest tests
"""

import asyncio
import io
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

import player_graph  # noqa: E402
import server_status  # noqa: E402
import status_card  # noqa: E402

WORLD = {"playerCount": 1, "players": "admin", "month": 6, "day": 11, "year": 1993, "hour": 7,
         "minutes": 43, "weather": "Clear", "temperature": 16, "windSpeed": 0.1, "elapsedDays": 3}


def test_card_is_a_jpeg_the_template_size():
    values = server_status.status_values(WORLD, {})
    for state in ("online", "stale", "offline"):
        data = status_card.render_status_card(title="PZT Test", state=state, values=values, players=1,
                                              max_players=8, graph_points=[(1_792_700_100, 1)],
                                              now=1_792_700_400)
        img = Image.open(io.BytesIO(data))
        assert img.format == "JPEG" and img.size == Image.open(status_card.TEMPLATE).size
        assert len(data) < 900_000


def test_every_value_has_a_pill_inside_the_template():
    values = server_status.status_values(WORLD, {})
    assert set(values) | {"PLAYERS"} == set(status_card.PILLS)
    w, h = Image.open(status_card.TEMPLATE).size
    for x0, y0, x1, y1 in status_card.PILLS.values():
        assert 0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h


def test_status_values_match_the_embed():
    values = server_status.status_values(WORLD, {})
    embed = server_status.build_embed(True, WORLD, {}, False, max_players=8)
    shown = {f.name: f.value for f in embed.fields}
    assert f" · {values['SERVER AGE']} · " in embed.description and values["SERVER AGE"] == "Day 3"
    assert shown["In-game date"] == values["DATE"] == "Jul 12, 1993"
    assert shown["In-game time"] == f"{values['TIME']} · {values['CYCLE']}" == "7:43 AM · Day"
    assert shown["Weather"] == "Clear · 16°C"


def test_unchanged_card_is_not_rendered_again(monkeypatch):
    renders, posts = [], []
    monkeypatch.setattr(status_card, "render_status_card", lambda **kw: renders.append(kw) or b"jpeg")
    cog = server_status.ServerStatusCog.__new__(server_status.ServerStatusCog)
    cog._history = player_graph.PlayerHistory(path=None)
    cog._card_key, cog._message_id = None, None

    async def publish(make_files, embeds=None):
        posts.append([f.filename for f in make_files()])
        cog._message_id = 1
    cog._publish = publish

    async def run():
        await cog._send_or_edit_card("online", WORLD, {}, 8)   # first post
        await cog._send_or_edit_card("online", WORLD, {}, 8)   # unchanged: skipped
        await cog._send_or_edit_card("online", {**WORLD, "minutes": 44}, {}, 8)   # time moved: render

    asyncio.run(run())
    assert len(renders) == 2 and posts[0] == ["status.jpg"]
    assert renders[-1]["values"]["TIME"] == "7:44 AM"
