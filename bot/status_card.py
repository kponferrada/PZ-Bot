"""status_card.py — the illustrated server-status card (STATUS_MODE=image).

Draws onto assets/status-card-template.png (1201 x 1310): the chalkboard
players graph at the top, the title and status line on the brush strokes, one
value per beige pill, and the player count. The art, labels and icons are
part of the template; everything that changes is drawn here.

The template still carries a few placeholders in its brush strokes and on the
chalkboard (the server name, the status line, the axis numbers, "Now __/__",
"(__ / __)"). `_base()` erases them once (masks the text pixels and refills
them from the surrounding texture, row by row, then softens the patch) and
caches the cleaned template; each render only draws on a copy of it.

Output is JPEG (painted art: a third of a PNG's size, ~20 ms to encode).
Coordinates below are template pixels.
"""
from __future__ import annotations

import datetime
import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

import player_graph

_ASSETS = Path(__file__).parent / "assets"
TEMPLATE = _ASSETS / "status-card-template.png"
_FONTS = _ASSETS / "fonts"

# Placeholders baked into the template: (box, which pixels are text).
_TITLE_BOX = (330, 352, 712, 440)          # "PZT TEST" on the red stroke
_STATUS_BOX = (316, 455, 842, 521)         # red dot + "OFFLINE — SERVER IS DOWN"
_NOW_BOX = (858, 56, 960, 95)              # "__/__" after "Now"
_PEAK_BOX = (1028, 56, 1076, 95)           # "__" after "Peak"
_Y_LABELS_BOX = (138, 108, 172, 262)       # 4 3 2 1 0
_X_LABELS_BOX = (205, 234, 1078, 286)      # 12 AM ... 8 PM (their tops reach above the 0 line)
_PLAYERS_BAND_BOX = (182, 1070, 578, 1112)  # "PLAYERS (__ / __) — ONLINE NOW"

# Value pills (the beige boxes under each label).
PILLS = {
    "SERVER AGE": (170, 643, 357, 683),
    "DATE": (481, 643, 641, 682),
    "TIME": (770, 643, 923, 683),
    "WEATHER": (171, 755, 356, 795),
    "WIND": (480, 755, 638, 796),
    "CYCLE": (769, 758, 922, 798),
    "NEXT SIEGE": (171, 878, 358, 919),
    "SIEGE STATUS": (483, 880, 641, 921),
    "COMPLETED": (769, 882, 923, 922),
    "RESTART": (175, 1000, 626, 1040),
    "PLAYERS": (178, 1113, 553, 1152),
}
_PILL = (194, 177, 151)
_INK = (38, 30, 24)

# Chart area on the chalkboard: x span, and the y of the "0" and top lines.
_PLOT_X = (178, 1070)
_PLOT_Y0, _PLOT_YTOP = 247, 126
_CHALK = (232, 232, 226)
_GREEN = (64, 222, 122)

_TITLE_INK = (244, 236, 222)
_STATUS_COLOURS = {"online": (46, 204, 96), "stale": (240, 176, 32), "offline": (220, 36, 48)}


def _font(name: str, size: int):
    try:
        return ImageFont.truetype(str(_FONTS / name), size)
    except OSError:
        return ImageFont.load_default()


def _lum(p) -> float:
    return 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]


def _erase(img: Image.Image, box: tuple, is_text, grow: int = 2) -> None:
    """Refill the text pixels in `box` from their left and right neighbours."""
    region = img.crop(box)
    px = region.load()
    w, h = region.size
    mask = Image.new("L", (w, h), 0)
    mp = mask.load()
    for y in range(h):
        for x in range(w):
            if is_text(px[x, y]):
                mp[x, y] = 255
    mask = mask.filter(ImageFilter.MaxFilter(2 * grow + 1))
    mp = mask.load()
    for y in range(h):
        x = 0
        while x < w:
            if not mp[x, y]:
                x += 1
                continue
            start = x
            while x < w and mp[x, y]:
                x += 1
            left = px[start - 1, y] if start > 0 else None
            right = px[x, y] if x < w else None
            a, b = left or right, right or left
            if a is None:
                continue
            n = x - start + 1
            for i in range(start, x):
                t = (i - start + 1) / n
                px[i, y] = tuple(int(a[c] * (1 - t) + b[c] * t) for c in range(3))
    soft = region.filter(ImageFilter.GaussianBlur(1.6))
    region.paste(soft, (0, 0), mask.filter(ImageFilter.GaussianBlur(1)))
    img.paste(region, box[:2])


@lru_cache(maxsize=1)
def _base() -> Image.Image:
    """The template with its placeholders erased (computed once)."""
    img = Image.open(TEMPLATE).convert("RGB")
    _erase(img, _TITLE_BOX, lambda p: min(p) > 150)
    _erase(img, _STATUS_BOX, lambda p: _lum(p) > 95 or (p[0] > 140 and p[1] < 90))
    _erase(img, _NOW_BOX, lambda p: p[1] > 110 or _lum(p) > 110)
    _erase(img, _PEAK_BOX, lambda p: p[1] > 110 or _lum(p) > 110)
    _erase(img, _Y_LABELS_BOX, lambda p: _lum(p) > 110)
    _erase(img, _X_LABELS_BOX, lambda p: _lum(p) > 110)
    _erase(img, _PLAYERS_BAND_BOX, lambda p: _lum(p) > 110)
    return img


