"""Re-crop the red/green player banners tightly (remove the black surround)
and make the black background transparent so the card edges become the image edges."""
from PIL import Image

SRC = r"C:\Users\keym\AppData\Roaming\Hermes\composer-images\image_4f4780.png"
OUT = r"C:\Users\keym\Documents\PZ-Tambayan-Bot\bot\assets"

im = Image.open(SRC).convert("RGB")
px = im.load()
W, H = im.size


def is_red(r, g, b):
    return r > b + 3 and r > g + 3


def is_green(r, g, b):
    return g > r + 3 and g > b + 3


def bbox(pred, y0, y1):
    xs, ys = [], []
    for y in range(y0, y1):
        for x in range(W):
            if pred(*px[x, y]):
                xs.append(x)
                ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


red_bb = bbox(is_red, 0, 362)
green_bb = bbox(is_green, 362, 725)
print("red bbox:", red_bb)
print("green bbox:", green_bb)

# Crop tightly (with a 1px pad so the anti-aliased border edge is kept).
def crop_card(bb, pad=1):
    x0, y0, x1, y1 = bb
    return (max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + 1 + pad), min(H, y1 + 1 + pad))


red_crop = crop_card(red_bb)
green_crop = crop_card(green_bb)
print("red crop:", red_crop, "->", (red_crop[2]-red_crop[0], red_crop[3]-red_crop[1]))
print("green crop:", green_crop, "->", (green_crop[2]-green_crop[0], green_crop[3]-green_crop[1]))


def make_transparent(img):
    """Flood-fill from the edges to make the dark black surround transparent."""
    img = img.convert("RGBA")
    p = img.load()
    w, h = img.size

    def is_bg(px):
        r, g, b, _a = px
        # The black surround is neutral/bluish; the card is red- or green-tinted.
        # Remove only pixels that are neither clearly red nor clearly green.
        return not (r > b + 3) and not (g > r + 3)

    stack = []
    for x in range(w):
        stack.append((x, 0)); stack.append((x, h - 1))
    for y in range(h):
        stack.append((0, y)); stack.append((w - 1, y))

    while stack:
        x, y = stack.pop()
        if x < 0 or y < 0 or x >= w or y >= h:
            continue
        r, g, b, a = p[x, y]
        if a == 0 or not is_bg((r, g, b, a)):
            continue
        p[x, y] = (r, g, b, 0)
        stack.append((x + 1, y)); stack.append((x - 1, y))
        stack.append((x, y + 1)); stack.append((x, y - 1))
    return img


red_img = im.crop(red_crop)
green_img = im.crop(green_crop)
red_img = make_transparent(red_img)
green_img = make_transparent(green_img)
red_img.save(OUT + r"\player-disconnect.png")
green_img.save(OUT + r"\player-connect.png")
print("saved disconnect", red_img.size, "connect", green_img.size)
print("RED origin:", red_crop[0], red_crop[1])
print("GREEN origin:", green_crop[0], green_crop[1])
