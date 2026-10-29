"""Daily snapshot of everything a cafe would lose with the server: data/backups/limybot-<date>.zip holds the database
and the photos. The last KEEP days are kept; copy the folder off the server (see README) to survive a dead disk.
Restore: stop the process, unzip into data/ (limybot.db and photos/), start again."""

import asyncio
import tempfile
import zipfile
from datetime import date
from pathlib import Path

from .config import Settings
from .store import Store

KEEP = 14


async def make_backup(store: Store, settings: Settings, day: date) -> Path:
    folder = settings.backup_dir
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"limybot-{day.isoformat()}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "limybot.db"
        store.backup_to(db)  # on the event loop: the connection belongs to it
        await asyncio.to_thread(_zip, db, settings.media_dir, path)
    for old in sorted(folder.glob("limybot-*.zip"))[:-KEEP]:
        old.unlink()
    return path


def _zip(db: Path, media: Path, path: Path) -> None:
    partial = path.with_suffix(".part")
    with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(db, "limybot.db")
        for photo in sorted(media.glob("*")) if media.exists() else []:
            z.write(photo, f"photos/{photo.name}", compress_type=zipfile.ZIP_STORED)  # JPEGs do not compress
    partial.replace(path)
