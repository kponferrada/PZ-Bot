"""Render the red/green player signal banners with a live player name.

The source artwork ships with a literal "{PLAYER_NAME}'" accent placeholder.
This module blanks that placeholder and re-types the name + apostrophe in the
matching accent colour, then re-positions the static white suffix
("s signal was lost." / "s signal is back.") so the sentence reads naturally
regardless of name length.
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_ASSET_DIR = Path(__file__).parent / "assets"
_FONT_SIZE = 74
_SUFFIX_GAP = 10          # px between the name's apostrophe and the white suffix

# Cross-platform bold sans-serif (Liberation Sans Bold is the free, metric
# Arial substitute and is bundled with the repo for the Linux VPS).
_FONT_CANDIDATES = [
    _ASSET_DIR / "fonts" / "LiberationSans-Bold.ttf",
    Path(r"C:\Windows\Fonts\arialbd.ttf"),
    Path(r"C:\Windows\Fonts\LiberationSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]


def _resolve_font() -> str:
    for p in _FONT_CANDIDATES:
        if p.is_file():
            return str(p)
    raise FileNotFoundError("No bold sans-serif font found")

# Geometry is in template-image coordinates (each banner is a separate image).
_BANNERS = {
    "disconnect": {
        "path": _ASSET_DIR / "player-disconnect.png",
        "accent": (255, 100, 102),          # red
        "accent_x0": 364,
        "accent_y0": 76,
        "accent_y1": 140,
        "bg_y": 71,                         # text-free row used to rebuild the gradient
        "baseline_y": 130,
        "suffix": (978, 75, 1530, 148),     # "s signal was lost." crop (x0,y0,x1,y1)
    },
    "connect": {
        "path": _ASSET_DIR / "player-connect.png",
        "accent": (12, 255, 169),           # green
        "accent_x0": 363,
        "accent_y0": 70,
        "accent_y1": 134,
        "bg_y": 65,
        "baseline_y": 124,
        "suffix": (980, 69, 1475, 142),     # "s signal is back." crop
    },
}

_SUFFIX_LUM = 120  # below this luminance a suffix pixel is treated as background


def _blank_region(px, cfg) -> None:
    """Erase the whole text line (accent placeholder + white suffix) by
    rebuilding the background gradient column-by-column from a text-free row."""
    x0 = cfg["accent_x0"]
    x1 = cfg["suffix"][2]
    y0 = min(cfg["accent_y0"], cfg["suffix"][1])
    y1 = max(cfg["accent_y1"], cfg["suffix"][3])
    bgy = cfg["bg_y"]
    for x in range(x0, x1):
        bg = px[x, bgy]
        for y in range(y0, y1):
            px[x, y] = bg


def _crop_suffix(im: Image.Image, cfg) -> Image.Image:
    """Crop the white suffix and make its background transparent."""
    sx0, sy0, sx1, sy1 = cfg["suffix"]
    suffix = im.crop((sx0, sy0, sx1, sy1)).convert("RGBA")
    sp = suffix.load()
    for y in range(suffix.height):
        for x in range(suffix.width):
            r, g, b, _a = sp[x, y]
            if max(r, g, b) >= _SUFFIX_LUM:
                sp[x, y] = (r, g, b, 255)
            else:
                sp[x, y] = (r, g, b, 0)
    return suffix


def render_banner(kind: str, name: str) -> bytes:
    """Return a PNG (bytes) of the given banner with `name` substituted."""
    cfg = _BANNERS[kind]
    im = Image.open(cfg["path"]).convert("RGBA")
    px = im.load()

    suffix = _crop_suffix(im, cfg)
    _blank_region(px, cfg)

    draw = ImageDraw.Draw(im)
    font_path = _resolve_font()
    font = ImageFont.truetype(font_path, _FONT_SIZE)
    text = f"{name}'"

    # Auto-shrink long names so they never overlap the white suffix.
    size = _FONT_SIZE
    available = cfg["suffix"][0] - cfg["accent_x0"] - _SUFFIX_GAP
    while size > 24:
        font = ImageFont.truetype(font_path, size)
        w = draw.textlength(text, font=font)
        if w <= available:
            break
        size -= 2

    draw.text((cfg["accent_x0"], cfg["baseline_y"]), text,
              font=font, fill=cfg["accent"], anchor="ls")
    w = draw.textlength(text, font=font)

    paste_x = cfg["accent_x0"] + int(w) + _SUFFIX_GAP
    im.paste(suffix, (paste_x, cfg["suffix"][1]), suffix)

    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def save_banner(kind: str, name: str, out_path: str) -> None:
    Path(out_path).write_bytes(render_banner(kind, name))


if __name__ == "__main__":
    import sys
    save_banner(sys.argv[1], sys.argv[2], sys.argv[3])
    print("wrote", sys.argv[3])
