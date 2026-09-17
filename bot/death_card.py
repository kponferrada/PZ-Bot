"""death_card.py — render a death notification onto assets/cause-of-death.png.

The card is a dark "DEATH NOTIFICATION" template with ghost `{placeholder}` text
slots. This module covers each slot and draws the real value, truncating anything
that would overflow its slot, then pastes the matching injury icon (droplet /
slash / scratch / bite) onto the paper-doll silhouette from the parsed
`Injuries:` list.
"""

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

_ASSETS = Path(__file__).parent / "assets"
_CARD_PATH = _ASSETS / "cause-of-death.png"
_FONT_PATH = _ASSETS / "fonts" / "LiberationSans-Bold.ttf"

# Card colours (sampled from the template).
_BG = (1, 12, 17)            # dark card background behind the value slots
_TEXT = (230, 230, 231)      # bright value text (matches the title)
_COUNT_TEXT = (250, 250, 250)

# Value slots: field -> (x_left, y_top, max_width_px, font_size_px)
_SLOTS = {
    "survivor":       (371, 284, 250, 18),
    "character_name": (371, 331, 250, 18),
    "infected":       (371, 383, 120, 18),
    "survival_time":  (372, 436, 250, 18),
    "zombie_kills":   (371, 490, 180, 18),
    "cause":          (371, 643, 280, 17),
    "injuries":       (372, 708, 300, 15),
    "location":       (372, 869, 300, 15),
    "game_date_time": (373, 929, 240, 15),
}

# Death-count box (the big number area) — left/top/right/bottom.
_COUNT_BOX = (850, 290, 1080, 468)

# Paper-doll body-part marker positions (image pixel coordinates).
# The doll faces the reader, so the character's LEFT side is on the viewer's
# RIGHT (higher x) and the character's RIGHT side on the viewer's LEFT.
_BODY_PARTS: Dict[str, Tuple[int, int]] = {
    "head":        (876, 658),
    "neck":        (876, 688),
    "torso_upper": (876, 725),
    "torso_lower": (876, 800),
    "groin":       (876, 852),
    "upperarm_l":  (917, 725),   # character's left arm → viewer's right
    "forearm_l":   (930, 775),
    "hand_l":      (946, 815),
    "upperarm_r":  (830, 725),   # character's right arm → viewer's left
    "forearm_r":   (818, 775),
    "hand_r":      (807, 815),
    "leg_l":       (905, 915),
    "foot_l":      (906, 958),
    "leg_r":       (843, 915),
    "foot_r":      (838, 958),
}

# Injury icons live in the template's legend (bottom-right "Injury Overview").
# Each condition maps to one of the four legend icons, which we crop out of the
# template and paste onto the paper doll at the matching body part.
_ICON_SOURCES = {
    "bleeding":  (988, 632, 1024, 682),   # red droplet
    "cut":       (985, 690, 1033, 743),   # 3 deep-red slanted lines
    "scratched": (987, 754, 1028, 801),   # 3 light-red lines
    "bitten":    (987, 811, 1032, 859),   # bite mark
}

# Injury condition -> legend icon key.
_CONDITION_ICONS = {
    "bitten":     "bitten",
    "bleeding":   "bleeding",
    "deep wound": "cut",
    "scratched":  "scratched",
    "cut":        "cut",
}

_ICON_SIZE = 14  # marker height on the paper doll (px)

_icons_cache: Optional[Dict[str, Image.Image]] = None


def _load_icons() -> Dict[str, Image.Image]:
    """Crop the four injury icons out of the template legend (transparent bg)."""
    global _icons_cache
    if _icons_cache is not None:
        return _icons_cache
    template = Image.open(_CARD_PATH).convert("RGB")
    icons: Dict[str, Image.Image] = {}
    for name, box in _ICON_SOURCES.items():
        icon = template.crop(box).convert("RGBA")
        ip = icon.load()
        for y in range(icon.height):
            for x in range(icon.width):
                r, g, b, _a = ip[x, y]
                # Keep red-dominant icon pixels and bright white (bite-mark
                # teeth); make the dark card background transparent.
                if (r > 40 and r > g * 1.2 and r > b * 1.2) or (r > 150 and g > 150 and b > 150):
                    ip[x, y] = (r, g, b, 255)
                else:
                    ip[x, y] = (0, 0, 0, 0)
        scale = _ICON_SIZE / icon.height
        icon = icon.resize((max(1, round(icon.width * scale)), _ICON_SIZE), Image.LANCZOS)
        icons[name] = icon
    _icons_cache = icons
    return icons


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_FONT_PATH), size)


def _clear_white_text(img: Image.Image, box: tuple) -> None:
    """Inpaint white placeholder text in `box` with the local card background.

    The `{death count}` placeholder is thick distressed-white text over the red
    splatter. A median filter is too weak for it, so we do a horizontal scanline
    fill: each run of white pixels is replaced with an interpolation of the
    nearest non-white pixel to its left and right, which reconstructs the
    splatter reasonably.
    """
    l, t, r, b = box
    region = img.crop(box).convert("RGB")
    px = region.load()
    w, h = region.size

    def _is_white(c) -> bool:
        return c[0] > 150 and c[1] > 150 and c[2] > 150

    for y in range(h):
        x = 0
        while x < w:
            if _is_white(px[x, y]):
                run_end = x
                while run_end < w and _is_white(px[run_end, y]):
                    run_end += 1
                left = None
                for xx in range(x - 1, -1, -1):
                    if not _is_white(px[xx, y]):
                        left = px[xx, y]
                        break
                right = None
                for xx in range(run_end, w):
                    if not _is_white(px[xx, y]):
                        right = px[xx, y]
                        break
                for xx in range(x, run_end):
                    if left is not None and right is not None:
                        frac = (xx - x + 1) / (run_end - x + 1)
                        px[xx, y] = tuple(
                            round(left[i] * (1 - frac) + right[i] * frac) for i in range(3))
                    elif left is not None:
                        px[xx, y] = left
                    elif right is not None:
                        px[xx, y] = right
                x = run_end
            else:
                x += 1

    img.paste(region, (l, t))


