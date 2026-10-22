import os

from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# rounded square gradient background
radius = 56
grad = Image.new("RGB", (S, S))
gd = ImageDraw.Draw(grad)
top = (47, 111, 237)
bottom = (24, 62, 156)
for y in range(S):
    t = y / (S - 1)
    gd.line([(0, y), (S, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=radius, fill=255)
img.paste(grad, (0, 0), mask)

d = ImageDraw.Draw(img)

# sun / moon
d.ellipse([166, 34, 216, 84], fill=(255, 236, 170, 255))

# mountains (two tones)
d.polygon([(18, 190), (78, 108), (128, 190)], fill=(255, 255, 255, 235))
d.polygon([(88, 190), (150, 96), (214, 190)], fill=(226, 236, 255, 230))

# sea waves
for i, y in enumerate((196, 210, 224)):
    w = 255 - i * 14
    d.arc([S // 2 - w // 2, y - 18, S // 2 + w // 2, y + 18], 200, 340, fill=(255, 255, 255, 210), width=7)

assets = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
os.makedirs(assets, exist_ok=True)
img.save(os.path.join(assets, "icon_256.png"))
img.save(
    os.path.join(assets, "app.ico"),
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print("icon ok")
