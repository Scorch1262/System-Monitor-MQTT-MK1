"""Erzeugt assets/icon.ico und assets/icon.png (Chip mit Amber-Rahmen und Teal-Balken)."""
import os
from PIL import Image, ImageDraw

def draw(size):
    s = size / 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8*s, 8*s, 248*s, 248*s], radius=44*s, fill=(14, 18, 22, 255),
                        outline=(245, 165, 36, 255), width=max(1, int(10*s)))
    # Pins
    for i in range(3):
        x = (88 + i*40) * s
        for y0, y1 in ((30, 56), (200, 226)):
            d.line([x, y0*s, x, y1*s], fill=(245, 165, 36, 255), width=max(1, int(9*s)))
    # Balken (Auslastung)
    bars = [70, 120, 95, 150]
    for i, h in enumerate(bars):
        x0 = (58 + i*38) * s
        d.rounded_rectangle([x0, (190 - h)*s, x0 + 26*s, 190*s], radius=5*s, fill=(45, 212, 191, 255))
    return img

base = os.path.join(os.path.dirname(__file__), "..", "assets")
big = draw(256)
big.save(os.path.join(base, "icon.png"))
big.save(os.path.join(base, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("Icon erzeugt")