def _fit_number(draw: ImageDraw.ImageDraw, text: str, max_width: int,
                start_size: int) -> ImageFont.FreeTypeFont:
    """Pick the largest font (down to 24) that keeps `text` within `max_width`."""
    size = start_size
    while size > 24:
        font = _font(size)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 4
    return _font(24)


def _truncate(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    """Shorten `text` (appending an ellipsis) until it fits `max_width` px."""
    text = text or ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _normalize_part(name: str) -> Optional[str]:
    """Map a Project Zomboid BodyPartType name to a `_BODY_PARTS` key."""
    n = name.strip().lower().replace(" ", "")
    if not n:
        return None
    left = ("left" in n) or n.endswith("_l")
    right = ("right" in n) or n.endswith("_r")

    if "head" in n:
        return "head"
    if "neck" in n:
        return "neck"
    if "groin" in n:
        return "groin"
    if "torso" in n or "back" in n:
        return "torso_lower" if "lower" in n else "torso_upper"
    if "hand" in n:
        return "hand_l" if left else ("hand_r" if right else None)
    if "forearm" in n or "fore" in n:
        return "forearm_l" if left else ("forearm_r" if right else None)
    if "arm" in n:
        return "upperarm_l" if left else ("upperarm_r" if right else None)
    if "foot" in n:
        return "foot_l" if left else ("foot_r" if right else None)
    if any(k in n for k in ("shin", "thigh", "lowerleg", "upperleg", "leg")):
        return "leg_l" if left else ("leg_r" if right else None)
    return None


def _parse_injuries(injuries: str) -> List[Tuple[str, List[str]]]:
    """Parse `Injuries:` text into [(body_part_key, [condition, ...]), ...]."""
    raw = (injuries or "").strip()
    if not raw or raw.lower() in ("none", "n/a", "-"):
        return []
    result: List[Tuple[str, List[str]]] = []
    for seg in raw.split(";"):
        seg = seg.strip()
        if not seg:
            continue
        part, _, conds = seg.partition(":")
        key = _normalize_part(part)
        if not key:
            continue
        cond_list = [c.strip().lower() for c in conds.split(",") if c.strip()]
        result.append((key, cond_list))
    return result


def _draw_centered(draw: ImageDraw.ImageDraw, center: Tuple[int, int],
                   text: str, font, fill) -> None:
    draw.text(center, text, font=font, fill=fill, anchor="mm")


def render_death_card(data: Dict) -> Image.Image:
    """Render the death card from a dict of field values.

    Expected keys: survivor, character_name, infected, survival_time,
    zombie_kills, cause, injuries, location, game_date_time, death_count.
    Returns a new PIL Image (the original template is never mutated).
    """
    img = Image.open(_CARD_PATH).convert("RGBA")
    draw = ImageDraw.Draw(img)

    # 1. Fill each value slot: cover the ghost placeholder, draw the value.
    for field, (x, y, max_w, size) in _SLOTS.items():
        value = str(data.get(field, "") or "").strip()
        # Cover a generous box: the placeholder spans ~x..x+max_w and ~30px tall.
        cover_h = 34 if size >= 17 else 28
        draw.rectangle([x - 4, y - 6, x + max_w + 8, y + cover_h], fill=_BG)
        if not value:
            continue
        font = _font(size)
        value = _truncate(draw, value, font, max_w)
        draw.text((x, y - 3), value, font=font, fill=_TEXT)

    # 2. Death count (big number, transparent background — no box).
    count = str(data.get("death_count", "") or "0").strip()
    l, t, r, b = _COUNT_BOX
    _clear_white_text(img, (l, t, r, b))
    if count:
        cfont = _fit_number(draw, count, r - l - 24, 88)
        _draw_centered(draw, ((l + r) // 2, (t + b) // 2), count, cfont, _COUNT_TEXT)

    # 3. Injury markers on the paper doll (legend icons).
    icons = _load_icons()
    spacing = _ICON_SIZE + 3
    for key, conds in _parse_injuries(str(data.get("injuries", "") or "")):
        pos = _BODY_PARTS.get(key)
        if pos is None:
            continue
        n = len(conds) or 1
        for i, cond in enumerate(conds):
            icon = icons.get(_CONDITION_ICONS.get(cond, ""))
            if icon is None:
                continue  # unknown condition — no icon to draw
            # Stack multiple conditions on the same part vertically, centred.
            cy = pos[1] + (i - (n - 1) / 2) * spacing
            px = pos[0] - icon.width // 2
            py = int(cy) - icon.height // 2
            img.paste(icon, (px, py), icon)

    return img
