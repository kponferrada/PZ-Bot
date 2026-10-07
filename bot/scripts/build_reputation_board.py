"""Build the blank reputation board from the filled-in source art.

The source art (assets/reputation-board-source.webp) is a "Reputation Ranking
- Top 5 Survivors" poster with sample names, roles, scores, progress bars and
portraits. This one-off tool makes:

  assets/reputation-board.png       the poster with the sample values, the
                                    header words and the portraits removed;
                                    the place tiles and medals turned grey (median brightness 160) so
                                    rep_board.py can tint them in rank colours
  assets/reputation-board-tint.png  greyscale mask (L) of those tintable pixels
  assets/reputation-board-ribbon.png the shape (L) of row 2's brush-stroke
                                    ribbon; rep_board.py stamps it, tinted,
                                    over every row's ribbon (the ribbon paint
                                    is too close to the paper to recolour)

rep_board.py then draws real names, titles, values, bars, portraits and
header. Coordinates here and in rep_board.py (`ROWS`) are the same.

It needs numpy and opencv-python-headless (not bot dependencies):
    pip install numpy opencv-python-headless
    python scripts/build_reputation_board.py
"""

from pathlib import Path

import cv2
import numpy as np

_ASSETS = Path(__file__).resolve().parent.parent / "assets"
SRC = _ASSETS / "reputation-board-source.webp"
OUT = _ASSETS / "reputation-board.png"
OUT_TINT = _ASSETS / "reputation-board-tint.png"
OUT_RIBBON = _ASSETS / "reputation-board-ribbon.png"

# Per row (places 1-5), boxes as (x0, y0, x1, y1), measured on the 1024x1536 art.
#   name    sample name on the cream paper (row 1 includes the red star)
#   ribbon  the brush-stroke role ribbon; `text` is the role text on it
#   hue     OpenCV hue range of the ribbon paint (None = by saturation)
#   value   the big score on the black panel; `label` is "REPUTATION"
#   medal   the rosette icon
#   tile    the painted place tile (number + crown are black ink, kept)
#   photo   the polaroid's picture window
ROWS = [
    dict(name=(348, 492, 594, 540), ribbon=(352, 540, 590, 586), text=(378, 548, 580, 580),
         hue=None, value=(764, 542, 972, 616), label=(806, 619, 948, 640),
         medal=(680, 540, 746, 630), tile=(46, 480, 170, 646), photo=(192, 492, 330, 632)),
    dict(name=(342, 662, 505, 708), ribbon=(360, 708, 600, 746), text=(378, 714, 590, 742),
         hue=(90, 125), value=(778, 692, 976, 757), label=(814, 760, 950, 780),
         medal=(684, 698, 746, 772), tile=(54, 652, 178, 802), photo=(196, 664, 326, 796)),
    dict(name=(355, 824, 462, 871), ribbon=(364, 865, 610, 912), text=(400, 872, 560, 905),
         hue=None, value=(812, 842, 972, 911), label=(822, 916, 962, 935),
         medal=(688, 852, 746, 932), tile=(48, 812, 178, 962), photo=(200, 816, 330, 948)),
    dict(name=(364, 978, 446, 1024), ribbon=(370, 1018, 606, 1062), text=(410, 1024, 570, 1058),
         hue=(28, 60), value=(812, 995, 968, 1061), label=(822, 1066, 958, 1084),
         medal=(684, 1008, 742, 1085), tile=(48, 965, 182, 1105), photo=(202, 968, 336, 1096)),
    dict(name=(367, 1124, 458, 1174), ribbon=(376, 1158, 600, 1205), text=(420, 1164, 570, 1200),
         hue=(125, 170), value=(815, 1138, 965, 1205), label=(825, 1207, 955, 1226),
         medal=(684, 1148, 742, 1222), tile=(48, 1112, 178, 1262), photo=(202, 1112, 342, 1248)),
]

# "Top 5 Survivors" lettering on the tape strip.
HEADER_TEXT = (305, 390, 730, 446)

# Neutral fill for the emptied picture windows (dark, slightly warm grey).
PHOTO_FILL = (38, 37, 35)


