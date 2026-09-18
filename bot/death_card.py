"""death_card.py — render a death notification onto assets/cause-of-death.png.

The card is a dark "DEATH NOTIFICATION" template with ghost `{placeholder}` text
slots. This module covers each slot and draws the real value (in Exo 2), then
pastes the matching injury icon (bleeding / cut / scratched / bitten / bruised /
fractured) onto the paper-doll silhouette from the parsed `Injuries:` list.

The "DEATH COUNT" panel holds a big total number (drawn over the blood splatter)
and three stat boxes — TODAY / THIS WEEK / ALL TIME — whose placeholder dots are
replaced with the real counts.
"""

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

_ASSETS = Path(__file__).parent / "assets"
_CARD_PATH = _ASSETS / "cause-of-death.png"
_FONT_PATH = _ASSETS / "fonts" / "Exo2.ttf"

# Card colours (sampled from the template).
_BG = (1, 12, 17)            # dark card background behind the value slots
_TEXT = (230, 230, 231)      # bright value text (matches the title)
_COUNT_TEXT = (250, 250, 250)

# Value slots: field -> (x_left, y_top, max_width_px, font_size_px)
_SLOTS = {
    "survivor":       (398, 298, 250, 18),
    "character_name": (398, 355, 250, 18),
    "infected":       (398, 414, 120, 18),
    "survival_time":  (399, 475, 250, 18),
    "zombie_kills":   (398, 535, 180, 18),
    "cause":          (400, 709, 280, 17),
    "injuries":       (400, 776, 300, 15),
    "location":       (401, 959, 300, 15),
    "game_date_time": (401, 1030, 240, 15),
}

# Injuries wrap onto multiple lines so long lists fit instead of truncating.
# The block shrinks its font until everything fits the "CAUSE OF DEATH &
# INJURIES" panel (which ends just above the "LOCATION & DATE" header ~y 874).
_INJURIES_MAX_H = 90         # vertical px available for the injuries block
_INJURIES_MIN_SIZE = 11      # smallest font before we give up and truncate

# Death count: the big total number sits centred on the blood splatter; the
# white circle placeholder is inpainted away first.
_COUNT_CENTER = (1072, 393)
_COUNT_MAX_W = 220          # horizontal room for the big number
_COUNT_MAX_SIZE = 76        # largest font for the big number

# Two smaller death-count cards: box -> (centre_x, count_y). The count replaces
# the placeholder dot. No background is drawn — just the number (low-key).
_STAT_BOXES = {
    "today": (965, 535),
    "week":  (1229, 535),
}
_STAT_SIZE = 24             # font size for the box counts (matches the card height)

# Paper-doll body-part marker positions (image pixel coordinates).
# The doll faces the reader, so the character's LEFT side is on the viewer's
# RIGHT (higher x) and the character's RIGHT side on the viewer's LEFT.
_BODY_PARTS: Dict[str, Tuple[int, int]] = {
    "head":        (932, 730),
    "neck":        (932, 765),
    "torso_upper": (930, 800),
    "torso_lower": (930, 860),
    "groin":       (930, 900),
    "upperarm_l":  (983, 810),   # character's left arm → viewer's right
    "forearm_l":   (1003, 860),
    "hand_l":      (1007, 880),
    "upperarm_r":  (875, 810),   # character's right arm → viewer's left
    "forearm_r":   (855, 860),
    "hand_r":      (848, 880),
    "leg_l":       (956, 940),
    "foot_l":      (965, 1000),
    "leg_r":       (901, 940),
    "foot_r":      (890, 1000),
}

# Injury icons live in the template's legend (right of the paper doll). Each
# condition maps to one legend icon, which we crop out of the template and paste
# onto the paper doll at the matching body part.
# NOTE: x_start was 1120, which clipped the left edge of every icon (the
# fractured bone lost half its width). The icons actually start at x≈1098.
_ICON_SOURCES = {
    "bleeding":  (1095, 708, 1152, 757),   # red droplet
    "cut":       (1095, 768, 1160, 820),   # 3 deep-red slanted lines
    "scratched": (1095, 834, 1154, 881),   # 3 light-red lines
    "bitten":    (1095, 900, 1156, 948),   # bite mark
    "fractured": (1095, 971, 1152, 1007),  # bone
}

# Injury condition -> legend icon key.
_CONDITION_ICONS = {
    "bitten":     "bitten",
    "bite":       "bitten",
    "bleeding":   "bleeding",
    "deep wound": "cut",
    "deepwound":  "cut",
    "scratched":  "scratched",
    "scratch":    "scratched",
    "cut":        "cut",
    "laceration": "cut",
    "burn":       "cut",
    "burned":     "cut",
    "fractured":  "fractured",
    "fracture":   "fractured",
    "broken bone": "fractured",
}

