"""death_card.py — render a death notification onto assets/cause-of-death.png.

The card is a dark "DEATH NOTIFICATION" template with ghost `{placeholder}` text
slots. This module covers each slot and draws the real value, truncating anything
that would overflow its slot, then draws coloured injury markers onto the paper-
doll silhouette from the parsed `Injuries:` list.
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
_COUNT_BG = (6, 6, 6)        # death-count box background
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
_BODY_PARTS: Dict[str, Tuple[int, int]] = {
    "head":        (880, 660),
    "neck":        (880, 680),
    "torso_upper": (888, 718),
    "torso_lower": (888, 752),
    "groin":       (888, 778),
    "upperarm_l":  (846, 712),
    "forearm_l":   (845, 745),
    "hand_l":      (844, 782),
    "upperarm_r":  (934, 712),
    "forearm_r":   (935, 745),
    "hand_r":      (936, 782),
    "leg_l":       (862, 812),
    "foot_l":      (862, 948),
    "leg_r":       (910, 812),
    "foot_r":      (910, 948),
}

# Injury condition -> marker colour.
_CONDITION_COLORS = {
    "bitten":     (228, 32, 32),
    "bleeding":   (150, 12, 12),
    "deep wound": (160, 34, 150),
    "scratched":  (232, 205, 58),
    "cut":        (235, 130, 30),
}

_MARKER_RADIUS = 6


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_FONT_PATH), size)


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
    box = draw.textbbox((0, 0), text, font=font)
    w = box[2] - box[0]
    h = box[3] - box[1]
    draw.text((center[0] - w // 2, center[1] - h // 2), text, font=font, fill=fill)


def render_death_card(data: Dict) -> Image.Image:
    """Render the death card from a dict of field values.

    Expected keys: survivor, character_name, infected, survival_time,
    zombie_kills, cause, injuries, location, game_date_time, death_count.
    Returns a new PIL Image (the original template is never mutated).
    """
    img = Image.open(_CARD_PATH).convert("RGB")
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

    # 2. Death count (big number).
    count = str(data.get("death_count", "") or "0").strip()
    l, t, r, b = _COUNT_BOX
    draw.rectangle([l, t, r, b], fill=_COUNT_BG)
    if count:
        cfont = _font(52)
        count = _truncate(draw, count, cfont, r - l - 16)
        _draw_centered(draw, ((l + r) // 2, (t + b) // 2), count, cfont, _COUNT_TEXT)

    # 3. Injury markers on the paper doll.
    for key, conds in _parse_injuries(str(data.get("injuries", "") or "")):
        pos = _BODY_PARTS.get(key)
        if pos is None:
            continue
        # Stack multiple conditions on the same part vertically.
        offsets = []
        n = len(conds) or 1
        for i in range(n):
            offsets.append((pos[0], pos[1] + (i - (n - 1) / 2) * (_MARKER_RADIUS * 2 + 2)))
        for i, cond in enumerate(conds):
            colour = _CONDITION_COLORS.get(cond)
            if colour is None:
                # Unknown condition — fall back to a neutral marker.
                colour = (200, 200, 200)
            cx, cy = offsets[i]
            draw.ellipse(
                [cx - _MARKER_RADIUS, cy - _MARKER_RADIUS,
                 cx + _MARKER_RADIUS, cy + _MARKER_RADIUS],
                fill=colour, outline=(255, 255, 255), width=2,
            )

    return img
