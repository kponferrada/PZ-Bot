"""Split the two-panel banner image into red (disconnect, top) + green (connect, bottom)."""
from PIL import Image

SRC = r"C:\Users\keym\AppData\Roaming\Hermes\composer-images\image_4f4780.png"
OUT_DIR = r"C:\Users\keym\Documents\PZ-Tambayan-Bot\bot\assets"

im = Image.open(SRC).convert("RGB")
W, H = im.size
mid = H // 2
print("size", W, H, "split at", mid)

top = im.crop((0, 0, W, mid))
bottom = im.crop((0, mid, W, H))
top.save(OUT_DIR + r"\player-disconnect.png")
bottom.save(OUT_DIR + r"\player-connect.png")
print("saved", OUT_DIR + r"\player-disconnect.png", top.size)
print("saved", OUT_DIR + r"\player-connect.png", bottom.size)
