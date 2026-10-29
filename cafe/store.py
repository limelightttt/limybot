"""SQLite storage for all cafes. Every query that takes an id from outside is scoped by cafe_id."""

import json
import secrets
import sqlite3
import time as clock
from datetime import UTC, datetime, time
from pathlib import Path

from .models import NEXT, Cafe, CartLine, Category, Item, ModifierGroup, Option, Order

SCHEMA = """
create table if not exists cafes (
  id integer primary key, slug text not null unique, bot_token text not null unique, bot_username text,
  name text not null, greeting text not null default '', currency text not null default 'сум',
  timezone text not null default 'Asia/Tashkent', open_time text, close_time text,
  pickup_address text not null default '', delivery_enabled integer not null default 0,
  delivery_min integer not null default 0, delivery_fee integer not null default 0,
  payment_note text not null default 'Оплата наличными или картой при получении',
  staff_chat_id integer, invite_code text
);
create table if not exists admins (
  cafe_id integer not null references cafes on delete cascade, user_id integer not null,
  name text not null default '', primary key (cafe_id, user_id)
);
create table if not exists categories (
  id integer primary key, cafe_id integer not null references cafes on delete cascade,
  name text not null, position integer not null default 0
);
create table if not exists items (
  id integer primary key, cafe_id integer not null references cafes on delete cascade,
  category_id integer not null references categories on delete cascade, name text not null,
  description text not null default '', price integer not null, photo text, photo_file_id text,
  available integer not null default 1, position integer not null default 0
);
create table if not exists modifier_groups (
  id integer primary key, cafe_id integer not null references cafes on delete cascade,
  name text not null, multiple integer not null default 0
);
create table if not exists modifier_options (
  id integer primary key, group_id integer not null references modifier_groups on delete cascade,
  name text not null, price integer not null default 0
);
create table if not exists item_modifiers (
  item_id integer not null references items on delete cascade,
  group_id integer not null references modifier_groups on delete cascade, primary key (item_id, group_id)
);
create table if not exists carts (
  id integer primary key, cafe_id integer not null, user_id integer not null,
  item_id integer not null references items on delete cascade, options text not null, qty integer not null,
  unique (cafe_id, user_id, item_id, options)
);
create table if not exists orders (
  id integer primary key, cafe_id integer not null references cafes on delete cascade, number integer not null,
  user_id integer not null, customer text not null, phone text not null, method text not null, address text,
  comment text, lines text not null, subtotal integer not null, delivery_fee integer not null,
  total integer not null, status text not null default 'new',
  payment_method text not null default 'cash', payment_status text not null default 'pending',
  created_at text not null, unique (cafe_id, number)
);
create table if not exists staff_messages (
  order_id integer not null references orders on delete cascade, chat_id integer not null,
  message_id integer not null, primary key (order_id, chat_id)
);
create table if not exists login_links (token text primary key, cafe_id integer not null,
  user_id integer not null, expires integer not null);
create table if not exists sessions (token text primary key, cafe_id integer not null,
  user_id integer not null, expires integer not null);
"""

CAFE_FIELDS = {
    "name",
    "greeting",
    "currency",
    "timezone",
    "open_time",
    "close_time",
    "pickup_address",
    "delivery_enabled",
    "delivery_min",
    "delivery_fee",
    "payment_note",
}
LOGIN_TTL = 15 * 60
SESSION_TTL = 30 * 24 * 3600


