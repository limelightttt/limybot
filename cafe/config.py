"""Process settings from .env. Everything about a particular cafe lives in the database (see store.py)."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    db_path: Path = ROOT / "data" / "limybot.db"
    # the public https address: admin login links and the Mini App are built from it
    public_url: str = "http://localhost:8000"
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    # how cafe owners reach you about the subscription, e.g. @limybot_support; shown in their admin
    support: str = ""

    @property
    def media_dir(self) -> Path:
        return self.db_path.parent / "photos"

    @property
    def backup_dir(self) -> Path:
        return self.db_path.parent / "backups"
