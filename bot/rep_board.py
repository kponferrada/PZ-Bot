"""rep_board.py — render the Barangay Tales reputation board ("Top 5") poster.

The template (assets/reputation-board.png, built from the source art by
scripts/build_reputation_board.py) is the PZ Tambayan "Reputation Ranking"
poster with its sample rows emptied. This module fills the five rows:

  - portrait: the Discord avatar of the player linked with /linkme (a faction
    row shows up to four of its linked members, highest Reputation first);
    "no photo" otherwise
  - name: the in-game (PZ) username, or the faction name
  - ribbon: the title the player wears in game (else their Reputation rank),
    or the faction's weekly title (else its rank)
  - bar + value: the score, the bar relative to 1st place

Each place is painted in its fire-rank colour from ranks.RANKS (1st Inferno,
2nd Blaze, 3rd Flame, 4th Cinder, 5th Spark: the ranks those places hold in
game), on the place tile, ribbon, medal, bar and score.

Pure Pillow, so it runs without Discord (tests/test_rep_board.py). Rendering
takes a moment; call it off the event loop.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

import ranks

_ASSETS = Path(__file__).parent / "assets"
_TEMPLATE = _ASSETS / "reputation-board.png"
_TINT = _ASSETS / "reputation-board-tint.png"
_RIBBON = _ASSETS / "reputation-board-ribbon.png"
_FONTS = _ASSETS / "fonts"
_ANTON = _FONTS / "Anton-Regular.ttf"
_OSWALD = _FONTS / "Oswald.ttf"
_MARKER = _FONTS / "PermanentMarker-Regular.ttf"

_INK = (26, 22, 20)
_LABEL = (205, 202, 196)

Box = Tuple[int, int, int, int]

# Row geometry (places 1-5), matching scripts/build_reputation_board.py ROWS.
#   name_x   left edge of the name; name_y its vertical centre
#   ribbon   where the ribbon stroke is stamped (covers the original)
#   bar      the progress bar track
#   value    centre of the score; label centre of the line under it
ROWS: List[Dict] = [
    dict(tile=(46, 480, 170, 646), medal=(680, 540, 746, 630), photo=(192, 492, 330, 632),
         name_x=352, name_y=516, ribbon=(350, 542, 592, 586), bar=(352, 587, 657, 623),
         value=(870, 580), label=(877, 630)),
    dict(tile=(54, 652, 178, 802), medal=(684, 698, 746, 772), photo=(196, 664, 326, 796),
         name_x=358, name_y=686, ribbon=(358, 708, 600, 746), bar=(348, 757, 665, 790),
         value=(876, 725), label=(882, 770)),
    dict(tile=(48, 812, 178, 962), medal=(688, 852, 746, 932), photo=(200, 816, 330, 948),
         name_x=362, name_y=848, ribbon=(364, 866, 610, 911), bar=(356, 918, 665, 947),
         value=(890, 877), label=(892, 926)),
    dict(tile=(48, 965, 182, 1105), medal=(684, 1008, 742, 1085), photo=(202, 968, 336, 1096),
         name_x=370, name_y=1001, ribbon=(370, 1019, 606, 1061), bar=(358, 1068, 640, 1099),
         value=(888, 1028), label=(890, 1075)),
    dict(tile=(48, 1112, 178, 1262), medal=(684, 1148, 742, 1222), photo=(202, 1112, 342, 1248),
         name_x=374, name_y=1149, ribbon=(376, 1159, 600, 1204), bar=(365, 1208, 625, 1239),
         value=(890, 1171), label=(890, 1216)),
]

# Header on the tape strip ("Top 5 Survivors").
_HEADER_CENTRE = (517, 418)
_HEADER_MAX_W = 420

_NAME_MAX_W = 236
_VALUE_MAX_W = 200

_font_cache: Dict[Tuple[str, int, str], ImageFont.FreeTypeFont] = {}


def _font(path: Path, size: int, variation: str = "") -> ImageFont.FreeTypeFont:
    key = (str(path), size, variation)
    if key not in _font_cache:
        font = ImageFont.truetype(str(path), size)
        if variation:
            try:
                font.set_variation_by_name(variation)
            except (OSError, ValueError):
                pass
        _font_cache[key] = font
    return _font_cache[key]


def _fit(draw: ImageDraw.ImageDraw, text: str, path: Path, size: int, max_w: int,
         min_size: int, variation: str = "") -> ImageFont.FreeTypeFont:
    """The largest font from `size` down to `min_size` whose `text` fits `max_w`."""
    for s in range(size, min_size - 1, -1):
        font = _font(path, s, variation)
        if draw.textlength(text, font=font) <= max_w:
            return font
    return _font(path, min_size, variation)


def _ellipsize(draw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def place_colour(place: int) -> Tuple[int, int, int]:
    """Paint colour of a place: the fire rank it holds (1st Inferno .. 5th Spark)."""
    rank = 7 - place if 1 <= place <= 5 else 1
    return ranks.RANKS[rank].rgb


def _shade(rgb, factor: float) -> Tuple[int, int, int]:
    if factor >= 1:
        return tuple(int(c + (255 - c) * (factor - 1)) for c in rgb)
    return tuple(int(c * factor) for c in rgb)


def _tint(img: Image.Image, mask: Image.Image, box: Box, rgb) -> None:
    """Recolour the grey paint inside `box` (template brightness 160 = `rgb`)."""
    region = img.crop(box)
    grey = region.convert("L")
    coloured = ImageOps.colorize(grey, black=(14, 12, 10), mid=rgb, white=_shade(rgb, 1.45),
                                 blackpoint=0, midpoint=160, whitepoint=255)
    img.paste(coloured, box[:2], mask.crop(box))


def _ribbon(img: Image.Image, stamp: Image.Image, box: Box, rgb) -> None:
    """Stamp the brush-stroke ribbon, painted `rgb` with some grain."""
    x0, y0, x1, y1 = box
    size = (x1 - x0, y1 - y0)
    shape = stamp.resize(size, Image.LANCZOS)
    grain = Image.effect_noise(size, 26).filter(ImageFilter.GaussianBlur(0.6))
    paint = ImageOps.colorize(grain, black=_shade(rgb, 0.72), mid=rgb, white=_shade(rgb, 1.25))
    img.paste(paint, (x0, y0), shape)


def _bar(draw: ImageDraw.ImageDraw, box: Box, fraction: float, rgb) -> None:
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=6, fill=(20, 19, 18), outline=(128, 124, 118), width=2)
    fraction = max(0.0, min(1.0, fraction))
    if fraction <= 0:
        return
    fx1 = x0 + 4 + int((x1 - x0 - 8) * fraction)
    if fx1 - (x0 + 4) < 8:
        fx1 = x0 + 12
    draw.rounded_rectangle((x0 + 4, y0 + 4, fx1, y1 - 4), radius=4, fill=rgb)
    # Highlight along the top, like the art's bars.
    mid = y0 + 4 + (y1 - y0 - 8) // 3
    draw.rounded_rectangle((x0 + 6, y0 + 6, fx1 - 2, mid), radius=3, fill=_shade(rgb, 1.25))


def _spaced(draw: ImageDraw.ImageDraw, centre: Tuple[int, int], text: str, font, fill,
            spacing: float = 3.0) -> None:
    """Letter-spaced text centred on `centre` (the art's "REPUTATION" line)."""
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = sum(widths) + spacing * (len(text) - 1)
    x = centre[0] - total / 2
    for ch, w in zip(text, widths):
        draw.text((x, centre[1]), ch, font=font, fill=fill, anchor="lm")
        x += w + spacing


def _photo(avatars: Sequence[bytes], size: Tuple[int, int], label: str) -> Image.Image:
    """The polaroid picture: one avatar, a 2x2 of up to four, or "no photo"."""
    pics = []
    for raw in avatars[:4]:
        try:
            pic = Image.open(io.BytesIO(raw))
            pic.seek(0)
            pics.append(pic.convert("RGB"))
        except Exception:
            continue
    w, h = size
    if not pics:
        return _no_photo(size, label)
    if len(pics) == 1:
        photo = ImageOps.fit(pics[0], size, Image.LANCZOS, centering=(0.5, 0.4))
    else:
        photo = Image.new("RGB", size, (38, 37, 35))
        cols = 2
        rows = 1 if len(pics) == 2 else 2
        cw, ch = w // cols, h // rows
        for i, pic in enumerate(pics):
            cell = ImageOps.fit(pic, (cw - 2, ch - 2), Image.LANCZOS)
            photo.paste(cell, ((i % cols) * cw + 1, (i // cols) * ch + 1))
    # Weathered print: a little faded and grainy, still in colour.
    photo = Image.blend(photo, ImageOps.colorize(ImageOps.grayscale(photo), (24, 22, 20), (232, 222, 204)), 0.25)
    grain = Image.effect_noise(size, 18).convert("RGB")
    return Image.blend(photo, grain, 0.05)


def _no_photo(size: Tuple[int, int], label: str) -> Image.Image:
    w, h = size
    img = Image.new("RGB", size, (38, 37, 35))
    d = ImageDraw.Draw(img)
    shade = (62, 60, 57)
    cx = w // 2
    d.ellipse([cx - 24, 22, cx + 24, 78], fill=shade)
    d.rounded_rectangle([cx - 48, 84, cx + 48, h + 20], radius=36, fill=shade)
    font = _fit(d, label, _OSWALD, 14, w - 10, 9, "Bold")
    d.text((cx, h - 14), label, font=font, fill=(150, 146, 138), anchor="mm")
    return img


def render_board(board: Dict, avatars: Optional[Dict[str, bytes]] = None) -> Image.Image:
    """Draw a `bt_progression.reputation_board(...)` result.

    `avatars` maps a PZ username to the raw bytes of its linked Discord avatar.
    """
    avatars = avatars or {}
    img = Image.open(_TEMPLATE).convert("RGB")
    mask = Image.open(_TINT).convert("L")
    stamp = Image.open(_RIBBON).convert("L")
    draw = ImageDraw.Draw(img)

    faction = board.get("kind") == "faction"
    period = board.get("period") or "week"

    if period == "lastweek":
        header = "Last Week's Top Factions" if faction else "Last Week's Winners"
    else:
        header = "Top 5 Factions" if faction else "Top 5 Survivors"
    font = _fit(draw, header, _MARKER, 44, _HEADER_MAX_W, 28)
    draw.text(_HEADER_CENTRE, header, font=font, fill=_INK, anchor="mm")

    rows = {r["place"]: r for r in board.get("rows") or [] if 1 <= r.get("place", 0) <= 5}
    top = max([r["value"] for r in rows.values()] + [0])
    label = {"week": "RP THIS WEEK", "lastweek": "RP LAST WEEK"}.get(period, "REPUTATION")
    if faction:
        label = "FACTION " + label

    for place, geo in enumerate(ROWS, 1):
        rgb = place_colour(place)
        row = rows.get(place)
        _tint(img, mask, geo["tile"], rgb)
        _tint(img, mask, geo["medal"], rgb)
        _ribbon(img, stamp, geo["ribbon"], rgb)
        draw = ImageDraw.Draw(img)

        # Portrait.
        px0, py0, px1, py1 = geo["photo"]
        pics = [avatars[p] for p in (row or {}).get("players", []) if p in avatars]
        no_photo = "NO PHOTO" if row else "OPEN SPOT"
        img.paste(_photo(pics, (px1 - px0, py1 - py0), no_photo), (px0, py0))

        # Name.
        name = row["name"] if row else "—"
        font = _fit(draw, name.upper(), _ANTON, 40, _NAME_MAX_W, 24)
        draw.text((geo["name_x"], geo["name_y"]), _ellipsize(draw, name.upper(), font, _NAME_MAX_W),
                  font=font, fill=_INK, anchor="lm")

        # Title on the ribbon.
        rx0, ry0, rx1, ry1 = geo["ribbon"]
        title = ((row or {}).get("title") or ("No title" if row else "Unclaimed")).upper()
        tmax = rx1 - rx0 - 36
        font = _fit(draw, title, _OSWALD, 20, tmax, 12, "Bold")
        draw.text(((rx0 + rx1) // 2, (ry0 + ry1) // 2), _ellipsize(draw, title, font, tmax),
                  font=font, fill=_INK, anchor="mm")

        # Bar, score and its label.
        value = row["value"] if row else 0
        _bar(draw, geo["bar"], (value / top) if top > 0 else 0.0, rgb)
        text = f"{value:,}" if row else "—"
        font = _fit(draw, text, _ANTON, 66, _VALUE_MAX_W, 30)
        draw.text(geo["value"], text, font=font, fill=rgb, anchor="mm")
        lfont = _fit(draw, label, _OSWALD, 15, 160, 10, "Medium")
        _spaced(draw, geo["label"], label, lfont, _LABEL, spacing=2.6 if len(label) < 14 else 1.2)

    return img


def render_board_png(board: Dict, avatars: Optional[Dict[str, bytes]] = None) -> io.BytesIO:
    buf = io.BytesIO()
    render_board(board, avatars).save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
