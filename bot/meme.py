"""Render text memes for the bot.

The model asks with a trailing "[梗图: 短文字]"; the adapter picks a sticker
from the library as the base image (or draws a gradient card when the library
is empty), stamps the text in a classic bottom band and sends it as an image.
"""

from __future__ import annotations

import colorsys
import hashlib
import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

MAX_CHARS = 24
CANVAS = 512

_CJK_FONTS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
)
_FALLBACK_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

_MEME_RE = re.compile(r"\[梗图[:：]\s*([^\]\n]{1,60})\]")


class MemeError(RuntimeError):
    pass


def extract_meme_marker(text: str) -> tuple[str, str | None]:
    """Split a "[梗图: 文字]" marker out of a reply; return (visible, text)."""
    match = _MEME_RE.search(text or "")
    if match is None:
        return text, None
    visible = _MEME_RE.sub("", text, count=1).strip()
    return visible, match.group(1).strip()


def available() -> bool:
    return any(Path(path).is_file() for path in _CJK_FONTS)


def _font(size: int):
    from PIL import ImageFont

    for path in _CJK_FONTS + _FALLBACK_FONTS:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _gradient(size: int, seed: str):
    from PIL import Image, ImageDraw

    hue = int(hashlib.sha1(seed.encode("utf-8")).hexdigest()[:6], 16) % 360 / 360
    top = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.45, 0.92))
    bottom = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.65, 0.42))
    image = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(image)
    for y in range(size):
        ratio = y / max(1, size - 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
        draw.line([(0, y), (size, y)], fill=color)
    return image


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        if char == "\n":
            lines.append(current)
            current = ""
            continue
        trial = current + char
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines[:4] or [""]


def render_meme(
    text: str,
    *,
    base_image: str | Path | None = None,
    output_dir: str | Path = "/var/lib/qqbot/memes",
) -> Path:
    """Draw `text` onto `base_image` (or a gradient card) and return the file."""
    try:
        from PIL import Image, ImageDraw, ImageOps
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise MemeError("Pillow is not installed") from exc

    text = " ".join((text or "").split())[:MAX_CHARS]
    if not text:
        raise MemeError("empty meme text")

    canvas = None
    if base_image:
        source = Path(base_image)
        if source.is_file():
            try:
                image = Image.open(source)
                image = ImageOps.exif_transpose(image).convert("RGB")
                image.thumbnail((CANVAS, CANVAS))
                canvas = Image.new("RGB", (CANVAS, CANVAS), "white")
                canvas.paste(
                    image,
                    ((CANVAS - image.width) // 2, (CANVAS - image.height) // 2),
                )
            except OSError as exc:
                log.warning("cannot use meme base %s: %s", source, exc)
    if canvas is None:
        canvas = _gradient(CANVAS, text)

    canvas = canvas.convert("RGBA")
    font = _font(44)
    probe = ImageDraw.Draw(canvas)
    lines = _wrap(probe, text, font, CANVAS - 48)
    line_height = 54
    band_height = line_height * len(lines) + 32

    band = Image.new("RGBA", (CANVAS, band_height), (0, 0, 0, 150))
    canvas.alpha_composite(band, (0, CANVAS - band_height))
    draw = ImageDraw.Draw(canvas)
    y = CANVAS - band_height + 16
    for line in lines:
        width = draw.textlength(line, font=font)
        draw.text(
            ((CANVAS - width) / 2, y),
            line,
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=3,
            stroke_fill=(0, 0, 0, 255),
        )
        y += line_height

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    target = output / f"meme-{int(time.time())}-{digest}.png"
    canvas.convert("RGB").save(target, "PNG")
    return target
