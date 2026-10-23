"""24-hour player graph: bucketing, pruning, gaps, and an offline render.

Run from bot/:  python -m pytest tests
"""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

import player_graph as pg  # noqa: E402

T0 = 1_792_700_000 // pg.BUCKET * pg.BUCKET   # a bucket boundary


def test_bucket_keeps_the_peak():
    h = pg.PlayerHistory(path=None)
    assert h.record(3, now=T0 + 10)
    assert h.record(5, now=T0 + 40)         # new peak in the same bucket
    assert not h.record(2, now=T0 + 70)     # lower: no change
    assert h.points(now=T0 + 100) == [(T0, 5)]
    assert h.record(1, now=T0 + pg.BUCKET)  # next bucket
    assert h.points(now=T0 + pg.BUCKET + 1) == [(T0, 5), (T0 + pg.BUCKET, 1)]


def test_old_buckets_drop_out_after_24_hours():
    h = pg.PlayerHistory(path=None)
    h.record(4, now=T0)
    later = T0 + pg.WINDOW + 2 * pg.BUCKET
    h.record(1, now=later)
    assert h.points(now=later) == [(later // pg.BUCKET * pg.BUCKET, 1)]
    assert T0 not in h.buckets


def test_history_survives_a_restart(tmp_path):
    path = tmp_path / "player_history.json"
    h = pg.PlayerHistory(path=path)
    h.record(7, now=T0)
    assert pg.PlayerHistory(path=path).buckets == {T0: 7}


def test_gap_when_the_bot_was_down():
    pts = [(T0, 1), (T0 + pg.BUCKET, 2), (T0 + 10 * pg.BUCKET, 3)]
    assert [len(s) for s in pg._segments(pts)] == [2, 1]


def test_render_is_a_png():
    now = T0 + pg.WINDOW
    pts = [(T0 + i * pg.BUCKET, i % 9) for i in range(1, 288)]
    for points in (pts, []):
        img = Image.open(io.BytesIO(pg.render(points, now, max_players=32)))
        assert img.format == "PNG" and img.size == (800, 300)
