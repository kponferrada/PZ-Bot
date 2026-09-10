"""status_card.py — renders the PZ Tambayan server-status card image (Pillow).

Produces a dark, game-themed PNG in the PZ Tambayan style:
  header ("PZ TAMBAYAN"), a two-card status bar (online + players), a 4-column
  grid of info cards with icons, and a footer. Fonts and the background image
  are auto-detected where possible, with safe fallbacks.
"""

import os
from PIL import Image, ImageDraw, ImageFont

# --- palette ---------------------------------------------------------------

BG = (11, 14, 20, 255)
CARD = (21, 25, 34, 255)
CARD_BORDER = (42, 47, 58, 255)
GREEN = (0, 230, 118, 255)
GREEN_DARK = (0, 128, 74, 255)
RED = (255, 82, 82, 255)
RED_DARK = (138, 36, 36, 255)
BLUE = (127, 179, 232, 255)
WHITE = (240, 244, 248, 255)
GREY = (150, 160, 172, 255)
DIM = (95, 105, 116, 255)
AMBER = (255, 193, 7, 255)
ORANGE = (255, 152, 0, 255)
PINK = (255, 150, 200, 255)
GOLD = (255, 205, 60, 255)

WIDTH = 1200
HEIGHT = 700

# field label -> (emoji icon, icon colour)
ICONS = {
    "TIME": ("\u23f0", AMBER),
    "DATE": ("\U0001f4c5", RED),
    "SERVER AGE": ("\U0001f382", PINK),
    "PLAYERS": ("\U0001f465", BLUE),
    "WEATHER": ("\u2600", AMBER),
    "WIND": ("\U0001f4a8", BLUE),
    "CYCLE": ("\U0001f317", AMBER),
    "HORDE": ("\U0001f480", WHITE),
    "RESTART": ("\U0001f504", RED),
    "STATUS": ("\U0001f4cb", ORANGE),
    "COMPLETED": ("\U0001f3c6", GOLD),
}


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    """Return a TTF font, trying common OS locations, then PIL's default."""
    candidates = []
    if os.name == "nt":
        if bold:
            candidates += [r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf"]
        else:
            candidates += [r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\segoeui.ttf"]
    if bold:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
    else:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
    for path in candidates:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _emoji_font(size: int) -> ImageFont.FreeTypeFont:
    """A font that can render emoji glyphs (monochrome via Pillow)."""
    candidates = [
        r"C:\Windows\Fonts\seguiemj.ttf",
        r"C:\Windows\Fonts\seguisym.ttf",
        "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return _font(size)


def _tw(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    """Text width (bounded, since load_default font has no length support)."""
    try:
        return int(draw.textlength(text, font=font))
    except Exception:
        return len(text) * (font.size // 2 if hasattr(font, "size") else 6)


def render_status_card(
    output_path: str,
    *,
    title: str = "PZ TAMBAYAN",
    subtitle: str = "SURVIVE \u2022 BUILD \u2022 TAMBAY",
    tagline: str = "ANOTHER DAY ANOTHER STORY.",
    online: bool = True,
    players: str = "0 / 32",
    quote: str = "Good luck out there, survivor.",
    fields: list = None,
    last_update: str = "",
    background: str = "",
) -> str:
    """Render the status card and save it to `output_path` (PNG)."""
    fields = fields or []

    img = Image.new("RGBA", (WIDTH, HEIGHT), BG)
    if background and os.path.exists(background):
        try:
            bg = Image.open(background).convert("RGBA").resize((WIDTH, HEIGHT))
            shade = Image.new("RGBA", (WIDTH, HEIGHT), (6, 8, 12, 215))
            img = Image.alpha_composite(bg, shade)
        except Exception as e:
            print(f"[StatusCard] background failed: {e}")

    draw = ImageDraw.Draw(img)

    f_small = _font(16, bold=False)
    f_label = _font(14, bold=False)
    f_value = _font(23, bold=True)
    f_sub = _font(12, bold=False)
    f_title = _font(60, bold=True)
    f_tag = _font(19, bold=False)
    f_status = _font(28, bold=True)
    f_icon = _emoji_font(24)

    # --- header -----------------------------------------------------------
    draw.text((40, 24), "PROJECT ZOMBOID", font=f_small, fill=GREY)
    parts = title.split(" ", 1)
    first = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    if rest:
        w_first = _tw(draw, first + " ", f_title)
        draw.text((40, 48), first, font=f_title, fill=WHITE)
        draw.text((40 + w_first, 48), rest, font=f_title, fill=BLUE)
    else:
        draw.text((40, 48), first, font=f_title, fill=WHITE)
    draw.text((42, 128), subtitle, font=f_small, fill=GREY)
    tw = _tw(draw, tagline, f_tag)
    draw.text((WIDTH - 40 - tw, 52), tagline, font=f_tag, fill=GREY)

    # --- status bar: two cards + quote ------------------------------------
    bar_y = 185
    bar_h = 100

    left_col = GREEN_DARK if online else RED_DARK
    draw.rounded_rectangle([40, bar_y, 400, bar_y + bar_h], radius=12, fill=left_col)
    draw.ellipse([58, bar_y + 20, 76, bar_y + 38], fill=GREEN if online else RED)
    status = "ONLINE" if online else "OFFLINE"
    draw.text((88, bar_y + 8), status, font=f_status, fill=WHITE)
    sub_col = (180, 240, 210) if online else (255, 200, 200)
    draw.text((58, bar_y + 58), "SERVER IS RUNNING" if online else "SERVER IS DOWN",
              font=_font(14, bold=False), fill=sub_col)

    draw.rounded_rectangle([416, bar_y, 776, bar_y + bar_h], radius=12,
                           fill=CARD, outline=CARD_BORDER, width=1)
    draw.text((436, bar_y + 8), players, font=f_status, fill=WHITE)
    draw.text((436, bar_y + 58), "PLAYERS ONLINE", font=_font(14, bold=False), fill=GREY)

    qf = _font(16, bold=False)
    qw = _tw(draw, quote, qf)
    draw.text((WIDTH - 40 - qw, bar_y + 38), quote, font=qf, fill=GREY)

    # --- info grid --------------------------------------------------------
    grid_y = bar_y + bar_h + 20
    gap = 12
    card_w = (WIDTH - 80 - 3 * gap) // 4
    card_h = 92
    row_h = card_h + gap

    for i, (label, value, sub) in enumerate(fields[:12]):
        col = i % 4
        row = i // 4
        x = 40 + col * (card_w + gap)
        y = grid_y + row * row_h
        draw.rounded_rectangle([x, y, x + card_w, y + card_h], radius=10,
                               fill=CARD, outline=CARD_BORDER, width=1)
        icon, icon_col = ICONS.get(label, ("", WHITE))
        if icon:
            draw.text((x + 14, y + 10), icon, font=f_icon, fill=icon_col)
            lx = x + 14 + 32
        else:
            lx = x + 14
        draw.text((lx, y + 16), label, font=f_label, fill=GREY)
        draw.text((x + 14, y + 38), str(value), font=f_value, fill=WHITE)
        draw.text((x + 14, y + 66), sub or "", font=f_sub, fill=DIM)

    # --- footer -----------------------------------------------------------
    foot_y = grid_y + 3 * row_h + 10
    draw.text((40, foot_y), last_update, font=_font(15, bold=False), fill=GREY)
    draw.text((40, foot_y + 26), "KNOX COUNTY NEVER FORGETS.", font=_font(17, bold=True), fill=DIM)
    stw = _tw(draw, "STAY ALIVE.", _font(17, bold=True))
    draw.text((WIDTH - 40 - stw, foot_y + 26), "STAY ALIVE.", font=_font(17, bold=True), fill=GREEN)

    img.convert("RGB").save(output_path, "PNG")
    return output_path
