"""status_icons.py — flat icons for the status embed, as the bot's own emojis.

Discord can't put images in embed field names, but it shows custom emoji
there. The icons are drawn here with Pillow (flat line style, one colour each)
and uploaded once as *application emojis* (owned by the bot, no server slots
used). Each emoji's name carries a short hash of its picture
(`pzs_<key>_<hash>`), so changing a drawing uploads the new one and removes the
old one on the next start.

`icon(key)` gives the emoji markup, or the plain Unicode emoji when the upload
hasn't happened (or failed), so the embed always has an icon.
"""
from __future__ import annotations

import hashlib
import io
import math

from PIL import Image, ImageDraw

_S = 512                 # drawing size (4x the 128 px emoji, for clean edges)
_W = 44                  # stroke width at drawing size
_PREFIX = "pzs_"

_AMBER = (250, 176, 5)
_BLUE = (77, 171, 247)
_SKY = (116, 192, 252)
_RED = (250, 82, 82)
_GREEN = (64, 192, 87)
_GREY = (173, 181, 189)
_TEAL = (56, 217, 169)
_VIOLET = (151, 117, 250)

# Plain emoji when the custom ones aren't available.
FALLBACK = {
    "age": "\U0001f382", "date": "\U0001f4c5", "day": "☀️", "night": "\U0001f319",
    "clear": "☀️", "cloud": "☁️", "rain": "\U0001f327️",
    "snow": "❄️", "fog": "\U0001f32b️", "wind": "\U0001f4a8",
    "siege": "\U0001f6e1️", "players": "\U0001f465", "restart": "\U0001f504",
}


def _canvas():
    img = Image.new("RGBA", (_S, _S), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _line(d, pts, colour, w=_W):
    d.line(pts, fill=colour, width=w, joint="curve")
    r = w / 2
    for x, y in (pts[0], pts[-1]):
        d.ellipse((x - r, y - r, x + r, y + r), fill=colour)


def _arc(d, box, start, end, colour, w=_W):
    d.arc(box, start, end, fill=colour, width=w)
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    rx, ry = (box[2] - box[0]) / 2 - w / 2, (box[3] - box[1]) / 2 - w / 2
    for a in (start, end):
        x, y = cx + rx * math.cos(math.radians(a)), cy + ry * math.sin(math.radians(a))
        d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=colour)


def _cloud(d, colour, dy=0):
    # Outline of a cloud: two bumps and a flat base.
    d.rounded_rectangle((70, 250 + dy, 442, 400 + dy), radius=75, outline=colour, width=_W)
    d.ellipse((150, 140 + dy, 330, 320 + dy), outline=colour, width=_W)
    d.ellipse((260, 190 + dy, 410, 340 + dy), outline=colour, width=_W)
    # Clear the inner overlaps so it reads as one shape.
    inner = (0, 0, 0, 0)
    d.rounded_rectangle((70 + _W, 250 + dy + _W, 442 - _W, 400 + dy - _W), radius=75 - _W, fill=inner)
    d.ellipse((150 + _W, 140 + dy + _W, 330 - _W, 320 + dy - _W), fill=inner)
    d.ellipse((260 + _W, 190 + dy + _W, 410 - _W, 340 + dy - _W), fill=inner)
    d.rectangle((70 + _W, 300 + dy, 442 - _W, 400 + dy - _W), fill=inner)


def _sun(d, colour, r=95):
    c = _S / 2
    d.ellipse((c - r, c - r, c + r, c + r), outline=colour, width=_W)
    for i in range(8):
        a = math.radians(i * 45)
        _line(d, [(c + math.cos(a) * (r + 55), c + math.sin(a) * (r + 55)),
                  (c + math.cos(a) * (r + 115), c + math.sin(a) * (r + 115))], colour)