def _time(value: str | None) -> time | None:
    return time.fromisoformat(value) if value else None


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma foreign_keys = on")
        self.db.executescript(SCHEMA)

    def _insert(self, table: str, **values) -> int:
        cols = ", ".join(values)
        marks = ", ".join("?" * len(values))
        with self.db:
            return self.db.execute(f"insert into {table} ({cols}) values ({marks})", tuple(values.values())).lastrowid

    # --- cafes ---
    def add_cafe(self, slug: str, bot_token: str, **fields) -> int:
        return self._insert("cafes", slug=slug, bot_token=bot_token, **self._cafe_values(fields))

    def _cafe_values(self, fields: dict) -> dict:
        if unknown := set(fields) - CAFE_FIELDS:
            raise ValueError(f"unknown cafe fields: {unknown}")
        return {k: v.strftime("%H:%M") if isinstance(v, time) else v for k, v in fields.items()}

    def _cafe(self, row: sqlite3.Row | None) -> Cafe | None:
        if not row:
            return None
        d = dict(row)
        d.update(
            open_time=_time(d["open_time"]),
            close_time=_time(d["close_time"]),
            delivery_enabled=bool(d["delivery_enabled"]),
        )
        return Cafe(**d)

    def cafe(self, cafe_id: int) -> Cafe | None:
        return self._cafe(self.db.execute("select * from cafes where id = ?", (cafe_id,)).fetchone())

    def cafe_by_slug(self, slug: str) -> Cafe | None:
        return self._cafe(self.db.execute("select * from cafes where slug = ?", (slug,)).fetchone())

    def cafes(self) -> list[Cafe]:
        return [self._cafe(r) for r in self.db.execute("select * from cafes order by id")]

    def update_cafe(self, cafe_id: int, **fields) -> None:
        values = self._cafe_values(fields)
        sets = ", ".join(f"{k} = ?" for k in values)
        with self.db:
            self.db.execute(f"update cafes set {sets} where id = ?", (*values.values(), cafe_id))

    def set_token(self, cafe_id: int, token: str) -> None:
        with self.db:
            self.db.execute("update cafes set bot_token = ?, bot_username = null where id = ?", (token, cafe_id))

    def set_bot_username(self, cafe_id: int, username: str) -> None:
        with self.db:
            self.db.execute("update cafes set bot_username = ? where id = ?", (username, cafe_id))

    def set_staff_chat(self, cafe_id: int, chat_id: int | None) -> None:
        with self.db:
            self.db.execute("update cafes set staff_chat_id = ? where id = ?", (chat_id, cafe_id))

    # --- admins ---
    def new_invite(self, cafe_id: int) -> str:
        code = secrets.token_urlsafe(9)
        with self.db:
            self.db.execute("update cafes set invite_code = ? where id = ?", (code, cafe_id))
        return code

    def join(self, cafe_id: int, code: str, user_id: int, name: str) -> bool:
        """An invite link makes whoever opens it an admin; the code then stops working."""
        with self.db:
            cur = self.db.execute(
                "update cafes set invite_code = null where id = ? and invite_code = ?",
                (cafe_id, code),
            )
            if not cur.rowcount:
                return False
            self.db.execute("insert or replace into admins values (?, ?, ?)", (cafe_id, user_id, name))
        return True

    def admins(self, cafe_id: int) -> list[tuple[int, str]]:
        rows = self.db.execute("select user_id, name from admins where cafe_id = ? order by name", (cafe_id,))
        return [(r[0], r[1]) for r in rows]

    def is_admin(self, cafe_id: int, user_id: int) -> bool:
        row = self.db.execute("select 1 from admins where cafe_id = ? and user_id = ?", (cafe_id, user_id))
        return row.fetchone() is not None

    def remove_admin(self, cafe_id: int, user_id: int) -> None:
        with self.db:
            self.db.execute("delete from admins where cafe_id = ? and user_id = ?", (cafe_id, user_id))
            self.db.execute("delete from sessions where cafe_id = ? and user_id = ?", (cafe_id, user_id))

    # --- web admin login ---
    def create_login(self, cafe_id: int, user_id: int) -> str:
        token = secrets.token_urlsafe(24)
        self._insert(
            "login_links", token=token, cafe_id=cafe_id, user_id=user_id, expires=int(clock.time()) + LOGIN_TTL
        )
        return token

    def consume_login(self, token: str) -> str | None:
        """Trade a one-time link for a long-lived session token."""
        with self.db:
            row = self.db.execute(
                "delete from login_links where token = ? and expires > ? returning cafe_id, user_id",
                (token, int(clock.time())),
            ).fetchone()
        if not row or not self.is_admin(row[0], row[1]):
            return None
        session = secrets.token_urlsafe(32)
        self._insert("sessions", token=session, cafe_id=row[0], user_id=row[1], expires=int(clock.time()) + SESSION_TTL)
        return session

    def session(self, token: str) -> tuple[int, int] | None:
        row = self.db.execute(
            "select cafe_id, user_id from sessions where token = ? and expires > ?",
            (token, int(clock.time())),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def drop_session(self, token: str) -> None:
        with self.db:
            self.db.execute("delete from sessions where token = ?", (token,))

    # --- menu ---
    def groups(self, cafe_id: int) -> list[ModifierGroup]:
        groups = {
            r["id"]: ModifierGroup(r["id"], r["name"], bool(r["multiple"]))
            for r in self.db.execute("select * from modifier_groups where cafe_id = ? order by id", (cafe_id,))
        }
        for r in self.db.execute(
            "select o.* from modifier_options o join modifier_groups g on g.id = o.group_id"
            " where g.cafe_id = ? order by o.id",
            (cafe_id,),
        ):
            groups[r["group_id"]].options.append(Option(r["id"], r["group_id"], r["name"], r["price"]))
        return list(groups.values())

    def _items(self, cafe_id: int, where: str = "", args: tuple = ()) -> list[Item]:
        groups = {g.id: g for g in self.groups(cafe_id)}
        links: dict[int, list[ModifierGroup]] = {}
        for r in self.db.execute(
            "select m.item_id, m.group_id from item_modifiers m join items i on i.id = m.item_id"
            " where i.cafe_id = ? order by m.group_id",
            (cafe_id,),
        ):
            links.setdefault(r[0], []).append(groups[r[1]])
        rows = self.db.execute(
            f"select * from items where cafe_id = ? {where} order by position, id",
            (cafe_id, *args),
        )
        return [
            Item(
                r["id"],
                r["category_id"],
                r["name"],
                r["description"],
                r["price"],
                r["photo"],
                r["photo_file_id"],
                bool(r["available"]),
                links.get(r["id"], []),
            )
            for r in rows
        ]

    def menu(self, cafe_id: int, only_available: bool = True) -> list[Category]:
        """Categories with their items. Customers see only available items and non-empty categories."""
        cats = {
            r["id"]: Category(r["id"], r["name"])
            for r in self.db.execute("select * from categories where cafe_id = ? order by position, id", (cafe_id,))
        }
        for it in self._items(cafe_id, "and available = 1" if only_available else ""):
            cats[it.category_id].items.append(it)
        return [c for c in cats.values() if c.items or not only_available]

    def category(self, cafe_id: int, category_id: int) -> Category | None:
        return next((c for c in self.menu(cafe_id) if c.id == category_id), None)

    def item(self, cafe_id: int, item_id: int, only_available: bool = True) -> Item | None:
        where = "and id = ?" + (" and available = 1" if only_available else "")
        return next(iter(self._items(cafe_id, where, (item_id,))), None)

    def _next_position(self, table: str, scope: str, value: int) -> int:
        row = self.db.execute(f"select coalesce(max(position), -1) + 1 from {table} where {scope} = ?", (value,))
        return row.fetchone()[0]

    def _move(self, table: str, scope: str, value: int, row_id: int, step: int) -> None:
        ids = [
            r[0] for r in self.db.execute(f"select id from {table} where {scope} = ? order by position, id", (value,))
        ]
        i = ids.index(row_id)
        if 0 <= i + step < len(ids):
            ids[i], ids[i + step] = ids[i + step], ids[i]
        with self.db:
            self.db.executemany(f"update {table} set position = ? where id = ?", list(enumerate(ids)))

    def add_category(self, cafe_id: int, name: str) -> int:
        pos = self._next_position("categories", "cafe_id", cafe_id)
        return self._insert("categories", cafe_id=cafe_id, name=name, position=pos)

    def rename_category(self, cafe_id: int, category_id: int, name: str) -> None:
        with self.db:
            self.db.execute("update categories set name = ? where id = ? and cafe_id = ?", (name, category_id, cafe_id))

    def move_category(self, cafe_id: int, category_id: int, step: int) -> None:
        if self._owned("categories", cafe_id, category_id):
            self._move("categories", "cafe_id", cafe_id, category_id, step)

    def delete_category(self, cafe_id: int, category_id: int) -> bool:
        """Only empty categories can be deleted, so a misclick never wipes out items."""
        if self.db.execute("select 1 from items where category_id = ?", (category_id,)).fetchone():
            return False
        with self.db:
            self.db.execute("delete from categories where id = ? and cafe_id = ?", (category_id, cafe_id))
        return True

    def _owned(self, table: str, cafe_id: int, row_id: int) -> bool:
        row = self.db.execute(f"select 1 from {table} where id = ? and cafe_id = ?", (row_id, cafe_id))
        return row.fetchone() is not None

    def add_item(
        self,
        cafe_id: int,
        category_id: int,
        name: str,
        price: int,
        description: str = "",
        group_ids: list[int] = (),
    ) -> int | None:
        if not self._owned("categories", cafe_id, category_id):
            return None
        item_id = self._insert(
            "items",
            cafe_id=cafe_id,
            category_id=category_id,
            name=name,
            price=price,
            description=description,
            position=self._next_position("items", "category_id", category_id),
        )
        with self.db:
            self._link_groups(cafe_id, item_id, group_ids)
        return item_id

    def _link_groups(self, cafe_id: int, item_id: int, group_ids: list[int]) -> None:
        own = {g.id for g in self.groups(cafe_id)}
        self.db.execute("delete from item_modifiers where item_id = ?", (item_id,))
        self.db.executemany(
            "insert into item_modifiers values (?, ?)",
            [(item_id, g) for g in sorted(set(group_ids) & own)],
        )

    def update_item(
        self,
        cafe_id: int,
        item_id: int,
        *,
        name: str,
        description: str,
        price: int,
        category_id: int,
        group_ids: list[int],
    ) -> bool:
        if not self._owned("items", cafe_id, item_id) or not self._owned("categories", cafe_id, category_id):
            return False
        with self.db:
            self.db.execute(
                "update items set name = ?, description = ?, price = ?, category_id = ? where id = ?",
                (name, description, price, category_id, item_id),
            )
            self._link_groups(cafe_id, item_id, group_ids)
        return True

    def move_item(self, cafe_id: int, item_id: int, step: int) -> None:
        if it := self.item(cafe_id, item_id, only_available=False):
            self._move("items", "category_id", it.category_id, item_id, step)

    def set_available(self, cafe_id: int, item_id: int, available: bool) -> None:
        with self.db:
            self.db.execute(
                "update items set available = ? where id = ? and cafe_id = ?",
                (int(available), item_id, cafe_id),
            )

    def set_photo(self, cafe_id: int, item_id: int, photo: str | None) -> str | None:
        """Returns the previous photo file name so the caller can delete it."""
        it = self.item(cafe_id, item_id, only_available=False)
        if not it:
            return None
        with self.db:
            self.db.execute("update items set photo = ?, photo_file_id = null where id = ?", (photo, item_id))
        return it.photo

    def set_photo_file_id(self, item_id: int, file_id: str) -> None:
        """Telegram's id of an uploaded photo, so the file is uploaded once per bot."""
        with self.db:
            self.db.execute("update items set photo_file_id = ? where id = ?", (file_id, item_id))

    def delete_item(self, cafe_id: int, item_id: int) -> str | None:
        """Returns the photo file name so the caller can delete it."""
        it = self.item(cafe_id, item_id, only_available=False)
        with self.db:
            self.db.execute("delete from items where id = ? and cafe_id = ?", (item_id, cafe_id))
        return it.photo if it else None

    # --- modifiers ---
    def add_group(self, cafe_id: int, name: str, multiple: bool) -> int:
        return self._insert("modifier_groups", cafe_id=cafe_id, name=name, multiple=int(multiple))

    def update_group(self, cafe_id: int, group_id: int, name: str, multiple: bool) -> None:
        with self.db:
            self.db.execute(
                "update modifier_groups set name = ?, multiple = ? where id = ? and cafe_id = ?",
                (name, int(multiple), group_id, cafe_id),
            )

    def delete_group(self, cafe_id: int, group_id: int) -> None:
        with self.db:
            self.db.execute("delete from modifier_groups where id = ? and cafe_id = ?", (group_id, cafe_id))

    def add_option(self, cafe_id: int, group_id: int, name: str, price: int) -> int | None:
        if not self._owned("modifier_groups", cafe_id, group_id):
            return None
        return self._insert("modifier_options", group_id=group_id, name=name, price=price)

    _OWN_OPTION = "id = ? and group_id in (select id from modifier_groups where cafe_id = ?)"

    def update_option(self, cafe_id: int, option_id: int, name: str, price: int) -> None:
        with self.db:
            self.db.execute(
                f"update modifier_options set name = ?, price = ? where {self._OWN_OPTION}",
                (name, price, option_id, cafe_id),
            )

    def delete_option(self, cafe_id: int, option_id: int) -> None:
        with self.db:
            self.db.execute(f"delete from modifier_options where {self._OWN_OPTION}", (option_id, cafe_id))

    # --- cart ---
    @staticmethod
    def pick(item: Item, option_ids: list[int]) -> list[Option] | None:
        """Options in menu order if the choice is valid: known options, exactly one in every single-choice group."""
        chosen = set(option_ids)
        picked = []
        for g in item.groups:
            in_group = [o for o in g.options if o.id in chosen]
            if not g.multiple and g.options and len(in_group) != 1:
                return None
            picked += in_group
        return picked if len(picked) == len(chosen) else None

    def add_to_cart(self, cafe_id: int, user_id: int, item_id: int, option_ids: list[int]) -> CartLine | None:
        it = self.item(cafe_id, item_id)
        options = self.pick(it, option_ids) if it else None
        if options is None:
            return None
        key = ".".join(str(o.id) for o in sorted(options, key=lambda o: o.id))
        with self.db:
            self.db.execute(
                "insert into carts (cafe_id, user_id, item_id, options, qty) values (?, ?, ?, ?, 1)"
                " on conflict (cafe_id, user_id, item_id, options) do update set qty = qty + 1",
                (cafe_id, user_id, item_id, key),
            )
        return next(ln for ln in self.cart(cafe_id, user_id) if ln.item.id == item_id and ln.options == options)

    def change(self, cafe_id: int, user_id: int, line_id: int, delta: int) -> None:
        with self.db:
            self.db.execute(
                "update carts set qty = qty + ? where id = ? and cafe_id = ? and user_id = ?",
                (delta, line_id, cafe_id, user_id),
            )
            self.db.execute("delete from carts where qty <= 0")

    def cart(self, cafe_id: int, user_id: int) -> list[CartLine]:
        items = {it.id: it for c in self.menu(cafe_id) for it in c.items}
        lines = []
        for r in self.db.execute(
            "select * from carts where cafe_id = ? and user_id = ? order by id",
            (cafe_id, user_id),
        ):
            # lines whose item went to the stop-list or whose options changed silently drop out
            it = items.get(r["item_id"])
            options = self.pick(it, [int(x) for x in r["options"].split(".") if x]) if it else None
            if options is not None:
                lines.append(CartLine(r["id"], it, options, r["qty"]))
        return lines

    def clear(self, cafe_id: int, user_id: int) -> None:
        with self.db:
            self.db.execute("delete from carts where cafe_id = ? and user_id = ?", (cafe_id, user_id))

    # --- orders ---
    def place(
        self,
        cafe: Cafe,
        user_id: int,
        customer: str,
        phone: str,
        method: str,
        address: str | None,
        comment: str | None,
    ) -> Order | None:
        lines = self.cart(cafe.id, user_id)
        if not lines:
            return None
        snapshot = [
            {"name": ln.item.name, "options": [o.name for o in ln.options], "qty": ln.qty, "unit": ln.unit}
            for ln in lines
        ]
        subtotal = sum(ln.sum for ln in lines)
        fee = cafe.delivery_fee if method == "delivery" else 0
        with self.db:
            number = self.db.execute(
                "select coalesce(max(number), 0) + 1 from orders where cafe_id = ?", (cafe.id,)
            ).fetchone()[0]
            cur = self.db.execute(
                "insert into orders (cafe_id, number, user_id, customer, phone, method, address, comment, lines,"
                " subtotal, delivery_fee, total, created_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cafe.id,
                    number,
                    user_id,
                    customer,
                    phone,
                    method,
                    address,
                    comment,
                    json.dumps(snapshot, ensure_ascii=False),
                    subtotal,
                    fee,
                    subtotal + fee,
                    datetime.now(UTC).isoformat(),
                ),
            )
            self.db.execute("delete from carts where cafe_id = ? and user_id = ?", (cafe.id, user_id))
        return self.order(cafe.id, cur.lastrowid)

    def _order(self, row: sqlite3.Row | None) -> Order | None:
        if not row:
            return None
        d = dict(row)
        d.update(lines=json.loads(d["lines"]), created_at=datetime.fromisoformat(d["created_at"]))
        return Order(**d)

    def order(self, cafe_id: int, order_id: int) -> Order | None:
        row = self.db.execute("select * from orders where id = ? and cafe_id = ?", (order_id, cafe_id))
        return self._order(row.fetchone())

    def orders(self, cafe_id: int, active_only: bool, limit: int = 100) -> list[Order]:
        where = f"and status in ({','.join('?' * len(NEXT))})" if active_only else ""
        rows = self.db.execute(
            f"select * from orders where cafe_id = ? {where} order by id desc limit ?",
            (cafe_id, *(NEXT if active_only else ()), limit),
        )
        return [self._order(r) for r in rows]

    def set_status(self, cafe_id: int, order_id: int, status: str) -> None:
        with self.db:
            self.db.execute("update orders set status = ? where id = ? and cafe_id = ?", (status, order_id, cafe_id))
            if status == "done":  # handed over: cash on pickup/delivery is collected at this point
                self.db.execute(
                    "update orders set payment_status = 'paid' where id = ? and payment_method = 'cash'",
                    (order_id,),
                )

    def add_staff_message(self, order_id: int, chat_id: int, message_id: int) -> None:
        with self.db:
            self.db.execute("insert or replace into staff_messages values (?, ?, ?)", (order_id, chat_id, message_id))

    def staff_messages(self, order_id: int) -> list[tuple[int, int]]:
        rows = self.db.execute("select chat_id, message_id from staff_messages where order_id = ?", (order_id,))
        return [(r[0], r[1]) for r in rows]

    def stats(self, cafe_id: int, since: datetime) -> tuple[int, int]:
        n, total = self.db.execute(
            "select count(*), coalesce(sum(total), 0) from orders"
            " where cafe_id = ? and status != 'cancelled' and created_at >= ?",
            (cafe_id, since.astimezone(UTC).isoformat()),
        ).fetchone()
        return n, total
