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

# Colours that read on Discord's light and dark themes alike: the image is
# transparent outside a faint grey panel, the text mid-grey, the line amber
# (the status embed's accent).
_PANEL = (128, 128, 128, 22)
_GUIDE = (128, 128, 128, 90)
_TEXT = (128, 132, 142, 255)
_LINE = (183, 121, 31)


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


def day_start(now: float) -> float:
    """Midnight Philippine time of `now`'s day."""
    t = datetime.datetime.fromtimestamp(now, _TZ)
    return t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def peak_since(points: list, since: float) -> int:
    """The highest count in buckets that end after `since` (0 if none)."""
    return max((n for ts, n in points if ts + BUCKET > since), default=0)


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
           width: int = 800, height: int = 220) -> bytes:
    """The chart as PNG bytes. Sized for an embed image (Discord shows it at
    about half size), so the type is large and the chart is short. Everything
    is drawn at 3x and scaled down: Pillow's lines and rounded corners aren't
    anti-aliased. `max_players` is unused (the scale follows the peak)."""
    now = time.time() if now is None else now
    start = now - WINDOW
    counts = [n for _, n in points]
    peak = max(counts) if counts else 0
    current = counts[-1] if counts else 0
    top = max(4, peak) * 1.15  # headroom over the peak

    k = 3
    img = Image.new("RGBA", (width * k, height * k), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    text_f, label_f = _font("NotoSans-Bold.ttf", 21 * k), _font("NotoSans-Bold.ttf", 18 * k)
    draw.rounded_rectangle((0, 0, width * k - 1, height * k - 1), radius=12 * k, fill=_PANEL)

    draw.text((22 * k, 16 * k), "Players · last 24 hours", font=text_f, fill=_TEXT)
    draw.text(((width - 22) * k, 16 * k), f"Now {current} · Peak {peak}", font=text_f, fill=_TEXT, anchor="ra")

    left, right, top_px, bottom = 30, width - 30, 64, height - 46

    def x(ts):
        return (left + (ts - start) / WINDOW * (right - left)) * k

    def y(n):
        return (bottom - n / top * (bottom - top_px)) * k

    # One dashed guide at half the scale.
    gy = y(top / 2)
    for gx in range(left * k, right * k, 12 * k):
        draw.line([(gx, gy), (min(gx + 6 * k, right * k), gy)], fill=_GUIDE, width=k)

    # Clock labels every 6 hours on the hour (PHT), then "Now" at the right end.
    label_y = (bottom + 14) * k
    first = datetime.datetime.fromtimestamp(start, _TZ).replace(minute=0, second=0, microsecond=0)
    t = first + datetime.timedelta(hours=1)
    while t.timestamp() <= now:
        xx = x(t.timestamp())
        if t.hour % 6 == 0 and right * k - xx > 70 * k:
            # Right at the left edge the label starts there instead of centring on it.
            edge = xx - left * k < 30 * k
            draw.text((left * k if edge else xx, label_y), _hour_label(t.timestamp()), font=label_f,
                      fill=_TEXT, anchor="la" if edge else "ma")
        t += datetime.timedelta(hours=1)
    draw.text((right * k, label_y), "Now", font=label_f, fill=_TEXT, anchor="ma")

    if points:
        peak_xy = None
        for seg in _segments(points):
            # One point per bucket, at its middle; straight lines between them.
            line = [(x(ts + BUCKET / 2), y(n)) for ts, n in seg]
            for (ts, n), xy in zip(seg, line):
                if n == peak and peak_xy is None or n == peak and xy[0] > peak_xy[0]:
                    peak_xy = xy  # the latest peak
            if len(line) == 1:
                cx, cy = line[0]
                draw.ellipse((cx - 3 * k, cy - 3 * k, cx + 3 * k, cy + 3 * k), fill=(*_LINE, 255))
                continue
            fill = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(fill).polygon(line + [(line[-1][0], bottom * k), (line[0][0], bottom * k)],
                                         fill=(*_LINE, 40))
            img.alpha_composite(fill)
            draw.line(line, fill=(*_LINE, 255), width=3 * k, joint="curve")
        if peak_xy and peak:
            cx, cy = peak_xy
            draw.ellipse((cx - 6 * k, cy - 6 * k, cx + 6 * k, cy + 6 * k), fill=(*_LINE, 255))
    else:
        draw.text(((left + right) / 2 * k, (top_px + bottom) / 2 * k), "Collecting data…",
                  font=text_f, fill=_TEXT, anchor="mm")

    img = img.resize((width, height), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