def draw(key: str) -> Image.Image:
    img, d = _canvas()
    if key == "age":                       # hourglass
        c = _AMBER
        _line(d, [(130, 70), (382, 70)], c)
        _line(d, [(130, 442), (382, 442)], c)
        d.line([(160, 90), (160, 150), (256, 256), (160, 362), (160, 422)], fill=c, width=_W, joint="curve")
        d.line([(352, 90), (352, 150), (256, 256), (352, 362), (352, 422)], fill=c, width=_W, joint="curve")
        d.polygon([(200, 400), (312, 400), (256, 330)], fill=c)
    elif key == "date":                    # calendar
        c = _RED
        d.rounded_rectangle((70, 100, 442, 450), radius=50, outline=c, width=_W)
        d.rectangle((70 + _W // 2, 180, 442 - _W // 2, 180 + _W), fill=c)
        _line(d, [(170, 60), (170, 140)], c)
        _line(d, [(342, 60), (342, 140)], c)
        for x in (170, 256, 342):
            for y in (290, 370):
                d.ellipse((x - 24, y - 24, x + 24, y + 24), fill=c)
    elif key in ("day", "clear"):
        _sun(d, _AMBER)
    elif key == "night":                   # crescent moon
        c = _VIOLET
        d.ellipse((80, 80, 432, 432), fill=c)
        d.ellipse((80 + _W, 80 + _W, 432 - _W, 432 - _W), fill=(0, 0, 0, 0))
        d.ellipse((190, 30, 500, 340), fill=(0, 0, 0, 0))
        d.arc((190 - _W, 30 - _W, 500 + _W, 340 + _W), 95, 225, fill=c, width=_W)
    elif key == "cloud":
        _cloud(d, _GREY, dy=20)
    elif key == "rain":
        _cloud(d, _BLUE, dy=-40)
        for x in (170, 256, 342):
            _line(d, [(x + 15, 405), (x - 20, 470)], _BLUE)
    elif key == "snow":
        _cloud(d, _SKY, dy=-40)
        for x in (170, 256, 342):
            d.ellipse((x - 26, 420, x + 26, 472), fill=_SKY)
    elif key == "fog":
        for y, x0, x1 in ((150, 90, 422), (256, 60, 452), (362, 110, 402)):
            _line(d, [(x0, y), (x1, y)], _GREY)
    elif key == "wind":
        c = _TEAL
        _line(d, [(60, 200), (330, 200)], c)
        _arc(d, (270, 90, 400, 220), 200, 450, c)
        _line(d, [(60, 300), (400, 300)], c)
        _arc(d, (340, 300, 460, 420), -90, 160, c)
        _line(d, [(110, 400), (260, 400)], c)
    elif key == "siege":                   # shield
        c = _BLUE
        pts = [(256, 50), (430, 115), (420, 280), (256, 465), (92, 280), (82, 115)]
        d.line(pts + [pts[0], pts[1]], fill=c, width=_W, joint="curve")
        _line(d, [(180, 255), (235, 310), (335, 205)], c)
    elif key == "players":                 # two people
        c = _BLUE
        d.ellipse((115, 95, 255, 235), outline=c, width=_W)
        _arc(d, (55, 270, 315, 530), 190, 350, c)
        d.ellipse((290, 125, 400, 235), outline=c, width=_W)
        _arc(d, (270, 290, 470, 490), 200, 340, c)
    elif key == "restart":                 # circular arrows
        c = _GREEN
        _arc(d, (85, 85, 427, 427), 200, 340, c)
        _arc(d, (85, 85, 427, 427), 20, 160, c)
        d.polygon([(405, 160), (470, 250), (365, 255)], fill=c)
        d.polygon([(107, 352), (42, 262), (147, 257)], fill=c)
    else:
        raise KeyError(key)
    return img.resize((128, 128), Image.LANCZOS)


KEYS = tuple(FALLBACK)


def png(key: str) -> bytes:
    buf = io.BytesIO()
    draw(key).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def emoji_name(key: str, data: bytes | None = None) -> str:
    """`pzs_<key>_<hash>`: a new drawing gets a new name (and a new upload)."""
    digest = hashlib.sha1(data if data is not None else png(key)).hexdigest()[:6]
    return f"{_PREFIX}{key}_{digest}"


_markup: dict[str, str] = {}


def icon(key: str) -> str:
    """The emoji for `key`: the uploaded flat one, else the plain fallback."""
    return _markup.get(key) or FALLBACK.get(key, "")


def weather_key(weather: str) -> str:
    w = (weather or "").lower()
    if "snow" in w:
        return "snow"
    if "rain" in w or "storm" in w or "drizzle" in w:
        return "rain"
    if "fog" in w or "mist" in w:
        return "fog"
    if "cloud" in w or "overcast" in w:
        return "cloud"
    return "clear"


async def sync(client) -> int:
    """Upload missing icons as application emojis and delete outdated ones.
    Fills the markup used by `icon`. -> how many were uploaded."""
    existing = {e.name: e for e in await client.fetch_application_emojis()}
    wanted = {}
    uploaded = 0
    for key in KEYS:
        data = png(key)
        name = emoji_name(key, data)
        wanted[name] = key
        emoji = existing.get(name)
        if emoji is None:
            emoji = await client.create_application_emoji(name=name, image=data)
            uploaded += 1
        _markup[key] = str(emoji)
    for name, emoji in existing.items():
        if name.startswith(_PREFIX) and name not in wanted:
            try:
                await emoji.delete()
            except Exception as e:  # an old icon left behind is harmless
                print(f"[StatusIcons] could not delete old emoji {name}: {e}")
    return uploaded