# Human-readable labels for the parsed body-part keys (for the injuries text).
_PART_LABELS = {
    "head":        "Head",
    "neck":        "Neck",
    "torso_upper": "Upper torso",
    "torso_lower": "Lower torso",
    "groin":       "Groin",
    "upperarm_l":  "Left upper arm",
    "forearm_l":   "Left forearm",
    "hand_l":      "Left hand",
    "upperarm_r":  "Right upper arm",
    "forearm_r":   "Right forearm",
    "hand_r":      "Right hand",
    "leg_l":       "Left leg",
    "foot_l":      "Left foot",
    "leg_r":       "Right leg",
    "foot_r":      "Right foot",
}

_ICON_SIZE = 16  # marker height on the paper doll (px)

_icons_cache: Optional[Dict[str, Image.Image]] = None


def _load_icons() -> Dict[str, Image.Image]:
    """Crop the six injury icons out of the template legend (transparent bg)."""
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
                # teeth / bone highlights); make the dark card background
                # transparent.
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
    font = ImageFont.truetype(str(_FONT_PATH), size)
    try:
        font.set_variation_by_axes([700])  # Bold
    except Exception:
        pass
    return font


def _clear_white_text(img: Image.Image, box: tuple) -> None:
    """Inpaint white pixels in `box` with the local background.

    Each run of white pixels on a row is replaced with an interpolation of the
    nearest non-white pixel to its left and right, reconstructing the splatter /
    background underneath a ghost placeholder.
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
    """Pick the largest font (down to 20) that keeps `text` within `max_width`."""
    size = start_size
    while size > 20:
        font = _font(size)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 4
    return _font(20)


def _truncate(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    """Shorten `text` (appending an ellipsis) until it fits `max_width` px."""
    text = text or ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _normalize_part(name: str) -> Optional[str]:
    """Map a Project Zomboid BodyPartType name to a `_BODY_PARTS` key.

    Accepts the enum-style names the Death Log mod emits (``Hand_L``,
    ``ForeArm_R``, ``UpperLeg_L`` …) as well as human phrases (``Left hand``,
    ``right forearm``). ``Hand_L`` == left hand, and on the doll the character's
    left is the viewer's right (handled by `_BODY_PARTS` coordinates).
    """
    n = (name or "").strip().lower().replace(" ", "").replace("-", "").replace(".", "")
    if not n:
        return None
    left = n.endswith("_l") or n.startswith("left") or "left" in n
    right = n.endswith("_r") or n.startswith("right") or "right" in n

    if "head" in n:
        return "head"
    if "neck" in n:
        return "neck"
    if "groin" in n:
        return "groin"
    if "torso" in n or "back" in n or "chest" in n or "stomach" in n:
        return "torso_lower" if ("lower" in n or "stomach" in n) else "torso_upper"
    if "hand" in n:
        return "hand_l" if left else ("hand_r" if right else None)
    if "forearm" in n or "fore" in n:
        return "forearm_l" if left else ("forearm_r" if right else None)
    if "arm" in n or "shoulder" in n:
        return "upperarm_l" if left else ("upperarm_r" if right else None)
    if "foot" in n:
        return "foot_l" if left else ("foot_r" if right else None)
    if any(k in n for k in ("shin", "thigh", "lowerleg", "upperleg", "leg", "knee")):
        return "leg_l" if left else ("leg_r" if right else None)
    return None


def _split_injury_segment(seg: str) -> Tuple[str, str]:
    """Split one `Injuries:` segment into (body_part, conditions_string).

    The Death Log mod has emitted two formats over time:
      old (raw name + colon):   "Hand_R: Bleeding, Deep Wound"
      new (friendly + parens):  "Left Hand (Bleeding, Deep Wound)"
    Both are handled here.
    """
    seg = (seg or "").strip()
    if "(" in seg:
        part, _, rest = seg.partition("(")
        return part.strip(), rest.rstrip(")").strip()
    if ":" in seg:
        part, _, conds = seg.partition(":")
        return part.strip(), conds.strip()
    return seg, ""


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
        part, conds = _split_injury_segment(seg)
        key = _normalize_part(part)
        if not key:
            continue
        cond_list = [c.strip().lower() for c in conds.split(",") if c.strip()]
        result.append((key, cond_list))
    return result


def _humanize_injuries(injuries: str) -> str:
    """Rewrite `Hand_L: Fractured` / `Left Hand (Fracture)` -> `Left hand: Fracture`."""
    raw = (injuries or "").strip()
    if not raw or raw.lower() in ("none", "n/a", "-"):
        return raw or ""
    parts: List[str] = []
    for seg in raw.split(";"):
        seg = seg.strip()
        if not seg:
            continue
        part, conds = _split_injury_segment(seg)
        key = _normalize_part(part)
        label = _PART_LABELS.get(key, part.strip()) if key else part.strip()
        parts.append(f"{label}: {conds}" if conds else label)
    return "; ".join(parts)


def _wrap_injuries(draw: ImageDraw.ImageDraw, text: str, max_w: int):
    """Wrap injuries into (lines, font, line_height) that fit the injuries block.

    Starts at the slot's base font size and shrinks it until the wrapped lines
    fit within `_INJURIES_MAX_H`, down to `_INJURIES_MIN_SIZE`. Lines break at
    '; ' boundaries so each injury stays intact; if even the smallest font is
    still too long, the last line is truncated with an ellipsis.
    """
    segments = [s.strip() for s in (text or "").split(";") if s.strip()]

    def _lines_for(size: int):
        font = _font(size)
        lh = size + 3
        lines: List[str] = []
        cur = ""
        for seg in segments:
            cand = f"{cur}; {seg}" if cur else seg
            if draw.textlength(cand, font=font) <= max_w:
                cur = cand
            else:
                if cur:
                    lines.append(cur)
                cur = seg
        if cur:
            lines.append(cur)
        return lines, font, lh

    base = _SLOTS["injuries"][3]
    for size in range(base, _INJURIES_MIN_SIZE - 1, -1):
        lines, font, lh = _lines_for(size)
        if len(lines) * lh <= _INJURIES_MAX_H:
            return lines, font, lh

    # Smallest font still overflows — truncate the last line to fit.
    lines, font, lh = _lines_for(_INJURIES_MIN_SIZE)
    max_lines = max(1, _INJURIES_MAX_H // lh)
    lines = lines[:max_lines]
    last = lines[-1]
    while last and draw.textlength(last + "…", font=font) > max_w:
        last = last[:-1]
    lines[-1] = last + "…"
    return lines, font, lh


def _draw_centered(draw: ImageDraw.ImageDraw, center: Tuple[int, int],
                   text: str, font, fill) -> None:
    draw.text(center, text, font=font, fill=fill, anchor="mm")


def _draw_stat_boxes(draw: ImageDraw.ImageDraw, img: Image.Image,
                     counts: Dict[str, int]) -> None:
    """Replace each stat-box dot with its count number (no background)."""
    font = _font(_STAT_SIZE)
    for box, (cx, cy) in _STAT_BOXES.items():
        value = str(counts.get(box, 0) or 0)
        # Inpaint the placeholder dot, then draw just the number (low-key).
        _clear_white_text(img, (cx - 12, cy - 12, cx + 12, cy + 12))
        _draw_centered(draw, (cx, cy), value, font, _COUNT_TEXT)


def render_death_card(data: Dict) -> Image.Image:
    """Render the death card from a dict of field values.

    Expected keys: survivor, character_name, infected, survival_time,
    zombie_kills, cause, injuries, location, game_date_time, death_count,
    deaths_today, deaths_week.
    Returns a new PIL Image (the original template is never mutated).
    """
    img = Image.open(_CARD_PATH).convert("RGBA")
    draw = ImageDraw.Draw(img)

    # 1. Fill each value slot: cover the ghost placeholder, draw the value.
    for field, (x, y, max_w, size) in _SLOTS.items():
        value = str(data.get(field, "") or "").strip()
        if field == "injuries":
            value = _humanize_injuries(value)
        cover_h = 34 if size >= 17 else 28
        draw.rectangle([x - 4, y - 6, x + max_w + 8, y + cover_h], fill=_BG)
        if not value:
            continue
        if field == "injuries":
            # Wrap long injury lists onto multiple lines instead of truncating.
            lines, ifont, lh = _wrap_injuries(draw, value, max_w)
            for i, line in enumerate(lines):
                draw.text((x, y - 3 + i * lh), line, font=ifont, fill=_TEXT)
        else:
            font = _font(size)
            value = _truncate(draw, value, font, max_w)
            draw.text((x, y - 3), value, font=font, fill=_TEXT)

    # 2. Big total death count on the splatter (transparent — no box).
    count = str(data.get("death_count", "") or "0").strip()
    cx, cy = _COUNT_CENTER
    _clear_white_text(img, (cx - 14, cy - 14, cx + 14, cy + 14))
    if count:
        cfont = _fit_number(draw, count, _COUNT_MAX_W, _COUNT_MAX_SIZE)
        _draw_centered(draw, (cx, cy), count, cfont, _COUNT_TEXT)

    # 3. Two smaller cards: TODAY / THIS WEEK.
    _draw_stat_boxes(draw, img, {
        "today": data.get("deaths_today", 0),
        "week": data.get("deaths_week", 0),
    })

    # 4. Injury markers on the paper doll (legend icons).
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
            cy = pos[1] + (i - (n - 1) / 2) * spacing
            px = pos[0] - icon.width // 2
            py = int(cy) - icon.height // 2
            img.paste(icon, (px, py), icon)

    return img
