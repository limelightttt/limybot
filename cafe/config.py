"""Everything that differs between cafes lives in clients/<name>.json; secrets come from .env."""

import json
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Item(BaseModel):
    id: str
    name: str
    price: int


class Category(BaseModel):
    category: str
    items: list[Item]


class Hours(BaseModel):
    open: time
    close: time


class Delivery(BaseModel):
    enabled: bool = False
    min_order: int = 0
    fee: int = 0


class Client(BaseModel):
    name: str
    greeting: str
    currency: str = "сум"
    timezone: str = "Asia/Tashkent"
    hours: Hours | None = None
    pickup_address: str = ""
    delivery: Delivery = Field(default_factory=Delivery)
    payment_note: str = "Оплата при получении"
    menu: list[Category]

    def is_open(self, now: datetime | None = None) -> bool:
        if not self.hours:
            return True
        t = (now or datetime.now(ZoneInfo(self.timezone))).time()
        o, c = self.hours.open, self.hours.close
        return o <= t <= c if o <= c else t >= o or t <= c  # also hours past midnight

    def money(self, amount: int) -> str:
        return f"{amount:,}".replace(",", " ") + f" {self.currency}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    bot_token: str = ""
    client: str = "demo_coffee"
    staff_chat_id: int | None = None
    admin_ids: str = ""
    db_path: Path = ROOT / "data" / "cafe.db"

    @property
    def admins(self) -> set[int]:
        return {int(x) for x in self.admin_ids.replace(" ", "").split(",") if x}


def load_client(name: str) -> Client:
    data = json.loads((ROOT / "clients" / f"{name}.json").read_text("utf-8"))
    client = Client.model_validate(data)
    ids = [i.id for c in client.menu for i in c.items]
    if len(ids) != len(set(ids)):
        raise ValueError(f"clients/{name}.json: menu item ids must be unique")
    return client
