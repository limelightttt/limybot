"""Uploaded images — item photos, cafe logo and cover — as normalised JPEGs in data/photos, each with a small thumbnail
for the Mini App grid: phones on slow mobile internet load that first."""

import secrets
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

PHOTO_SIZE = 1280
THUMB_SIZE = 480
MAX_BYTES = 15 * 1024 * 1024  # a phone photo is 2–8 MB
MAX_PIXELS = 50_000_000  # ~8000×6000; a few KB of PNG can claim 13000×13000 and take gigabytes to decode


class BadImage(ValueError):
    """Not a picture we can use; the message is for the person who uploaded it."""


def thumb(name: str) -> str:
    return name.removesuffix(".jpg") + "_s.jpg"


def _save(img: Image.Image, path: Path, size: int) -> None:
    copy = img.copy()
    copy.thumbnail((size, size))
    copy.save(path, "JPEG", quality=85)


def save_photo(data: bytes, media_dir: Path) -> str:
    """Phone photos are big and rotated via EXIF; logos come as PNG with transparency, which goes white.
    Anything that is not a sane picture — not an image, truncated, absurdly large — raises BadImage before it can
    eat the server's memory (every cafe runs in this one process)."""
    if len(data) > MAX_BYTES:
        raise BadImage("Файл больше 15 МБ — уменьшите фото")
    try:
        img = Image.open(BytesIO(data))
        if img.width * img.height > MAX_PIXELS:  # known from the header, before anything is decoded
            raise BadImage("Картинка слишком большая по размеру — нужна не больше 8000×6000")
        img.draft("RGB", (PHOTO_SIZE * 2, PHOTO_SIZE * 2))  # JPEGs decode straight at a smaller size
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            rgba = img.convert("RGBA")
            img = Image.new("RGB", rgba.size, "white")
            img.paste(rgba, mask=rgba.getchannel("A"))
        name = f"{secrets.token_hex(8)}.jpg"
        _save(img, media_dir / name, PHOTO_SIZE)
        _save(img, media_dir / thumb(name), THUMB_SIZE)
    except BadImage:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError) as e:
        raise BadImage("Не получилось прочитать картинку — загрузите JPG или PNG") from e
    return name


def drop_photo(media_dir: Path, name: str | None) -> None:
    if name:
        for n in (name, thumb(name)):
            (media_dir / n).unlink(missing_ok=True)


def ensure_thumbs(media_dir: Path) -> None:
    """Photos uploaded before thumbnails existed get one."""
    for path in media_dir.glob("*.jpg"):
        small = path.with_name(thumb(path.name))
        if not path.stem.endswith("_s") and not small.exists():
            with Image.open(path) as img:
                _save(img.convert("RGB"), small, THUMB_SIZE)
