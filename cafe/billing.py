"""Subscriptions: a cafe pays the operator (a card transfer, recorded in /ops) and its bot takes orders until
paid_until plus GRACE_DAYS. Nothing is ever deleted for not paying: the bot only stops taking orders and starts
again the moment a payment is recorded. Pure functions here; the reminders that go out are in notify.py."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta

PRICE = 299_000  # a month, in sum; the operator changes it in /ops (meta "price")
TRIAL_DAYS = 14  # a new cafe works free this long
GRACE_DAYS = 2  # after paid_until the bot still takes orders while the owner is reminded to pay
SOON_DAYS = 3  # "ends soon" from this many days before paid_until

# what the operator sees about a subscription (Cafe.subscription())
SUBSCRIPTION = {
    "unlimited": "Без срока",
    "ok": "Оплачено",
    "soon": "Скоро заканчивается",
    "grace": "Не оплачено — отсрочка",
    "off": "Не оплачено — заказы не принимаются",
    "paused": "Приостановлено вами",
}


@dataclass
class Payment:
    id: int
    cafe_id: int
    amount: int
    months: int
    until: date  # paid_until after this payment
    prev_until: date | None  # paid_until before it: cancelling the payment brings it back
    note: str
    created_at: datetime


def add_months(day: date, months: int) -> date:
    """The same day `months` later; the 31st becomes the month's last day when that month is shorter."""
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, monthrange(year, month)[1]))


def extend(paid_until: date | None, today: date, months: int) -> date:
    """paid_until after paying for `months`. Paid on time or within the grace days — continues from the old date,
    so the grace days are not free; paid later (or the first payment) — counts from today."""
    if paid_until and paid_until >= today - timedelta(days=GRACE_DAYS):
        return add_months(paid_until, months)
    return add_months(today, months)


def state(enabled: bool, paid_until: date | None, today: date) -> tuple[str, int | None]:
    """(one of SUBSCRIPTION, days left until paid_until: 0 is the last paid day, negative — days past it)."""
    if not enabled:
        return "paused", None
    if paid_until is None:
        return "unlimited", None
    left = (paid_until - today).days
    if left > SOON_DAYS:
        return "ok", left
    if left >= 0:
        return "soon", left
    if left >= -GRACE_DAYS:
        return "grace", left
    return "off", left


def last_order_day(paid_until: date) -> date:
    return paid_until + timedelta(days=GRACE_DAYS)


def days(n: int) -> str:
    n = abs(n)
    word = (
        "день" if n % 10 == 1 and n % 100 != 11 else "дня" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "дней"
    )
    return f"{n} {word}"
