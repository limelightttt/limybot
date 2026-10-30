"""Plain data the store returns and the bot, the Mini App and the web admin render."""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

STATUSES = {
    "new": "Новый",
    "accepted": "Принят",
    "cooking": "Готовится",
    "ready": "Готов",
    "done": "Выдан",
    "courier": "Передан курьеру",
    "delivered": "Доставлен",
    "cancelled": "Отменён",
}
# the way an order goes, by how the customer gets it; the last step means it is in the customer's hands
FLOW = {
    "pickup": ["new", "accepted", "cooking", "ready", "done"],
    "delivery": ["new", "accepted", "cooking", "courier", "delivered"],
}
STAFF_CANCELLABLE = {"new", "accepted", "cooking"}  # nothing is cancelled once it is ready or on the way
ACTIVE = sorted({s for flow in FLOW.values() for s in flow[:-1]})
HANDED_OVER = {flow[-1] for flow in FLOW.values()}

# online providers (payme, click) will be added here; their payment_status flips to "paid" from a callback
PAYMENT_METHODS = {"cash": "При получении", "transfer": "Перевод на карту"}
PAYMENT_STATUSES = {"pending": "Не оплачен", "claimed": "Клиент сообщил о переводе", "paid": "Оплачен"}


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
    staff_chat_id: int | None
    invite_code: str | None
    brand_color: str
    logo: str | None
    cover: str | None
    card_number: str
    card_holder: str
    paid_until: date | None  # set by the operator: python -m cafe paid <name> <date>
    enabled: bool
    courier_chat_id: int | None  # the couriers' group: they take orders on their way and mark them delivered
    finance_chat_id: int | None  # the owner's money group: receipts, transfer screenshots, daily reports
    day_start: int  # hour a 24/7 cafe's business day starts; with opening hours the day starts at opening
    reported_until: datetime | None  # end of the last business day whose report went out

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.timezone))

    def day_began(self, now: datetime | None = None) -> datetime:
        """Start of the business day: opening time, or day_start for a cafe open around the clock. A night cafe
        (18:00–02:00) is one day, not two."""
        now = now or self.now()
        start = self.open_time or time(self.day_start)
        began = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        return began if began <= now else began - timedelta(days=1)

    def day_ends(self, began: datetime) -> datetime:
        """Closing time of the business day that began then; the next day's start for a 24/7 cafe."""
        if self.open_time is None or self.close_time is None:
            return began + timedelta(days=1)
        length = datetime.combine(began.date(), self.close_time) - datetime.combine(began.date(), self.open_time)
        return began + (length if length > timedelta(0) else length + timedelta(days=1))

    def is_open(self, now: datetime | None = None) -> bool:
        o, c = self.open_time, self.close_time
        if o is None or c is None:
            return True
        t = (now or self.now()).time()
        return o <= t <= c if o <= c else t >= o or t <= c  # also hours past midnight

    def hours(self) -> str:
        return f"{self.open_time:%H:%M}–{self.close_time:%H:%M}" if self.open_time and self.close_time else ""

    def active(self) -> bool:
        """Takes orders: not switched off by the operator and the subscription is paid."""
        return self.enabled and (self.paid_until is None or self.paid_until >= self.now().date())

    def payment_methods(self) -> list[str]:
        return ["cash", "transfer"] if self.card_number else ["cash"]

    def money(self, amount: int) -> str:
        return money(amount, self.currency)

    def invite_link(self) -> str | None:
        if self.invite_code and self.bot_username:
            return f"https://t.me/{self.bot_username}?start=join-{self.invite_code}"
        return None


@dataclass
class Customer:
    phone: str | None
    address: str | None
    lang: str


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
    """An item with a valid choice of options, priced by the current menu."""

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
    # snapshot at checkout: {"item_id", "option_ids", "name", "options", "qty", "unit"}; orders placed before
    # the Mini App have no ids and cannot be repeated
    lines: list[dict]
    subtotal: int
    delivery_fee: int
    total: int
    status: str
    payment_method: str
    payment_status: str
    created_at: datetime
    courier_id: int | None
    courier_name: str | None
    change_from: int | None  # cash: the banknote the customer pays with, so the courier brings change

    @property
    def next_statuses(self) -> list[str]:
        """What staff can do with the order now."""
        flow = FLOW[self.method]
        if self.status not in flow[:-1]:
            return []
        step = [flow[flow.index(self.status) + 1]]
        return step + (["cancelled"] if self.status in STAFF_CANCELLABLE else [])

    @property
    def awaits_transfer(self) -> bool:
        return self.payment_method == "transfer" and self.payment_status != "paid" and self.status != "cancelled"

    def lines_text(self, cafe: Cafe) -> str:
        return "\n".join(
            f"{title(ln['name'], ln['options'])} × {ln['qty']} — {cafe.money(ln['unit'] * ln['qty'])}"
            for ln in self.lines
        )
