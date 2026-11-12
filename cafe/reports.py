"""Reports for the owner: money and orders over a period, split the way a cafe counts cash at the end of the day —
by payment method, goods vs delivery, cancelled separately — the best sellers, every day of the period, and a CSV
of the orders that opens in Excel. All sums come from the orders' own snapshots (what was charged then), so editing
the menu never changes past reports. Cancelled orders are counted apart and never in the revenue."""

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .models import PAYMENT_METHODS, PAYMENT_STATUSES, STATUSES, Cafe, title
from .store import Store

PERIODS = {
    "today": "Сегодня",
    "yesterday": "Вчера",
    "week": "7 дней",
    "month": "Этот месяц",
    "last_month": "Прошлый месяц",
}


@dataclass
class Report:
    since: date
    until: date  # inclusive
    orders: int = 0
    revenue: int = 0  # what guests pay: goods + delivery
    goods: int = 0
    delivery: int = 0
    delivery_orders: int = 0
    pickup_orders: int = 0
    by_method: dict[str, int] = field(default_factory=dict)  # PAYMENT_METHODS key → sum
    paid: int = 0  # confirmed: handed over for cash, "payment received" for transfers
    unpaid_transfers: int = 0  # transfers staff have not confirmed yet
    cancelled: int = 0
    cancelled_sum: int = 0
    top: list[tuple[str, int, int]] = field(default_factory=list)  # (name with options, qty, sum)
    days: list[tuple[date, int, int]] = field(default_factory=list)  # (day, orders, revenue)

    @property
    def average(self) -> int:
        return self.revenue // self.orders if self.orders else 0


def period(cafe: Cafe, name: str) -> tuple[date, date]:
    today = cafe.now().date()
    if name == "yesterday":
        return today - timedelta(days=1), today - timedelta(days=1)
    if name == "week":
        return today - timedelta(days=6), today
    if name == "month":
        return today.replace(day=1), today
    if name == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    return today, today


def bounds(cafe: Cafe, since: date, until: date) -> tuple[datetime, datetime]:
    """The period's days in the cafe's time zone, as moments: [since 00:00, the day after until 00:00)."""
    tz = cafe.now().tzinfo
    start = datetime.combine(since, datetime.min.time(), tz)
    return start, datetime.combine(until + timedelta(days=1), datetime.min.time(), tz)


def build(store: Store, cafe: Cafe, since: date, until: date) -> Report:
    r = Report(since, until)
    tz = cafe.now().tzinfo
    items: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    days: dict[date, list[int]] = {since + timedelta(days=n): [0, 0] for n in range((until - since).days + 1)}
    for o in store.orders_between(cafe.id, *bounds(cafe, since, until)):
        if o.status == "cancelled":
            r.cancelled += 1
            r.cancelled_sum += o.total
            continue
        r.orders += 1
        r.revenue += o.total
        r.goods += o.subtotal
        r.delivery += o.delivery_fee
        if o.method == "delivery":
            r.delivery_orders += 1
        else:
            r.pickup_orders += 1
        r.by_method[o.payment_method] = r.by_method.get(o.payment_method, 0) + o.total
        if o.payment_status == "paid":
            r.paid += o.total
        elif o.payment_method == "transfer":
            r.unpaid_transfers += o.total
        for ln in o.lines:
            entry = items[title(ln["name"], ln["options"])]
            entry[0] += ln["qty"]
            entry[1] += ln["unit"] * ln["qty"]
        day = days.setdefault(o.created_at.astimezone(tz).date(), [0, 0])
        day[0] += 1
        day[1] += o.total
    r.top = sorted(((name, q, s) for name, (q, s) in items.items()), key=lambda t: (-t[2], t[0]))[:10]
    r.days = [(d, n, s) for d, (n, s) in sorted(days.items())]
    return r


def csv_export(store: Store, cafe: Cafe, since: date, until: date) -> bytes:
    """Every order of the period, one row each. Excel in Russian opens ";"-separated UTF-8 with a BOM as columns."""
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(
        [
            "Номер",
            "Дата",
            "Время",
            "Статус",
            "Получение",
            "Адрес",
            "Гость",
            "Телефон",
            "Состав",
            "Товары",
            "Доставка",
            "Итого",
            "Оплата",
            "Статус оплаты",
            "Комментарий",
        ]
    )
    tz = cafe.now().tzinfo
    for o in store.orders_between(cafe.id, *bounds(cafe, since, until)):
        at = o.created_at.astimezone(tz)
        w.writerow(
            [
                o.number,
                at.strftime("%d.%m.%Y"),
                at.strftime("%H:%M"),
                STATUSES[o.status],
                "Доставка" if o.method == "delivery" else "Самовывоз",
                o.address or "",
                o.customer,
                o.phone,
                "; ".join(f"{title(ln['name'], ln['options'])} × {ln['qty']}" for ln in o.lines),
                o.subtotal,
                o.delivery_fee,
                o.total,
                PAYMENT_METHODS[o.payment_method],
                PAYMENT_STATUSES[o.payment_status],
                o.comment or "",
            ]
        )
    return "﻿".encode() + out.getvalue().encode()


def daily_summary(store: Store, cafe: Cafe, day: date) -> str | None:
    """The owner's morning message about the day before; None if there were no orders."""
    r = build(store, cafe, day, day)
    if not r.orders and not r.cancelled:
        return None
    lines = [
        f"Итоги {day:%d.%m} — «{cafe.name}»",
        f"Заказов: {r.orders} на {cafe.money(r.revenue)} (средний чек {cafe.money(r.average)})",
    ]
    if r.delivery:
        lines.append(f"Из них доставка: {cafe.money(r.delivery)}")
    for method, total in r.by_method.items():
        lines.append(f"{PAYMENT_METHODS[method]}: {cafe.money(total)}")
    if r.unpaid_transfers:
        lines.append(f"⚠️ Переводы без подтверждения: {cafe.money(r.unpaid_transfers)}")
    if r.cancelled:
        lines.append(f"Отменено: {r.cancelled}")
    if r.top:
        lines.append("Чаще всего: " + ", ".join(f"{name} × {q}" for name, q, _ in r.top[:3]))
    lines.append("\nПодробно — /admin → Отчёты")
    return "\n".join(lines)