def _fit(draw, text: str, name: str, size: int, max_w: int, min_size: int = 12):
    """The largest font (down to `min_size`) that fits `text` in `max_w`."""
    while size > min_size:
        font = _font(name, size)
        if draw.textlength(text, font=font) <= max_w:
            return font
        size -= 1
    return _font(name, min_size)


def _pill(draw, box: tuple, text: str) -> None:
    x0, y0, x1, y1 = box
    draw.rounded_rectangle((x0 + 1, y0 + 1, x1 - 1, y1 - 1), radius=6, fill=_PILL)
    font = _fit(draw, text, "Oswald.ttf", 26, x1 - x0 - 20)
    draw.text((x0 + 10, (y0 + y1) / 2), text, font=font, fill=_INK, anchor="lm")


def _graph(img: Image.Image, draw, points: list, now: float) -> None:
    start = now - player_graph.WINDOW
    counts = [n for _, n in points]
    top = max(4, max(counts) if counts else 0)
    top += -top % 4
    x_left, x_right = _PLOT_X
    label = _font("PermanentMarker-Regular.ttf", 17)

    def x(ts):
        return x_left + (ts - start) / player_graph.WINDOW * (x_right - x_left)

    def y(n):
        return _PLOT_Y0 - n / top * (_PLOT_Y0 - _PLOT_YTOP)

    for i in range(5):
        n = top * i // 4
        draw.text((160, y(n)), str(n), font=label, fill=_CHALK, anchor="rm")
    t = datetime.datetime.fromtimestamp(start, player_graph._TZ).replace(minute=0, second=0, microsecond=0)
    t += datetime.timedelta(hours=1)
    while t.timestamp() <= now:
        if t.hour % 4 == 0:
            draw.text((x(t.timestamp()), 268), player_graph._hour_label(t.timestamp()),
                      font=label, fill=_CHALK, anchor="mm")
        t += datetime.timedelta(hours=1)

    if not points:
        return
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    for seg in player_graph._segments(points):
        line = []
        for i, (ts, n) in enumerate(seg):
            end = seg[i + 1][0] if i + 1 < len(seg) else min(ts + player_graph.BUCKET, now)
            line += [(x(ts), y(n)), (x(end), y(n))]
        odraw.polygon(line + [(line[-1][0], _PLOT_Y0), (line[0][0], _PLOT_Y0)], fill=(*_GREEN, 70))
        odraw.line(line, fill=(*_GREEN, 255), width=3)
    img.paste(overlay, (0, 0), overlay)


def render_status_card(*, title: str, state: str, values: dict, players: int, max_players: int,
                       graph_points: list, now: float | None = None) -> bytes:
    """The card as JPEG bytes.

    `state` is "online", "stale" (online, RCON quiet) or "offline". `values`
    maps PILLS keys (except PLAYERS) to their text."""
    import time
    now = time.time() if now is None else now
    img = _base().copy()
    draw = ImageDraw.Draw(img)

    # Title on the red stroke.
    title_font = _fit(draw, title.upper(), "Anton-Regular.ttf", 74, _TITLE_BOX[2] - _TITLE_BOX[0] - 8, 34)
    draw.text((_TITLE_BOX[0] + 6, (_TITLE_BOX[1] + _TITLE_BOX[3]) / 2), title.upper(),
              font=title_font, fill=_TITLE_INK, anchor="lm")

    # Status line on the black stroke.
    cx, cy = 346, 488
    draw.ellipse((cx - 21, cy - 21, cx + 21, cy + 21), fill=_STATUS_COLOURS.get(state, _STATUS_COLOURS["offline"]))
    head, tail = {"online": ("ONLINE", " — SERVER IS RUNNING"),
                  "stale": ("ONLINE", " — DATA MAY BE STALE"),
                  "offline": ("OFFLINE", " — SERVER IS DOWN")}.get(state, ("OFFLINE", " — SERVER IS DOWN"))
    head_font = _font("Anton-Regular.ttf", 46)
    draw.text((385, cy), head, font=head_font, fill=_TITLE_INK, anchor="lm")
    tx = 385 + draw.textlength(head, font=head_font)
    tail_font = _fit(draw, tail, "Anton-Regular.ttf", 32, _STATUS_BOX[2] - 6 - int(tx), 18)
    draw.text((tx, cy + 3), tail, font=tail_font, fill=(196, 196, 190), anchor="lm")

    # Values.
    for key, box in PILLS.items():
        if key != "PLAYERS":
            _pill(draw, box, str(values.get(key, "—")))
    _pill(draw, PILLS["PLAYERS"], f"{players} / {max_players}")
    draw.text((192, 1091), "PLAYERS — ONLINE NOW", font=_font("Anton-Regular.ttf", 28),
              fill=_TITLE_INK, anchor="lm")

    # Chalkboard: Now / Peak and the 24-hour graph.
    counts = [n for _, n in graph_points]
    num = _font("Oswald.ttf", 30)
    draw.text((864, 75), f"{players}/{max_players}", font=num, fill=_GREEN, anchor="lm")
    draw.text((1034, 75), str(max(counts) if counts else players), font=num, fill=_GREEN, anchor="lm")
    _graph(img, draw, graph_points, now)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue()
