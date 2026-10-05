"""death_card.py — render a death notification as a Barangay Tambayan death certificate.

The template (assets/death-certificate.png, built from the source art by
scripts/build_death_certificate.py) is a blank "Official Death Certificate and
Autopsy Report". This module types the values into its fields, puts the
survivor's Discord avatar in the polaroid, and paints the parsed `Injuries:`
onto the two paper dolls of the injury diagram.

Everything here is pure Pillow, so it runs without Discord (see
tests/test_death_card.py). Rendering takes a moment; call it off the event loop.
"""

import datetime
import io
import re
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

_ASSETS = Path(__file__).parent / "assets"
_CARD_PATH = _ASSETS / "death-certificate.png"
_FONTS = _ASSETS / "fonts"
_MONO = _FONTS / "IBMPlexMono-Regular.ttf"
_MONO_BOLD = _FONTS / "IBMPlexMono-Bold.ttf"
_SLAB_BOLD = _FONTS / "RobotoSlab-Bold.ttf"

_TZ = datetime.timezone(datetime.timedelta(hours=8))  # PHT, as in death_store

# Typewriter ink (sampled from the source art).
_INK = (32, 28, 24)
_RED = (142, 28, 26)
_FADED = (110, 104, 96)

# Single-line value fields: name -> (x_left, y_cap_centre, max_width, size).
# y is the vertical centre of a capital letter, so values sit on the ruled rows
# whatever the font size.
_FIELDS: Dict[str, Tuple[int, int, int, int]] = {
    # Header
    "registry_no":   (952, 66, 162, 13),
    "date_issued":   (952, 94, 162, 13),
    # DECEASED
    "character_name": (338, 327, 222, 18),
    "username":       (338, 360, 222, 18),
    "infection":      (338, 393, 222, 18),
    # SURVIVAL RECORD
    "survival_time":  (338, 470, 222, 16),
    "zombie_kills":   (338, 502, 222, 16),
    "server_deaths":  (338, 533, 222, 16),
    "deaths_today":   (338, 565, 222, 16),
    "deaths_week":    (338, 596, 222, 16),
    # DEATH INFORMATION
    "game_date_time": (376, 672, 420, 15),
    "location":       (376, 704, 420, 15),
    "cause":          (376, 734, 420, 15),
    # SUBJECT
    "s_character_name": (1007, 389, 203, 14),
    "s_username":       (1007, 419, 203, 14),
    "s_registry_no":    (1007, 449, 203, 14),
    # FORENSIC FINDINGS
    "f_cause":          (1064, 519, 148, 14),
    "f_infection":      (1064, 553, 148, 14),
    # DEATH DETAILS
    "real_date_time":   (1082, 700, 215, 14),
    "d_location":       (1082, 729, 215, 14),
}

# Multi-line blocks: (x_left, first_cap_centre_y, max_width, size, line_h, max_lines)
_INJURY_LIST = (376, 764, 420, 15, 18, 4)        # DEATH INFORMATION
_INJURY_SUMMARY = (1064, 585, 148, 13, 17, 4)    # FORENSIC FINDINGS (bullets)
_NOTES = (899, 794, 386, 13, 15, 3)              # AUTOPSY NOTES
_CERT = (85, 876, 578, 12, 15.5, 4)              # CERTIFICATION paragraph

# FINAL DETERMINATION box: centre x, headline/subline centres, width.
_FINAL_CX = 1062
_FINAL_Y = (893, 921)
_FINAL_W = 360

_CERT_TEXT = (
    "This document certifies that the above-mentioned survivor has been "
    "recorded as DECEASED in the official records of Barangay Tambayan. This "
    "record was automatically generated from the survivor's recorded death "
    "event and is maintained as part of the Barangay Tambayan Civil Registry."
)

# Polaroid window (matches PHOTO_BOX in scripts/build_death_certificate.py).
_PHOTO_BOX = (583, 318, 774, 543)

# ---------------------------------------------------------------------------
# Injury diagram
# ---------------------------------------------------------------------------

