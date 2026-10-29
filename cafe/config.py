"""Process settings from .env. Everything about a particular cafe lives in the database (see store.py)."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    db_path: Path = ROOT / "data" / "limybot.db"
    # address the owner's phone opens the admin at; login links from /admin are built from it
    public_url: str = "http://localhost:8000"
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    @property
    def media_dir(self) -> Path:
        return self.db_path.parent / "photos"
