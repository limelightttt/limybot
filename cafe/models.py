"""Plain data the store returns and the bot and web admin render."""

from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

STATUSES = {
    "new": "Новый",
    "accepted": "Готовится",
    "ready": "Готов",
    "done": "Выдан",
    "cancelled": "Отменён",
}
# what staff can do with an order in each status
NEXT = {
    "new": ["accepted", "cancelled"],
    "accepted": ["ready", "cancelled"],
    "ready": ["done"],
}
# online providers (payme, click) will be added here; their payment_status flips to "paid" from a callback
PAYMENT_METHODS = {"cash": "При получении"}
PAYMENT_STATUSES = {"pending": "Не оплачен", "paid": "Оплачен"}


def money(amount: int, currency: str = "сум") -> str:
    return f"{amount:,}".replace(",", " ") + f" {currency}"


@dataclass
class Cafe:
    id: int
    slug: str
    bot_token: str
    bot_username: str | None
    name: str
    greeting: str
    currency: str
    timezone: str
    open_time: time | None
    close_time: time | None
    pickup_address: str
    delivery_enabled: bool
    delivery_min: int
    delivery_fee: int
    payment_note: str
    staff_chat_id: int | None
    invite_code: str | None

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.timezone))

    def today(self) -> datetime:
        return self.now().replace(hour=0, minute=0, second=0, microsecond=0)

    def is_open(self, now: datetime | None = None) -> bool:
        o, c = self.open_time, self.close_time
        if o is None or c is None:
            return True
        t = (now or self.now()).time()
        return o <= t <= c if o <= c else t >= o or t <= c  # also hours past midnight

    def money(self, amount: int) -> str:
        return money(amount, self.currency)

    def invite_link(self) -> str | None:
        if self.invite_code and self.bot_username:
            return f"https://t.me/{self.bot_username}?start=join-{self.invite_code}"
        return None


@dataclass
class Option:
    id: int
    group_id: int
    name: str
    price: int


@dataclass
class ModifierGroup:
    id: int
    name: str
    multiple: bool  # False: pick exactly one (size, milk); True: any number (syrups)
    options: list[Option] = field(default_factory=list)


@dataclass
class Item:
    id: int
    category_id: int
    name: str
    description: str
    price: int
    photo: str | None
    photo_file_id: str | None
    available: bool
    groups: list[ModifierGroup] = field(default_factory=list)

    def default_options(self) -> list[int]:
        return [g.options[0].id for g in self.groups if not g.multiple and g.options]


@dataclass
class Category:
    id: int
    name: str
    items: list[Item] = field(default_factory=list)


@dataclass
class CartLine:
    id: int
    item: Item
    options: list[Option]
    qty: int

    @property
    def unit(self) -> int:
        return self.item.price + sum(o.price for o in self.options)

    @property
    def sum(self) -> int:
        return self.unit * self.qty

    @property
    def title(self) -> str:
        return title(self.item.name, [o.name for o in self.options])


def title(name: str, options: list[str]) -> str:
    return f"{name} ({', '.join(options)})" if options else name


@dataclass
class Order:
    id: int
    cafe_id: int
    number: int
    user_id: int
    customer: str
    phone: str
    method: str
    address: str | None
    comment: str | None
    lines: list[dict]  # snapshot at checkout: {"name", "options", "qty", "unit"}
    subtotal: int
    delivery_fee: int
    total: int
    status: str
    payment_method: str
    payment_status: str
    created_at: datetime

    def lines_text(self, cafe: Cafe) -> str:
        return "\n".join(
            f"{title(ln['name'], ln['options'])} × {ln['qty']} — {cafe.money(ln['unit'] * ln['qty'])}"
            for ln in self.lines
        )
