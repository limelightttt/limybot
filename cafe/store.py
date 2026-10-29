"""SQLite: carts, orders and owner edits of the menu (price, stop-list) on top of the client config."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import Client, Item

STATUSES = {
    "new": "Новый",
    "accepted": "Готовится",
    "ready": "Готов",
    "done": "Выдан",
    "cancelled": "Отменён",
}

SCHEMA = """
create table if not exists overrides (item_id text primary key, price integer, available integer not null default 1);
create table if not exists carts (user_id integer, item_id text, qty integer not null, primary key (user_id, item_id));
create table if not exists orders (
  id integer primary key autoincrement, user_id integer not null, customer text not null, phone text not null,
  method text not null, address text, comment text, lines text not null, total integer not null,
  status text not null default 'new', created_at text not null
);
"""


@dataclass
class Line:
    item: Item
    qty: int

    @property
    def sum(self) -> int:
        return self.item.price * self.qty


@dataclass
class Order:
    id: int
    user_id: int
    customer: str
    phone: str
    method: str
    address: str | None
    comment: str | None
    lines: str
    total: int
    status: str
    created_at: str


class Store:
    def __init__(self, path: Path, client: Client):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.client = client

    # --- menu: config + owner edits ---
    def menu(self, only_available: bool = True) -> list[tuple[str, list[Item]]]:
        edits = {r[0]: (r[1], bool(r[2])) for r in self.db.execute("select * from overrides")}
        out = []
        for cat in self.client.menu:
            items = []
            for it in cat.items:
                price, available = edits.get(it.id, (None, True))
                if available or not only_available:
                    items.append(it.model_copy(update={"price": price}) if price else it)
            if items:
                out.append((cat.category, items))
        return out

    def item(self, item_id: str) -> Item | None:
        return next((i for _, items in self.menu() for i in items if i.id == item_id), None)

    def is_known(self, item_id: str) -> bool:
        return any(i.id == item_id for c in self.client.menu for i in c.items)

    def set_price(self, item_id: str, price: int) -> None:
        with self.db:
            self.db.execute(
                "insert into overrides (item_id, price) values (?, ?) on conflict(item_id) do update set price = ?",
                (item_id, price, price),
            )

    def set_available(self, item_id: str, available: bool) -> None:
        with self.db:
            self.db.execute(
                "insert into overrides (item_id, available) values (?, ?) "
                "on conflict(item_id) do update set available = excluded.available",
                (item_id, int(available)),
            )

    # --- cart ---
    def add(self, user_id: int, item_id: str, delta: int = 1) -> None:
        with self.db:
            self.db.execute(
                "insert into carts values (?, ?, ?) on conflict(user_id, item_id) do update set qty = qty + ?",
                (user_id, item_id, delta, delta),
            )
            self.db.execute("delete from carts where qty <= 0")

    def cart(self, user_id: int) -> list[Line]:
        rows = self.db.execute("select item_id, qty from carts where user_id = ?", (user_id,)).fetchall()
        # items removed from the menu (stop-list) silently drop out of the cart
        return [Line(it, qty) for item_id, qty in rows if (it := self.item(item_id))]

    def clear(self, user_id: int) -> None:
        with self.db:
            self.db.execute("delete from carts where user_id = ?", (user_id,))

    # --- orders ---
    def place(
        self,
        user_id: int,
        customer: str,
        phone: str,
        method: str,
        address: str | None,
        comment: str | None,
        fee: int,
    ) -> Order | None:
        lines = self.cart(user_id)
        if not lines:
            return None
        text = "\n".join(f"{ln.item.name} × {ln.qty} — {self.client.money(ln.sum)}" for ln in lines)
        total = sum(ln.sum for ln in lines) + fee
        with self.db:
            cur = self.db.execute(
                "insert into orders (user_id, customer, phone, method, address, comment, lines, total, created_at)"
                " values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    customer,
                    phone,
                    method,
                    address,
                    comment,
                    text,
                    total,
                    datetime.now(UTC).isoformat(),
                ),
            )
            self.db.execute("delete from carts where user_id = ?", (user_id,))
        return self.order(cur.lastrowid)

    def order(self, order_id: int) -> Order | None:
        row = self.db.execute("select * from orders where id = ?", (order_id,)).fetchone()
        return Order(*row) if row else None

    def set_status(self, order_id: int, status: str) -> None:
        with self.db:
            self.db.execute("update orders set status = ? where id = ?", (status, order_id))

    def stats(self, since: datetime) -> tuple[int, int]:
        n, total = self.db.execute(
            "select count(*), coalesce(sum(total), 0) from orders where status != 'cancelled' and created_at >= ?",
            (since.astimezone(UTC).isoformat(),),
        ).fetchone()
        return n, total
