from functools import lru_cache
from math import cos, pi, sin

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter


BORDER_COLORS = {"white": "#FFFFFF", "neon": "#52FFDB", "pink": "#FF78BB", "gold": "#FFD166"}
STICKERS = {"heart": "❤️ Сердце", "star": "⭐ Звезда", "bolt": "⚡ Молния"}
STICKER_SECONDS = 1.5


def timed_events(settings):
    events = set(settings.get("stickers", {}).values())
    if "monochrome" in settings:
        events.add(settings["monochrome"])
    return sorted(events)


def film_frame(canvas, number):
    flicker = 2 * sin(number * 1.17)
    warm = cv2.transform(canvas, np.array([
        [0.86, 0, 0.02, 9 + flicker],
        [0, 0.94, 0.02, 10 + flicker],
        [0, 0.03, 1.02, 12 + flicker],
    ], dtype=np.float32))
    grain = np.random.default_rng(number).integers(-8, 9, (*canvas.shape[:2], 1), dtype=np.int16)
    return np.clip(warm + grain, 0, 255).astype(np.uint8)


@lru_cache(maxsize=3)
def sticker_image(name):
    shape = Image.new("L", (144, 144))
    draw = ImageDraw.Draw(shape)
    if name == "heart":
        draw.ellipse((13, 23, 80, 91), fill=255)
        draw.ellipse((63, 23, 130, 91), fill=255)
        draw.polygon([(17, 69), (72, 126), (126, 69), (72, 42)], fill=255)
        color = (255, 87, 129)
    elif name == "star":
        points = []
        for index in range(10):
            angle = index * pi / 5 - pi / 2
            radius = 61 if index % 2 == 0 else 29
            points.append((72 + cos(angle) * radius, 74 + sin(angle) * radius))
        draw.polygon(points, fill=255)
        color = (255, 190, 53)
    else:
        draw.polygon([(69, 13), (114, 13), (89, 59), (120, 59),
                      (55, 132), (68, 80), (32, 80)], fill=255)
        color = (255, 211, 65)
    outline = Image.new("RGBA", shape.size, (255, 255, 255, 255))
    outline.putalpha(shape.filter(ImageFilter.MaxFilter(11)))
    fill = Image.new("RGBA", shape.size, (*color, 255))
    fill.putalpha(shape)
    outline.alpha_composite(fill)
    rgba = np.asarray(outline.resize((72, 72), Image.Resampling.LANCZOS))
    return rgba[:, :, :3][:, :, ::-1], rgba[:, :, 3:4].astype(np.float32) / 255


def add_stickers(canvas, stickers, elapsed):
    positions = {"heart": (98, 158), "star": (310, 158), "bolt": (310, 265)}
    for name, second in stickers.items():
        if second <= elapsed < second + STICKER_SECONDS:
            x, y = positions[name]
            pixels, alpha = sticker_image(name)
            height, width = pixels.shape[:2]
            region = canvas[y:y + height, x:x + width]
            region[:] = region * (1 - alpha) + pixels * alpha