# Paper-doll anchor points. The front doll faces the reader, so the
# character's LEFT is on the viewer's RIGHT; the back doll is seen from
# behind, so the character's left is on the viewer's left.
_FRONT: Dict[str, Tuple[int, int]] = {
    "head": (1293, 329), "neck": (1293, 350),
    "torso_upper": (1293, 378), "torso_lower": (1293, 407), "groin": (1293, 438),
    "upperarm_r": (1264, 387), "upperarm_l": (1322, 387),
    "forearm_r": (1255, 418), "forearm_l": (1331, 418),
    "hand_r": (1246, 450), "hand_l": (1340, 450),
    "upperleg_r": (1281, 462), "upperleg_l": (1306, 462),
    "lowerleg_r": (1281, 512), "lowerleg_l": (1306, 512),
    "foot_r": (1280, 553), "foot_l": (1307, 553),
}
_BACK: Dict[str, Tuple[int, int]] = {
    "head": (1397, 329), "neck": (1397, 348),
    "torso_upper": (1397, 377), "torso_lower": (1397, 407), "groin": (1397, 433),
    "upperarm_l": (1368, 387), "upperarm_r": (1426, 387),
    "forearm_l": (1359, 418), "forearm_r": (1435, 418),
    "hand_l": (1352, 450), "hand_r": (1442, 450),
    "upperleg_l": (1384, 463), "upperleg_r": (1411, 463),
    "lowerleg_l": (1384, 512), "lowerleg_r": (1411, 512),
    "foot_l": (1384, 555), "foot_r": (1411, 555),
}
# Project Zomboid doesn't say which side of a body part was hit, so the front
# doll carries the wounds and the back doll a fainter echo of them.
_BACK_OPACITY = 0.45
_DOLL_BOX = (1232, 304, 1462, 568)
_SS = 4  # supersampling factor for the wound marks
_MARK_SCALE = 1.3  # wound mark size, relative to the base sizes in _mark

# Offsets for several wounds on the same body part (final pixels).
_STACK = [(0, 0), (4, -5), (-4, 5), (5, 4), (-5, -4), (0, 7)]

# Condition text -> wound kind.
_CONDITION_KIND = {
    "bitten": "bite", "bite": "bite",
    "scratched": "scratch", "scratch": "scratch",
    "cut": "cut", "laceration": "cut", "lacerated": "cut",
    "deep wound": "deep", "deepwound": "deep",
    "bleeding": "bleed",
    "fractured": "fracture", "fracture": "fracture", "broken bone": "fracture",
    "burn": "burn", "burned": "burn", "burnt": "burn",
    "bullet": "deep", "glass shards": "cut", "glass": "cut",
    "infected": "infection", "infection": "infection",
}

# Wound kind -> plural label for the forensic summary.
_KIND_LABEL = {
    "bite": ("Bite Wound", "Bite Wounds"),
    "scratch": ("Scratch", "Scratches"),
    "cut": ("Laceration", "Lacerations"),
    "deep": ("Deep Wound", "Deep Wounds"),
    "bleed": ("Bleeding Wound", "Bleeding Wounds"),
    "fracture": ("Fracture", "Fractures"),
    "burn": ("Burn", "Burns"),
    "infection": ("Infected Wound", "Infected Wounds"),
}

# Human-readable labels for the parsed body-part keys.
_PART_LABELS = {
    "head": "Head", "neck": "Neck",
    "torso_upper": "Upper torso", "torso_lower": "Lower torso", "groin": "Groin",
    "upperarm_l": "Left upper arm", "forearm_l": "Left forearm", "hand_l": "Left hand",
    "upperarm_r": "Right upper arm", "forearm_r": "Right forearm", "hand_r": "Right hand",
    "upperleg_l": "Left thigh", "lowerleg_l": "Left shin", "foot_l": "Left foot",
    "upperleg_r": "Right thigh", "lowerleg_r": "Right shin", "foot_r": "Right foot",
}

# Approximate town bounds on the Knox County map (x0, y0, x1, y1). A death
# outside all of them shows plain coordinates.
_TOWNS = [
    ("Muldraugh", (10500, 9200, 11100, 10800)),
    ("West Point", (11050, 6550, 12300, 7350)),
    ("Riverside", (5800, 5050, 7050, 5700)),
    ("Rosewood", (7900, 11250, 8600, 12000)),
    ("March Ridge", (9650, 12450, 10450, 13150)),
    ("Valley Station", (12600, 4500, 14100, 5800)),
    ("Louisville", (11700, 900, 14700, 4500)),
]

