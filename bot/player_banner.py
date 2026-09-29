"""Render the red/green/blue player signal banners with a live player name.

The source artwork ships with a literal "{PLAYER_NAME}’" accent placeholder
followed by a white suffix ("s signal was lost." / "s signal is back." /
"has joined the apocalypse."). To substitute a real name we:

  1. Erase the text line. Over the plain gradient part of the card the whole
     band is rebuilt by blending the clean rows above and below it (removes the
     text *and* its drop shadow). Where the line overlaps the scenery (trees /
     fire tower) only the text pixels are inpainted, so the artwork stays intact.
  2. Un-matte the white suffix against the cleaned background to get a layer
     with true anti-aliased alpha (no hard luminance threshold, no stray pixels).
  3. Type the name (+ curly apostrophe) in the accent colour with a font close
     to the artwork's, re-place the suffix after it, and add a soft drop shadow.
     Long names scale the whole line down together.

Steps 1-2 depend only on the template, so they are computed once per banner
and cached.
"""
from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

_ASSET_DIR = Path(__file__).parent / "assets"
_FONT_SIZE = 72           # matches the artwork's x-height / cap height
_SUFFIX_GAP = 4           # px between the name's apostrophe and the white suffix
# Every banner is normalised to this exact rendered size (the connect banner at
# half scale). Each banner is scaled to fit (preserving aspect ratio) and centred
# on a canvas of this size, so all three render at the same dimensions with the
# text fully visible and undistorted.
_TARGET_SIZE = (1053, 143)

