"""Uploaded images — item photos, cafe logo and cover — as normalised JPEGs in data/photos, each with a small thumbnail
for the Mini App grid: phones on slow mobile internet load that first."""

import secrets
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

PHOTO_SIZE = 1280
THUMB_SIZE = 480


def thumb(name: str) -> str:
    return name.removesuffix(".jpg") + "_s.jpg"


def _save(img: Image.Image, path: Path, size: int) -> None:
    copy = img.copy()
    copy.thumbnail((size, size))
    copy.save(path, "JPEG", quality=85)


def save_photo(data: bytes, media_dir: Path) -> str:
    """Phone photos are big and rotated via EXIF; logos come as PNG with transparency, which goes white."""
    img = ImageOps.exif_transpose(Image.open(BytesIO(data)))
    if img.mode != "RGB":
        rgba = img.convert("RGBA")
        img = Image.new("RGB", rgba.size, "white")
        img.paste(rgba, mask=rgba.getchannel("A"))
    name = f"{secrets.token_hex(8)}.jpg"
    _save(img, media_dir / name, PHOTO_SIZE)
    _save(img, media_dir / thumb(name), THUMB_SIZE)
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
