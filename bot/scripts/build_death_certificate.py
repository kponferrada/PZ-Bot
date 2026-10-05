"""Build assets/death-certificate.png from the filled-in source art.

The source art (assets/death-certificate-source.webp) arrives with sample
values ("Jamiebear", "Zombie Attack", ...), a sample portrait and blood painted
on the paper dolls. This one-off tool inpaints all of that away so
death_card.py can draw real values on a blank form.

It needs numpy and opencv-python-headless (not bot dependencies):
    pip install numpy opencv-python-headless
    python scripts/build_death_certificate.py
"""

from pathlib import Path

import cv2
import numpy as np

_ASSETS = Path(__file__).resolve().parent.parent / "assets"
SRC = _ASSETS / "death-certificate-source.webp"
OUT = _ASSETS / "death-certificate.png"

# Value areas holding sample text: (x0, y0, x1, y1). Only ink pixels inside
# each box are inpainted, so the faint ruled lines and paper grain survive.
TEXT_BOXES = [
    # Header: registry no. / date issued
    (950, 57, 1116, 75), (950, 84, 1118, 106),
    # DECEASED + SURVIVAL RECORD values
    (334, 312, 562, 343), (334, 346, 562, 374), (334, 379, 562, 405),
    (334, 459, 562, 484), (334, 492, 562, 513), (334, 523, 562, 544),
    (334, 555, 562, 576), (334, 586, 562, 607),
    # DEATH INFORMATION values
    (372, 656, 800, 688), (372, 689, 800, 719), (372, 719, 800, 748),
    (372, 750, 800, 829),
    # SUBJECT values
    (1003, 379, 1212, 400), (1003, 410, 1212, 430), (1003, 439, 1212, 459),
    # FORENSIC FINDINGS values
    (1060, 509, 1212, 530), (1060, 543, 1212, 564), (1060, 575, 1232, 646),
    # DEATH DETAILS values
    (1078, 689, 1300, 712), (1078, 718, 1300, 742),
    # AUTOPSY NOTES (second line stops short of the stamp)
    (895, 785, 1432, 808), (895, 808, 1290, 828),
    # FINAL DETERMINATION box contents
    (870, 874, 1255, 934),
    # CERTIFICATION paragraph (re-typed by death_card.py; the art garbles it)
    # (lower lines stop short of the barcode caption)
    (82, 862, 708, 898), (82, 898, 666, 934),
]

# Polaroid photo window and the paper-doll card.
PHOTO_BOX = (583, 318, 774, 543)
DOLL_BOX = (1232, 304, 1462, 568)

# Neutral fill for the emptied photo window (dark, slightly warm grey).
PHOTO_FILL = (38, 37, 35)


def _ink_mask(img: np.ndarray, box) -> np.ndarray:
    """Dark or red pixels inside `box`, dilated to catch anti-aliased edges."""
    x0, y0, x1, y1 = box
    region = img[y0:y1, x0:x1].astype(int)
    b, g, r = region[..., 0], region[..., 1], region[..., 2]
    lum = (r + g + b) / 3
    ink = (lum < 125) | ((r - g > 40) & (r - b > 40))
    mask = np.zeros(img.shape[:2], np.uint8)
    mask[y0:y1, x0:x1] = ink.astype(np.uint8) * 255
    return mask


# Lab colour of the dolls' bare skin (sampled from an unpainted thigh).
SKIN_LAB = np.array([182.0, 129.0, 139.0])


def _wash_blood(img: np.ndarray, box) -> np.ndarray:
    """Wash the painted blood off the paper dolls.

    Inpainting smears the line art, so this works in Lab instead: any pixel
    redder than bare skin (a* above SKIN_LAB) has its a*/b* pulled back to
    skin and its lightness lifted toward skin by the same weight. The grey
    line art has no excess a* and is left alone.
    """
    x0, y0, x1, y1 = box
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(float)
    reg = lab[y0:y1, x0:x1]
    excess = np.clip(reg[..., 1] - SKIN_LAB[1] - 1, 0, None)
    weight = cv2.GaussianBlur(np.clip(excess / 6, 0, 1), (3, 3), 0)
    reg[..., 0] += np.clip(SKIN_LAB[0] - 6 - reg[..., 0], 0, None) * weight
    reg[..., 1] -= excess
    reg[..., 2] -= np.clip(reg[..., 2] - SKIN_LAB[2], 0, None) * weight
    return cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)


def main() -> None:
    img = cv2.imread(str(SRC), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"cannot read {SRC}")

    kernel = np.ones((3, 3), np.uint8)
    mask = np.zeros(img.shape[:2], np.uint8)
    for box in TEXT_BOXES:
        mask |= _ink_mask(img, box)
    mask = cv2.dilate(mask, kernel, iterations=2)
    img = cv2.inpaint(img, mask, 4, cv2.INPAINT_TELEA)

    img = _wash_blood(img, DOLL_BOX)

    x0, y0, x1, y1 = PHOTO_BOX
    img[y0:y1, x0:x1] = PHOTO_FILL[::-1]

    cv2.imwrite(str(OUT), img, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