_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


# ---------------------------------------------------------------------------
# Pure formatting helpers (tested in tests/test_death_card.py)
# ---------------------------------------------------------------------------

def normalize_part(name: str) -> Optional[str]:
    """Map a Project Zomboid BodyPartType name to a doll anchor key.

    Accepts the enum names the Death Log mod writes (``Hand_L``, ``ForeArm_R``,
    ``UpperLeg_L``) and friendly ones (``Left Hand``, ``right forearm``).
    """
    n = (name or "").strip().lower().replace(" ", "").replace("-", "").replace(".", "")
    if not n:
        return None
    left = n.endswith("_l") or "left" in n
    right = n.endswith("_r") or "right" in n
    side = "l" if left else ("r" if right else None)

    if "head" in n:
        return "head"
    if "neck" in n:
        return "neck"
    if "groin" in n:
        return "groin"
    if "torso" in n or "back" in n or "chest" in n or "stomach" in n:
        return "torso_lower" if ("lower" in n or "stomach" in n) else "torso_upper"
    if side is None:
        return None
    if "hand" in n:
        return f"hand_{side}"
    if "forearm" in n or "fore" in n:
        return f"forearm_{side}"
    if "arm" in n or "shoulder" in n:
        return f"upperarm_{side}"
    if "foot" in n:
        return f"foot_{side}"
    if any(k in n for k in ("lowerleg", "shin", "calf", "knee")):
        return f"lowerleg_{side}"
    if any(k in n for k in ("upperleg", "thigh", "leg")):
        return f"upperleg_{side}"
    return None


def _split_injury_segment(seg: str) -> Tuple[str, str]:
    """Split one `Injuries:` segment into (body_part, conditions).

    The Death Log mod has written two formats:
      old: "Hand_R: Bleeding, Deep Wound"
      new: "Left Hand (Bleeding, Deep Wound)"
    """
    seg = (seg or "").strip()
    if "(" in seg:
        part, _, rest = seg.partition("(")
        return part.strip(), rest.rstrip(")").strip()
    if ":" in seg:
        part, _, conds = seg.partition(":")
        return part.strip(), conds.strip()
    return seg, ""


def parse_injuries(injuries: str) -> List[Tuple[str, List[str]]]:
    """Parse `Injuries:` text into [(part_key, [condition, ...]), ...]."""
    raw = (injuries or "").strip()
    if not raw or raw.lower() in ("none", "n/a", "-"):
        return []
    result: List[Tuple[str, List[str]]] = []
    for seg in raw.split(";"):
        part, conds = _split_injury_segment(seg)
        key = normalize_part(part)
        if not key:
            continue
        result.append((key, [c.strip().lower() for c in conds.split(",") if c.strip()]))
    return result


def injury_lines(injuries: str) -> List[str]:
    """One "Left hand: Bitten, Bleeding" line per injured body part."""
    lines = []
    for key, conds in parse_injuries(injuries):
        label = _PART_LABELS[key]
        lines.append(f"{label}: {', '.join(c.title() for c in conds)}" if conds else label)
    return lines


def injury_summary(injuries: str) -> List[str]:
    """Count wounds by kind: ["Bite Wound", "Scratches (3)", ...], worst first."""
    counts: Dict[str, int] = {}
    for _key, conds in parse_injuries(injuries):
        for c in conds:
            kind = _CONDITION_KIND.get(c)
            if kind:
                counts[kind] = counts.get(kind, 0) + 1
    order = list(_KIND_LABEL)
    out = []
    for kind in sorted(counts, key=order.index):
        one, many = _KIND_LABEL[kind]
        n = counts[kind]
        out.append(one if n == 1 else f"{many} ({n})")
    return out


