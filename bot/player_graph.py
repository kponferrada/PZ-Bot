"""player_graph.py — players online over the last 24 hours, for the status card.

The status loop (every 30 s) calls `PlayerHistory.record` with the player
count (0 while the server is offline). Each 5-minute bucket keeps the highest
count seen in it, so short peaks still show. Buckets older than 24 hours are
dropped; the history is saved to player_history.json so a bot restart keeps
it. When the bot itself was down there are no samples and the line has a gap.

`render` draws the chart with Pillow (x axis in Philippine time, UTC+8, like
the rest of the bot).
"""
from __future__ import annotations

import datetime
import io
import json
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BUCKET = 300           # seconds per sample (5 min)
WINDOW = 24 * 3600     # seconds shown
_HISTORY = Path(__file__).parent / "player_history.json"
_TZ = datetime.timezone(datetime.timedelta(hours=8))  # GMT+8 (Philippines)
_FONTS = Path(__file__).parent / "assets" / "fonts"

# Discord's dark embed background, so the chart blends into the card.
_BG = (43, 45, 49)
_GRID = (64, 66, 72)
_AXIS_TEXT = (148, 155, 164)
_TITLE = (242, 243, 245)
_LINE = (0, 230, 118)


class PlayerHistory:
    """5-minute buckets of the highest player count, last 24 hours."""

    def __init__(self, path: Path | None = _HISTORY):
        self.path = path
        self.buckets: dict[int, int] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.buckets = {int(k): int(v) for k, v in raw.items()}
        except (OSError, ValueError, AttributeError):
            self.buckets = {}

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.write_text(json.dumps(self.buckets), encoding="utf-8")
        except OSError as e:
            print(f"[PlayerGraph] could not save {self.path.name}: {e}")

    def record(self, count: int, now: float | None = None) -> bool:
        """Add a sample. True when the chart changes (new bucket or a new peak)."""
        now = time.time() if now is None else now
        key = int(now // BUCKET * BUCKET)
        count = max(0, int(count or 0))
        old = self.buckets.get(key)
        if old is not None and count <= old:
            return False
        new_bucket = old is None
        self.buckets[key] = count
        if new_bucket:
            cutoff = now - WINDOW - BUCKET
            self.buckets = {k: v for k, v in self.buckets.items() if k >= cutoff}
            self._save()
        return True

    def points(self, now: float | None = None) -> list:
        """(bucket start, count) for the last 24 hours, oldest first."""
        now = time.time() if now is None else now
        return sorted((k, v) for k, v in self.buckets.items() if k > now - WINDOW)


def _font(name: str, size: int):
    try:
        return ImageFont.truetype(str(_FONTS / name), size)
    except OSError:
        return ImageFont.load_default()


def _hour_label(ts: float) -> str:
    t = datetime.datetime.fromtimestamp(ts, _TZ)
    h = t.hour % 12 or 12
    return f"{h} {'AM' if t.hour < 12 else 'PM'}"


def _segments(points: list) -> list:
    """Split the points where samples are missing (bot down) for over 2 buckets."""
    segs, cur = [], []
    for ts, n in points:
        if cur and ts - cur[-1][0] > 2 * BUCKET:
            segs.append(cur)
            cur = []
        cur.append((ts, n))
    if cur:
        segs.append(cur)
    return segs


def render(points: list, now: float | None = None, max_players: int = 0,
           width: int = 800, height: int = 300) -> bytes:
    """The chart as PNG bytes. Sized for an embed image (Discord shows it at
    about half size), so the type is large."""
    now = time.time() if now is None else now
    start = now - WINDOW
    counts = [n for _, n in points]
    peak = max(counts) if counts else 0
    current = counts[-1] if counts else 0
    top = max(4, peak)
    top = top + (-top % 4)  # a multiple of 4: four even grid steps

    img = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(img)
    title_f, label_f = _font("Oswald.ttf", 30), _font("NotoSans-Bold.ttf", 19)
    stat_f = _font("Oswald.ttf", 30)

    draw.text((20, 12), "Players · last 24 hours", font=title_f, fill=_TITLE)
    cap = f" / {max_players}" if max_players else ""
    peak_text = f"Peak {peak}"
    draw.text((width - 20, 14), peak_text, font=stat_f, fill=_AXIS_TEXT, anchor="ra")
    now_x = width - 20 - draw.textlength(peak_text, font=stat_f) - 24
    draw.text((now_x, 14), f"Now {current}{cap}", font=stat_f, fill=_LINE, anchor="ra")

    left, right, top_px, bottom = 50, width - 20, 72, height - 40
    plot_w, plot_h = right - left, bottom - top_px

    def x(ts):
        return left + (ts - start) / WINDOW * plot_w

    def y(n):
        return bottom - n / top * plot_h

    for i in range(5):
        n = top * i / 4
        yy = y(n)
        draw.line([(left, yy), (right, yy)], fill=_GRID, width=1)
        if i % 2 == 0:  # 0, half, top: enough to read, not cluttered
            draw.text((left - 10, yy), f"{n:g}", font=label_f, fill=_AXIS_TEXT, anchor="rm")

    # Hour labels every 4 hours, on the hour in PHT.
    first = datetime.datetime.fromtimestamp(start, _TZ).replace(minute=0, second=0, microsecond=0)
    t = first + datetime.timedelta(hours=1)
    while t.timestamp() <= now:
        if t.hour % 4 == 0:
            xx = x(t.timestamp())
            draw.text((xx, bottom + 8), _hour_label(t.timestamp()), font=label_f, fill=_AXIS_TEXT, anchor="ma")
        t += datetime.timedelta(hours=1)

    if points:
        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        for seg in _segments(points):
            # Step line: each bucket holds its value until the next one.
            line = []
            for i, (ts, n) in enumerate(seg):
                end = seg[i + 1][0] if i + 1 < len(seg) else min(ts + BUCKET, now)
                line += [(x(ts), y(n)), (x(end), y(n))]
            odraw.polygon(line + [(line[-1][0], bottom), (line[0][0], bottom)], fill=(*_LINE, 55))
            odraw.line(line, fill=(*_LINE, 255), width=4)
        img.paste(overlay, (0, 0), overlay)
    else:
        draw.text(((left + right) / 2, (top_px + bottom) / 2), "Collecting data…",
                  font=title_f, fill=_AXIS_TEXT, anchor="mm")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