# Noto Sans Bold (SIL OFL, bundled) is the closest free match to the artwork's
# humanist sans; the others are fallbacks.
_FONT_CANDIDATES = [
    _ASSET_DIR / "fonts" / "NotoSans-Bold.ttf",
    _ASSET_DIR / "fonts" / "LiberationSans-Bold.ttf",
    Path(r"C:\Windows\Fonts\arialbd.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]


def _resolve_font() -> str:
    for p in _FONT_CANDIDATES:
        if p.is_file():
            return str(p)
    raise FileNotFoundError("No bold sans-serif font found")

# Geometry is in template-image coordinates (each banner is a separate image).
#   text_box    : (x0, y0, x1, y1) covering the placeholder + suffix line and its
#                 drop shadow; rows y0-1 and y1 must be text-free
#   plain_until : x where the scenery starts. Left of it the band is rebuilt from
#                 the rows above/below; right of it only text pixels are inpainted
#   suffix      : (x0, y0, x1, y1) of the white suffix in the template
#   max_x       : right-most x the text line may reach (keeps it clear of the
#                 tower icon); longer lines are scaled down as a whole
_BANNERS = {
    "disconnect": {
        "path": _ASSET_DIR / "player-disconnect.png",
        "accent": (255, 100, 102),          # red
        "accent_x0": 364,
        "baseline_y": 130,
        "text_box": (355, 64, 1545, 162),
        "plain_until": 1320,
        "suffix": (977, 72, 1535, 152),     # "s signal was lost."
        "max_x": 1690,
        "apostrophe": True,
    },
    "connect": {
        "path": _ASSET_DIR / "player-connect.png",
        "accent": (12, 255, 169),           # green
        "accent_x0": 363,
        "baseline_y": 124,
        "text_box": (354, 58, 1490, 156),
        "plain_until": 1280,
        "suffix": (978, 66, 1480, 146),     # "s signal is back."
        "max_x": 1700,
        "apostrophe": True,
    },
    "new": {
        "path": _ASSET_DIR / "new-player-connect.png",
        "accent": (0, 199, 253),            # sky blue
        "accent_x0": 339,
        "baseline_y": 124,
        "text_box": (330, 60, 1635, 156),
        "plain_until": 1180,
        "suffix": (890, 68, 1626, 145),     # "has joined the apocalypse."
        "max_x": 1700,
        "apostrophe": False,
        "suffix_gap": 20,                   # word space (no apostrophe)
    },
}

_WHITE = (246, 246, 246)   # suffix text colour in the artwork
_MASK_DILATE = 5           # grow the text mask over anti-aliased edges + shadow
_SHADOW_OFFSET = (2, 3)    # soft drop shadow re-applied under the new text
_SHADOW_BLUR = 4
_SHADOW_OPACITY = 0.7


def _is_text(p, accent) -> bool:
    r, g, b = p[:3]
    if min(r, g, b) > 120 and max(r, g, b) - min(r, g, b) < 60:
        return True  # white / near-white suffix (incl. its soft edges)
    ar, ag, ab = accent
    # Accent-hued pixel noticeably brighter than the dark background.
    return abs(r - ar) + abs(g - ag) + abs(b - ab) < 200 and max(r, g, b) > 90


def _lum(p) -> float:
    return 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]


def _row_avg(px, x, y, width):
    """Average colour of a small horizontal window (smooths out noise)."""
    acc = [0, 0, 0]
    xs = range(max(0, x - 2), min(width, x + 3))
    for xx in xs:
        p = px[xx, y]
        acc[0] += p[0]; acc[1] += p[1]; acc[2] += p[2]
    n = len(xs)
    return (acc[0] / n, acc[1] / n, acc[2] / n)


@lru_cache(maxsize=None)
def _prepare(kind: str):
    """Return (clean_background, suffix_layer) for a banner — cached."""
    cfg = _BANNERS[kind]
    orig = Image.open(cfg["path"]).convert("RGBA")
    clean = orig.copy()
    opx, cpx = orig.load(), clean.load()
    x0, y0, x1, y1 = cfg["text_box"]
    split = cfg["plain_until"]

    # 1. Text mask (dilated) — used for the scenery inpaint and suffix un-matte.
    raw = {(x, y) for y in range(y0, y1) for x in range(x0, x1) if _is_text(opx[x, y], cfg["accent"])}
    mask = set()
    d = _MASK_DILATE
    for (x, y) in raw:
        for dy in range(-d, d + 1):
            for dx in range(-d, d + 1):
                mask.add((x + dx, y + dy))

    # 2a. Plain gradient area: rebuild the whole band column by column by
    #     blending the clean rows just above and below it.
    h = y1 - y0
    for x in range(x0, split):
        top = _row_avg(opx, x, y0 - 1, orig.width)
        bot = _row_avg(opx, x, y1, orig.width)
        for y in range(y0, y1):
            t = (y - y0 + 0.5) / h
            cpx[x, y] = (round(top[0] + (bot[0] - top[0]) * t),
                         round(top[1] + (bot[1] - top[1]) * t),
                         round(top[2] + (bot[2] - top[2]) * t), 255)

    # 2b. Scenery area: onion-peel inpaint of just the text pixels, filling from
    #     the outside in with the average of already-known neighbours.
    todo = {q for q in mask if q[0] >= split}
    while todo:
        filled = {}
        for (x, y) in todo:
            acc = [0, 0, 0]
            n = 0
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    q = (x + dx, y + dy)
                    if q in todo or not (0 <= q[0] < orig.width and 0 <= q[1] < orig.height):
                        continue
                    p = cpx[q]
                    acc[0] += p[0]; acc[1] += p[1]; acc[2] += p[2]
                    n += 1
            if n >= 2:
                filled[(x, y)] = (acc[0] // n, acc[1] // n, acc[2] // n, 255)
        if not filled:
            break
        for q, p in filled.items():
            cpx[q] = p
        todo -= filled.keys()

    # 3. Un-matte the white suffix: original = a*WHITE + (1-a)*background.
    sx0, sy0, sx1, sy1 = cfg["suffix"]
    suffix = Image.new("RGBA", (sx1 - sx0, sy1 - sy0), (0, 0, 0, 0))
    spx = suffix.load()
    wl = _lum(_WHITE)
    for y in range(sy0, sy1):
        for x in range(sx0, sx1):
            if (x, y) not in mask:
                continue
            o, b = opx[x, y], cpx[x, y]
            p = o[:3]
            # Only the white suffix — skip accent-hued pixels (e.g. the apostrophe).
            if max(p) - min(p) > 90:
                continue
            bl = _lum(b)
            a = (_lum(o) - bl) / max(1.0, wl - bl)
            if a > 0.03:
                spx[x - sx0, y - sy0] = (*_WHITE, min(255, round(a * 255)))
    return clean, suffix


def render_banner(kind: str, name: str) -> bytes:
    """Return a PNG (bytes) of the given banner with `name` substituted."""
    cfg = _BANNERS[kind]
    clean, suffix = _prepare(kind)
    im = clean.copy()

    font = ImageFont.truetype(_resolve_font(), _FONT_SIZE)
    text = f"{name}\u2019" if cfg.get("apostrophe", True) else name
    gap = cfg.get("suffix_gap", _SUFFIX_GAP)
    x0, baseline = cfg["accent_x0"], cfg["baseline_y"]
    sy0, sy1 = cfg["suffix"][1], cfg["suffix"][3]

    # Compose the whole line (name + suffix) on its own layer at full size.
    w = round(ImageDraw.Draw(im).textlength(text, font=font))
    line_w = w + gap + suffix.width
    line = Image.new("RGBA", (line_w, im.height), (0, 0, 0, 0))
    ImageDraw.Draw(line).text((0, baseline), text, font=font, fill=cfg["accent"], anchor="ls")
    line.alpha_composite(suffix, (w + gap, sy0))

    # Long names: scale the whole line down together (keeps name and suffix the
    # same size), centred on the text's vertical middle.
    available = cfg["max_x"] - x0
    dy = 0
    if line_w > available:
        s = available / line_w
        mid = (sy0 + sy1) / 2
        line = line.resize((available, max(1, round(line.height * s))), Image.LANCZOS)
        dy = round(mid - mid * s)

    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    layer.alpha_composite(line, (x0, dy))

    # Soft drop shadow under the text, like the original artwork.
    alpha = layer.getchannel("A").point(lambda a: round(a * _SHADOW_OPACITY))
    shadow = Image.new("RGBA", im.size, (0, 0, 0, 0))
    shadow.putalpha(alpha)
    shadow = shadow.filter(ImageFilter.GaussianBlur(_SHADOW_BLUR))
    im.alpha_composite(shadow, _SHADOW_OFFSET)
    im.alpha_composite(layer)

    bg = im.getpixel((im.width // 2, int(im.height * 0.85)))  # padding colour
    im = _fit_to_target(im, bg)

    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _fit_to_target(im: Image.Image, bg_color: tuple) -> Image.Image:
    """Scale `im` to fit within `_TARGET_SIZE` (preserving aspect ratio) and
    centre it on an opaque canvas filled with `bg_color`. The padding fills the
    aspect-ratio gap so every banner is the exact same rendered size."""
    tw, th = _TARGET_SIZE
    scale = min(tw / im.width, th / im.height)
    nw = round(im.width * scale)
    nh = round(im.height * scale)
    if (nw, nh) != (im.width, im.height):
        im = im.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", _TARGET_SIZE, bg_color)
    canvas.paste(im, ((tw - nw) // 2, (th - nh) // 2), im)
    return canvas


def save_banner(kind: str, name: str, out_path: str) -> None:
    Path(out_path).write_bytes(render_banner(kind, name))


if __name__ == "__main__":
    import sys
    save_banner(sys.argv[1], sys.argv[2], sys.argv[3])
    print("wrote", sys.argv[3])