def _lum(img: np.ndarray) -> np.ndarray:
    return img.astype(int).sum(axis=2) / 3


def _box_mask(shape, box, cond: np.ndarray) -> np.ndarray:
    x0, y0, x1, y1 = box
    mask = np.zeros(shape[:2], np.uint8)
    mask[y0:y1, x0:x1] = cond.astype(np.uint8) * 255
    return mask


def _dark_ink(img, box, limit=110):
    """Dark or red pixels inside `box` (text on paper, tape or a ribbon)."""
    x0, y0, x1, y1 = box
    reg = img[y0:y1, x0:x1].astype(int)
    b, g, r = reg[..., 0], reg[..., 1], reg[..., 2]
    ink = (_lum(img[y0:y1, x0:x1]) < limit) | ((r - g > 60) & (r - b > 60))
    return _box_mask(img.shape, box, ink)


def _bright(img, box, limit=70):
    """Light pixels inside `box` (text on the black panel)."""
    x0, y0, x1, y1 = box
    return _box_mask(img.shape, box, _lum(img[y0:y1, x0:x1]) > limit)


def _ribbon_mask(img, row) -> np.ndarray:
    x0, y0, x1, y1 = row["ribbon"]
    hsv = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2HSV).astype(int)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    if row["hue"]:
        lo, hi = row["hue"]
        paint = (h >= lo) & (h <= hi) & (s > 30) & (v > 60)
    else:
        paint = (s > 110) & (v > 80)
    mask = _box_mask(img.shape, row["ribbon"], paint)
    # Close the gaps left by paper grain and by where the text was.
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))


def _tile_mask(img, row) -> np.ndarray:
    # Painted paper is light; the number and crown ink and the board are dark.
    return _bright(img, row["tile"], 105)


def _medal_mask(img, row) -> np.ndarray:
    return _bright(img, row["medal"], 75)


def main() -> None:
    img = cv2.imread(str(SRC), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"cannot read {SRC}")
    kernel = np.ones((3, 3), np.uint8)

    # 1. Remove the sample text (names, ribbon text, scores, labels, header).
    mask = _dark_ink(img, HEADER_TEXT, 100)
    for row in ROWS:
        mask |= _dark_ink(img, row["name"])
        mask |= _dark_ink(img, row["text"], 95)
        mask |= _bright(img, row["value"])
        mask |= _bright(img, row["label"], 90)
    mask = cv2.dilate(mask, kernel, iterations=2)
    img = cv2.inpaint(img, mask, 5, cv2.INPAINT_TELEA)

    # 2. Grey out the tintable paint and record where it is.
    tint = np.zeros(img.shape[:2], np.uint8)
    grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(float)
    out = img.copy()
    for row in ROWS:
        for m in (_tile_mask(img, row), _medal_mask(img, row)):
            sel = m > 0
            if not sel.any():
                continue
            g = grey[sel]
            scaled = np.clip(g / max(np.median(g), 1) * 160, 0, 255).astype(np.uint8)
            out[sel] = scaled[:, None]
            tint |= m
    tint = cv2.GaussianBlur(tint, (3, 3), 0)

    # Row 2's ribbon is the cleanest stroke: keep its shape as the stamp.
    x0, y0, x1, y1 = ROWS[1]["ribbon"]
    ribbon = _ribbon_mask(img, ROWS[1])[y0:y1, x0:x1]
    ys, xs = np.nonzero(ribbon)
    ribbon = cv2.GaussianBlur(ribbon[ys.min():ys.max() + 1, xs.min():xs.max() + 1], (3, 3), 0)

    # 3. Empty the picture windows.
    for row in ROWS:
        x0, y0, x1, y1 = row["photo"]
        out[y0:y1, x0:x1] = PHOTO_FILL[::-1]

    cv2.imwrite(str(OUT), out, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    cv2.imwrite(str(OUT_TINT), tint, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    cv2.imwrite(str(OUT_RIBBON), ribbon, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    print(f"wrote {OUT}, {OUT_TINT} and {OUT_RIBBON}")


if __name__ == "__main__":
    main()
