from functools import lru_cache
from math import sin
from pathlib import Path

import cv2
import emoji
import numpy as np
from PIL import Image, ImageDraw, ImageFont


BORDER_COLORS = {"white": "#FFFFFF", "neon": "#52FFDB", "pink": "#FF78BB", "gold": "#FFD166"}
STICKER_PRESETS = ("❤️", "⭐", "⚡", "🔥", "😂", "😍", "👍", "👏", "🎉", "🚀", "💯", "✨",
                   "🙌", "💡", "😎", "🤩", "🎬", "💬")
STICKER_POSITIONS = {"top": "Сверху", "bottom": "Снизу"}
MAX_STICKERS_PER_SIDE = 3
STICKER_SIZE = 44
_LEGACY_STICKERS = {"heart": "❤️", "star": "⭐", "bolt": "⚡"}


def timed_events(settings):
    return [settings["monochrome"]] if "monochrome" in settings else []


def sticker_rows(settings):
    saved = settings.get("stickers", {})
    rows = {side: saved[side] for side in STICKER_POSITIONS if saved.get(side)}
    if rows:
        return rows
    legacy = [symbol for name, symbol in _LEGACY_STICKERS.items() if name in saved]
    return {"bottom": legacy} if legacy else {}


def film_frame(canvas, number):
    flicker = 2 * sin(number * 1.17)
    warm = cv2.transform(canvas, np.array([
        [0.86, 0, 0.02, 9 + flicker],
        [0, 0.94, 0.02, 10 + flicker],
        [0, 0.03, 1.02, 12 + flicker],
    ], dtype=np.float32))
    grain = np.random.default_rng(number).integers(-8, 9, (*canvas.shape[:2], 1), dtype=np.int16)
    return np.clip(warm + grain, 0, 255).astype(np.uint8)


@lru_cache(maxsize=1)
def sticker_font():
    for path, size in (
        ("/System/Library/Fonts/Apple Color Emoji.ttc", 96),
        ("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", 109),
    ):
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return None


def emoji_image(symbol, font):
    left, top, right, bottom = font.getbbox(symbol)
    if right <= left or bottom <= top:
        return None
    image = Image.new("RGBA", (right - left, bottom - top))
    ImageDraw.Draw(image).text((-left, -top), symbol, font=font, embedded_color=True)
    bounds = image.getbbox()
    return image.crop(bounds) if bounds else None


@lru_cache(maxsize=192)
def sticker_image(symbol):
    font = sticker_font()
    if font is None or not 0 < font.getlength(symbol) <= font.getlength("😀") * 1.25:
        return None
    image = emoji_image(symbol, font)
    if image is None:
        return None
    missing = emoji_image("\u0378", font)
    if missing is not None and image.size == missing.size and image.tobytes() == missing.tobytes():
        return None
    image.thumbnail((STICKER_SIZE, STICKER_SIZE), Image.Resampling.LANCZOS)
    rgba = np.asarray(image)
    return rgba[:, :, :3][:, :, ::-1], rgba[:, :, 3:4].astype(np.float32) / 255


def parse_stickers(text):
    if len(text) > 96:
        return None
    matches = emoji.emoji_list(text)
    if not 1 <= len(matches) <= MAX_STICKERS_PER_SIDE:
        return None
    symbols = []
    end = 0
    for match in matches:
        symbol = match["emoji"]
        if text[end:match["match_start"]].strip() or sticker_image(symbol) is None:
            return None
        symbols.append(symbol)
        end = match["match_end"]
    return None if text[end:].strip() else symbols


def add_stickers(canvas, settings):
    for side, symbols in sticker_rows(settings).items():
        images = [image for symbol in symbols if (image := sticker_image(symbol)) is not None]
        if not images:
            continue
        gap = 8
        width = sum(pixels.shape[1] for pixels, _ in images) + gap * (len(images) - 1)
        left = (canvas.shape[1] - width) // 2
        center_y = 43 if side == "top" else canvas.shape[0] - 43
        for pixels, alpha in images:
            height, width = pixels.shape[:2]
            top = center_y - height // 2
            region = canvas[top:top + height, left:left + width]
            region[:] = region * (1 - alpha) + pixels * alpha
            left += width + gap
