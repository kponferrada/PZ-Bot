"""Offline render of the reputation board poster (rep_board.py).

Run from bot/:  python -m pytest tests
"""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

import ranks  # noqa: E402
import rep_board  # noqa: E402


def _avatar(colour):
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), colour).save(buf, "PNG")
    return buf.getvalue()


def _board(kind="personal", n=5):
    return {"kind": kind, "period": "week", "rows": [
        {"place": i, "name": f"player{i}", "title": "Legendary Survivor" if i == 1 else None,
         "value": 1000 - i * 100, "players": [f"player{i}"]} for i in range(1, n + 1)]}


def test_place_colours_are_the_fire_ranks():
    assert [rep_board.place_colour(p) for p in range(1, 6)] == [
        ranks.RANKS[r].rgb for r in (6, 5, 4, 3, 2)]


def test_renders_at_template_size_with_tinted_tiles():
    img = rep_board.render_board(_board(), {"player1": _avatar((255, 0, 255))})
    assert img.size == (1024, 1536)
    # The place tiles take their rank colour (sample a painted spot per tile).
    for place, geo in enumerate(rep_board.ROWS, 1):
        x0, y0, x1, y1 = geo["tile"]
        px = [img.getpixel((x, y)) for x in range(x0 + 10, x1 - 10, 6) for y in range(y0 + 10, y1 - 10, 6)]
        target = rep_board.place_colour(place)
        def close(c):
            return sum(abs(a - b) for a, b in zip(c, target)) < 90
        assert sum(map(close, px)) > len(px) * 0.2, place
    # Player 1's avatar is in the first polaroid.
    x0, y0, x1, y1 = rep_board.ROWS[0]["photo"]
    r, g, b = img.getpixel(((x0 + x1) // 2, (y0 + y1) // 2))
    assert r > 150 and b > 150 and g < 120


def test_partial_and_faction_boards_render():
    img = rep_board.render_board(_board("faction", 2), {
        "player1": _avatar((10, 200, 10)), "x": _avatar((10, 10, 200))})
    assert img.size == (1024, 1536)
    assert rep_board.render_board({"rows": []}).size == (1024, 1536)
    buf = rep_board.render_board_png(_board())
    assert buf.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"


def test_last_week_board_renders():
    board = _board()
    board["period"] = "lastweek"
    assert rep_board.render_board(board).size == (1024, 1536)
    board["kind"] = "faction"
    assert rep_board.render_board(board).size == (1024, 1536)