def split_cause(cause: str) -> Tuple[str, str]:
    """Split the mod's "Kind - detail" cause into (headline, detail).

    "Zombie - Shambler" -> ("Zombie Attack", "Shambler")
    "Player - bob"      -> ("Homicide", "bob")
    "Animal - Wolf"     -> ("Animal Attack", "Wolf")
    Anything else is kept as the headline.
    """
    cause = (cause or "").strip() or "Unknown"
    # A legacy "… — by killer" suffix (death_log adds it from "Last attacker").
    by = ""
    if " — by " in cause:
        cause, _, by = cause.partition(" — by ")
    kind, sep, detail = cause.partition(" - ")
    kind_l = kind.strip().lower()
    if sep and kind_l in ("zombie", "player", "animal"):
        headline = {"zombie": "Zombie Attack", "player": "Homicide",
                    "animal": "Animal Attack"}[kind_l]
        return headline, (by or detail).strip()
    if by:
        return cause.strip(), by.strip()
    return cause.strip(), ""


def format_cause(cause: str) -> str:
    headline, detail = split_cause(cause)
    if not detail:
        return headline
    if headline == "Homicide":
        return f"Homicide — killed by {detail}"
    return f"{headline} ({detail})"


def format_game_date(value: str) -> str:
    """"1993-7-22 14:32" -> "July 22, 1993   14:32"; anything else unchanged."""
    m = re.match(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T]+(\d{1,2}):(\d{2}))?", value or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        return (value or "").strip()
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    text = f"{_MONTHS[month - 1]} {day}, {year}"
    if m.group(4):
        text += f"   {int(m.group(4)):02d}:{m.group(5)}"
    return text


def parse_position(position: str) -> Optional[Tuple[int, int, int]]:
    m = re.search(r"X:\s*(-?\d+(?:\.\d+)?)\s*,\s*Y:\s*(-?\d+(?:\.\d+)?)"
                  r"(?:\s*,\s*Z:\s*(-?\d+(?:\.\d+)?))?", position or "")
    if not m:
        return None
    return (round(float(m.group(1))), round(float(m.group(2))),
            round(float(m.group(3) or 0)))


def town_for(x: int, y: int) -> Optional[str]:
    for name, (x0, y0, x1, y1) in _TOWNS:
        if x0 <= x <= x1 and y0 <= y <= y1:
            return name
    return None


def format_location(position: str) -> Tuple[str, str]:
    """(long, short) location text: "Muldraugh (X 10612, Y 9845, Floor 0)", "Muldraugh"."""
    pos = parse_position(position)
    if pos is None:
        raw = (position or "").strip() or "Unknown"
        return raw, raw
    x, y, z = pos
    coords = f"X {x}, Y {y}, Floor {z}"
    town = town_for(x, y)
    if town:
        return f"near {town} ({coords})", f"near {town}"
    return coords, f"{x}, {y} (F{z})"


def registry_number(issued: datetime.datetime, serial: int) -> str:
    return f"BT-DC-{issued:%Y%m%d}-{max(0, int(serial)):05d}"


def autopsy_notes(cause: str, infected: bool, survival_time: str,
                  injuries: str, profession: str = "") -> str:
    headline, detail = split_cause(cause)
    if headline == "Zombie Attack":
        what = f"a zombie attack ({detail})" if detail else "a zombie attack"
    elif headline == "Homicide":
        what = f"an attack by {detail}" if detail else "an attack by another survivor"
    elif headline == "Animal Attack":
        what = f"an animal attack ({detail})" if detail else "an animal attack"
    else:
        what = headline.lower()
    who = f"Victim ({profession.strip().lower()})" if profession.strip() else "Victim"
    text = f"{who} succumbed to {what}"
    if survival_time.strip():
        text += f" after surviving {survival_time.strip()}"
    text += "."
    wounds = sum(len(c) or 1 for _k, c in parse_injuries(injuries))
    if wounds:
        text += f" {wounds} wound{'s' if wounds != 1 else ''} recorded."
    text += (" Signs of infection were present at time of death." if infected
             else " No signs of infection were found.")
    return text


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

_font_cache: Dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    key = (str(path), size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(str(path), size)
    return _font_cache[key]


def _baseline(font: ImageFont.FreeTypeFont, cap_centre_y: float) -> float:
    """Baseline y that puts the centre of a capital letter at `cap_centre_y`."""
    top = font.getbbox("H", anchor="ls")[1]  # negative: cap height above baseline
    return cap_centre_y - top / 2


def _fit(draw: ImageDraw.ImageDraw, text: str, path: Path, size: int,
         max_w: int, min_size: int = 10) -> Tuple[str, ImageFont.FreeTypeFont]:
    """Shrink the font down to `min_size`, then truncate with an ellipsis."""
    for s in range(size, min_size - 1, -1):
        font = _font(path, s)
        if draw.textlength(text, font=font) <= max_w:
            return text, font
    font = _font(path, min_size)
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…", font


def _type(draw, x, cap_y, text, max_w, size, fill=_INK, path=_MONO, min_size=None):
    if min_size is None:
        min_size = max(11, size - 3)
    text, font = _fit(draw, str(text), path, size, max_w, min_size)
    draw.text((x, _baseline(font, cap_y)), text, font=font, fill=fill, anchor="ls")


def _wrap(draw, text: str, path: Path, size: int, max_w: int) -> List[str]:
    font = _font(path, size)
    char_w = max(1.0, draw.textlength("M", font=font))
    return textwrap.wrap(text, width=max(8, int(max_w // char_w)))


def _type_block(draw, block, lines: Sequence[str], fill=_INK, bullet: bool = False) -> None:
    """Type `lines` into a multi-line block; overflow becomes "+N more"."""
    x, y, max_w, size, line_h, max_lines = block
    lines = list(lines)
    if len(lines) > max_lines:
        extra = len(lines) - (max_lines - 1)
        lines = lines[:max_lines - 1] + [f"+{extra} more"]
    indent = 15 if bullet else 0
    for i, line in enumerate(lines):
        cy = y + i * line_h
        if bullet:
            draw.ellipse([x + 1, cy - 2, x + 5, cy + 2], fill=fill)
        _type(draw, x + indent, cy, line, max_w - indent, size, fill, min_size=size - 3)


def _paragraph_fits(draw, block, text: str) -> bool:
    _x, _y, max_w, size, _lh, max_lines = block
    return len(_wrap(draw, text, _MONO, size, max_w)) <= max_lines


def _type_paragraph(draw, block, text: str, fill=_INK) -> None:
    x, y, max_w, size, line_h, max_lines = block
    for s in range(size, size - 3, -1):
        lines = _wrap(draw, text, _MONO, s, max_w)
        if len(lines) <= max_lines:
            break
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .") + "…"
    for i, line in enumerate(lines):
        _type(draw, x, y + i * line_h, line, max_w, s, fill, min_size=s - 2)


def _portrait(avatar: Optional[bytes], size: Tuple[int, int]) -> Image.Image:
    """Turn a Discord avatar into an aged black-and-white polaroid print."""
    w, h = size
    src = None
    if avatar:
        try:
            src = Image.open(io.BytesIO(avatar))
            src.seek(0)
            src = src.convert("RGB")
        except Exception:
            src = None
    if src is None:
        return _no_photo(size)
    photo = ImageOps.fit(src, size, Image.LANCZOS, centering=(0.5, 0.4))
    grey = ImageOps.autocontrast(ImageOps.grayscale(photo), cutoff=1)
    # Slightly faded, warm print: lift the blacks, cap the whites.
    photo = ImageOps.colorize(grey, black=(24, 22, 20), white=(222, 214, 198), mid=(118, 112, 102))
    # Vignette.
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).ellipse([-w * 0.25, -h * 0.2, w * 1.25, h * 1.2], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(min(w, h) // 5))
    dark = Image.new("RGB", size, (18, 17, 16))
    photo = Image.composite(photo, dark, mask)
    # Film grain.
    grain = Image.effect_noise(size, 18).convert("RGB")
    return Image.blend(photo, grain, 0.06)


def _no_photo(size: Tuple[int, int]) -> Image.Image:
    """Placeholder print: a grey silhouette and "NO PHOTO ON FILE"."""
    w, h = size
    img = Image.new("RGB", size, (38, 37, 35))
    d = ImageDraw.Draw(img)
    shade = (62, 60, 57)
    cx = w // 2
    d.ellipse([cx - 32, 30, cx + 32, 104], fill=shade)
    d.rounded_rectangle([cx - 66, 110, cx + 66, h - 44], radius=44, fill=shade)
    font = _font(_MONO_BOLD, 14)
    d.text((cx, h - 22), "NO PHOTO ON FILE", font=font, fill=(150, 146, 138), anchor="mm")
    return img


# --- wound marks ------------------------------------------------------------

def _mark(d: ImageDraw.ImageDraw, kind: str, x: float, y: float, alpha: int) -> None:
    """Draw one wound mark centred on (x, y) in supersampled pixels."""
    s = _SS * _MARK_SCALE
    blood = (150, 18, 14, alpha)
    dark = (95, 8, 6, alpha)
    if kind == "bite":
        # Two opposing tooth arcs.
        r = 4.5 * s
        d.arc([x - r, y - r - 1 * s, x + r, y + r - 1 * s], 200, 340, fill=dark, width=int(1.6 * s))
        d.arc([x - r, y - r + 1 * s, x + r, y + r + 1 * s], 20, 160, fill=dark, width=int(1.6 * s))
        d.ellipse([x - 2 * s, y - 2 * s, x + 2 * s, y + 2 * s], fill=(150, 18, 14, alpha // 2))
    elif kind == "scratch":
        for off in (-2.2, 0, 2.2):
            d.line([x - 3.5 * s + off * s, y - 4 * s, x + 2.5 * s + off * s, y + 4 * s],
                   fill=blood, width=int(0.9 * s))
    elif kind in ("cut", "deep"):
        w = 2.2 if kind == "deep" else 1.4
        d.line([x - 5 * s, y + 3 * s, x + 5 * s, y - 3 * s], fill=dark, width=int(w * s))
        d.line([x - 4 * s, y + 2.4 * s, x + 4 * s, y - 2.4 * s], fill=blood, width=max(1, int((w - 0.8) * s)))
    elif kind == "bleed":
        d.ellipse([x - 3.5 * s, y - 3 * s, x + 3.5 * s, y + 3 * s], fill=blood)
        d.ellipse([x - 1.2 * s, y + 2 * s, x + 1.2 * s, y + 7 * s], fill=blood)
        d.ellipse([x + 2.5 * s, y + 3 * s, x + 4 * s, y + 4.5 * s], fill=blood)
    elif kind == "fracture":
        pts = [(x - 5 * s, y - 3 * s), (x - 2 * s, y), (x, y - 3 * s), (x + 2 * s, y + 1 * s),
               (x + 5 * s, y - 1 * s)]
        d.line(pts, fill=(40, 30, 26, alpha), width=int(1.4 * s), joint="curve")
    elif kind == "burn":
        d.ellipse([x - 4.5 * s, y - 3.5 * s, x + 4.5 * s, y + 3.5 * s], fill=(120, 62, 24, alpha * 2 // 3))
        d.ellipse([x - 2.5 * s, y - 2 * s, x + 2.5 * s, y + 2 * s], fill=(70, 30, 14, alpha * 2 // 3))
    else:  # infection / unknown: a sickly blot
        d.ellipse([x - 3.5 * s, y - 3.5 * s, x + 3.5 * s, y + 3.5 * s], fill=(96, 92, 30, alpha // 2))
        d.ellipse([x - 1.5 * s, y - 1.5 * s, x + 1.5 * s, y + 1.5 * s], fill=dark)


def _draw_wounds(img: Image.Image, injuries: str) -> None:
    x0, y0, x1, y1 = _DOLL_BOX
    layer = Image.new("RGBA", ((x1 - x0) * _SS, (y1 - y0) * _SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for key, conds in parse_injuries(injuries):
        kinds = [_CONDITION_KIND.get(c, "other") for c in conds] or ["bleed"]
        for i, kind in enumerate(kinds):
            dx, dy = _STACK[i % len(_STACK)]
            for anchors, alpha in ((_BACK, int(230 * _BACK_OPACITY)), (_FRONT, 230)):
                ax, ay = anchors[key]
                _mark(d, kind, (ax + dx - x0) * _SS, (ay + dy - y0) * _SS, alpha)
    layer = layer.resize((x1 - x0, y1 - y0), Image.LANCZOS)
    img.alpha_composite(layer, (x0, y0))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "yes", "1", "infected")


def render_death_card(data: Dict, avatar: Optional[bytes] = None) -> Image.Image:
    """Render the death certificate.

    `data` keys: survivor, character_name, infected, survival_time,
    zombie_kills, cause, injuries, location, game_date_time, death_count,
    deaths_today, deaths_week; optional: profession, registry_serial,
    issued_at (aware datetime, default now in PHT).
    `avatar` is the raw image bytes of the survivor's Discord avatar, if linked.
    """
    img = Image.open(_CARD_PATH).convert("RGBA")
    draw = ImageDraw.Draw(img)

    survivor = str(data.get("survivor") or "").strip() or "Unknown"
    character = str(data.get("character_name") or "").strip() or survivor
    infected = _truthy(data.get("infected"))
    cause = str(data.get("cause") or "Unknown")
    injuries = str(data.get("injuries") or "")
    issued = data.get("issued_at") or datetime.datetime.now(_TZ)
    issued = issued.astimezone(_TZ)
    reg_no = registry_number(issued, data.get("registry_serial") or data.get("death_count") or 0)
    loc_long, loc_short = format_location(str(data.get("location") or ""))
    cause_text = format_cause(cause)
    infection = ("Infected", _RED) if infected else ("Not Infected", _INK)

    values = {
        "registry_no": (reg_no, _RED),
        "date_issued": (f"{_MONTHS[issued.month - 1]} {issued.day}, {issued.year}", _INK),
        "character_name": (character, _INK),
        "username": (survivor, _INK),
        "infection": infection,
        "survival_time": (str(data.get("survival_time") or "").strip() or "Unknown", _INK),
        "zombie_kills": (str(data.get("zombie_kills") or "0"), _INK),
        "server_deaths": (str(data.get("death_count") or "1"), _INK),
        "deaths_today": (str(data.get("deaths_today") or 0), _INK),
        "deaths_week": (str(data.get("deaths_week") or 0), _INK),
        "game_date_time": (format_game_date(str(data.get("game_date_time") or "")) or "Unknown", _INK),
        "location": (loc_long, _INK),
        "cause": (cause_text, _INK),
        "s_character_name": (character, _INK),
        "s_username": (survivor, _INK),
        "s_registry_no": (reg_no, _INK),
        "f_cause": (split_cause(cause)[0], _INK),
        "f_infection": infection,
        "real_date_time": (f"{issued:%b} {issued.day}, {issued.year}  {issued:%H:%M} PHT", _INK),
        "d_location": (loc_short, _INK),
    }
    for name, (x, y, max_w, size) in _FIELDS.items():
        text, fill = values[name]
        _type(draw, x, y, text, max_w, size, fill)

    _type_block(draw, _INJURY_LIST, injury_lines(injuries) or ["None recorded"])
    summary = injury_summary(injuries)
    _type_block(draw, _INJURY_SUMMARY, summary or ["None recorded"], bullet=bool(summary))
    survived = str(data.get("survival_time") or "")
    notes = autopsy_notes(cause, infected, survived, injuries,
                          str(data.get("profession") or ""))
    if not _paragraph_fits(draw, _NOTES, notes):
        notes = autopsy_notes(cause, infected, survived, injuries)
    _type_paragraph(draw, _NOTES, notes)
    _type_paragraph(draw, _CERT, _CERT_TEXT)

    # FINAL DETERMINATION
    headline = split_cause(cause)[0].upper()
    headline, font = _fit(draw, headline, _SLAB_BOLD, 30, _FINAL_W, 16)
    draw.text((_FINAL_CX, _baseline(font, _FINAL_Y[0])), headline, font=font,
              fill=_RED, anchor="ms")
    sub = "(INFECTED)" if infected else "(NOT INFECTED)"
    sub_font = _font(_SLAB_BOLD, 18)
    draw.text((_FINAL_CX, _baseline(sub_font, _FINAL_Y[1])), sub, font=sub_font,
              fill=_RED, anchor="ms")

    # Portrait
    px0, py0, px1, py1 = _PHOTO_BOX
    img.paste(_portrait(avatar, (px1 - px0, py1 - py0)), (px0, py0))

    # Injury diagram
    _draw_wounds(img, injuries)
    return img
