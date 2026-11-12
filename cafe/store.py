"""SQLite storage for all cafes. Every query that takes an id from outside is scoped by cafe_id."""

import json
import secrets
import sqlite3
import time as clock
from datetime import UTC, date, datetime, time
from pathlib import Path

from .billing import Payment, extend
from .delivery import fee as delivery_fee
from .models import ACTIVE, HANDED_OVER, ROLES, Cafe, CartLine, Category, Customer, Item, ModifierGroup, Option, Order

# The current shape of the database: a new database is created from it and marked as up to date.
SCHEMA = """
create table cafes (
  id integer primary key, slug text not null unique, bot_token text not null unique, bot_username text,
  name text not null, greeting text not null default '', currency text not null default 'сум',
  timezone text not null default 'Asia/Tashkent', open_time text, close_time text,
  pickup_address text not null default '', delivery_enabled integer not null default 0,
  delivery_min integer not null default 0, delivery_fee integer not null default 0,
  payment_note text not null default 'Оплата наличными или картой при получении',
  staff_chat_id integer, invite_code text, brand_color text not null default '#8b5e3c', logo text, cover text,
  card_number text not null default '', card_holder text not null default '', paid_until text,
  enabled integer not null default 1, app_theme text not null default 'auto', lat real, lon real,
  delivery_zones text not null default '[]', courier_chat_id integer, finance_chat_id integer
);
create table admins (
  cafe_id integer not null references cafes on delete cascade, user_id integer not null,
  name text not null default '', primary key (cafe_id, user_id)
);
create table categories (
  id integer primary key, cafe_id integer not null references cafes on delete cascade,
  name text not null, position integer not null default 0
);
create table items (
  id integer primary key, cafe_id integer not null references cafes on delete cascade,
  category_id integer not null references categories on delete cascade, name text not null,
  description text not null default '', price integer not null, photo text,
  available integer not null default 1, position integer not null default 0
);
create table modifier_groups (
  id integer primary key, cafe_id integer not null references cafes on delete cascade,
  name text not null, multiple integer not null default 0
);
create table modifier_options (
  id integer primary key, group_id integer not null references modifier_groups on delete cascade,
  name text not null, price integer not null default 0
);
create table item_modifiers (
  item_id integer not null references items on delete cascade,
  group_id integer not null references modifier_groups on delete cascade, primary key (item_id, group_id)
);
create table orders (
  id integer primary key, cafe_id integer not null references cafes on delete cascade, number integer not null,
  user_id integer not null, customer text not null, phone text not null, method text not null, address text,
  comment text, lines text not null, subtotal integer not null, delivery_fee integer not null,
  total integer not null, status text not null default 'new',
  payment_method text not null default 'cash', payment_status text not null default 'pending',
  created_at text not null, lat real, lon real, customer_message_id integer, rating integer,
  unique (cafe_id, number)
);
create table staff_messages (
  order_id integer not null references orders on delete cascade, chat_id integer not null,
  message_id integer not null, primary key (order_id, chat_id)
);
create table login_links (token text primary key, cafe_id integer not null,
  user_id integer not null, expires integer not null);
create table sessions (token text primary key, cafe_id integer not null,
  user_id integer not null, expires integer not null);
create table customers (
  cafe_id integer not null references cafes on delete cascade, user_id integer not null, phone text,
  address text, lang text not null default 'ru', lat real, lon real, muted integer not null default 0,
  primary key (cafe_id, user_id)
);
create table broadcasts (
  id integer primary key, cafe_id integer not null references cafes on delete cascade, text text not null,
  audience integer not null, sent integer not null default 0, failed integer not null default 0,
  status text not null default 'sending', created_at text not null
);
create table payments (
  id integer primary key, cafe_id integer not null references cafes on delete cascade, amount integer not null,
  months integer not null, until text not null, prev_until text, note text not null default '',
  created_at text not null
);
create table reminders (cafe_id integer not null, kind text not null, key text not null,
  primary key (cafe_id, kind, key));
create table operators (chat_id integer primary key, name text not null default '');
create table meta (key text primary key, value text not null);
"""

# Steps that bring an existing database to SCHEMA; pragma user_version counts the applied ones.
# Append only: never edit a step that may have run somewhere. tests/test_store.py checks the result equals SCHEMA.
MIGRATIONS = [
    # 1: statuses by how the order is received, Mini App (cart moves to the client), branding, card transfers,
    # subscription, customer profiles
    """
    update orders set status = 'cooking' where status = 'accepted';
    update orders set status = 'courier' where method = 'delivery' and status = 'ready';
    update orders set status = 'delivered' where method = 'delivery' and status = 'done';
    drop table carts;
    alter table items drop column photo_file_id;
    alter table cafes add column brand_color text not null default '#8b5e3c';
    alter table cafes add column logo text;
    alter table cafes add column cover text;
    alter table cafes add column card_number text not null default '';
    alter table cafes add column card_holder text not null default '';
    alter table cafes add column paid_until text;
    alter table cafes add column enabled integer not null default 1;
    create table customers (
      cafe_id integer not null references cafes on delete cascade, user_id integer not null, phone text,
      address text, lang text not null default 'ru', primary key (cafe_id, user_id)
    );
    """,
    # 2: the Mini App can keep the cafe's own light or dark look instead of following the guest's Telegram theme
    """
    alter table cafes add column app_theme text not null default 'auto';
    """,
    # 3: subscription payments, sent reminders, the operator's service bot (its chats and settings)
    """
    create table payments (
      id integer primary key, cafe_id integer not null references cafes on delete cascade, amount integer not null,
      months integer not null, until text not null, prev_until text, note text not null default '',
      created_at text not null
    );
    create table reminders (cafe_id integer not null, kind text not null, key text not null,
      primary key (cafe_id, kind, key));
    create table operators (chat_id integer primary key, name text not null default '');
    create table meta (key text primary key, value text not null);
    """,
    # 4: delivery by the map: the cafe's point and price zones, the guest's pin on orders and in the profile
    """
    alter table cafes add column lat real;
    alter table cafes add column lon real;
    alter table cafes add column delivery_zones text not null default '[]';
    alter table orders add column lat real;
    alter table orders add column lon real;
    alter table customers add column lat real;
    alter table customers add column lon real;
    """,
    # 5: groups for couriers and for money next to the kitchen's; the guest's order message is kept up to date
    """
    alter table cafes add column courier_chat_id integer;
    alter table cafes add column finance_chat_id integer;
    alter table orders add column customer_message_id integer;
    """,
    # 6: news to the cafe's guests (with opting out), a 1–5 rating of each order
    """
    alter table customers add column muted integer not null default 0;
    alter table orders add column rating integer;
    create table broadcasts (
      id integer primary key, cafe_id integer not null references cafes on delete cascade, text text not null,
      audience integer not null, sent integer not null default 0, failed integer not null default 0,
      status text not null default 'sending', created_at text not null
    );
    """,
]

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
    "brand_color",
    "app_theme",
    "card_number",
    "card_holder",
    "lat",
    "lon",
    "delivery_zones",
}
CAFE_IMAGES = {"logo", "cover"}
# The operator (who runs this installation) logs in to /ops with a link from the terminal; their sessions and login
# links use this id. Telegram user ids start at 1, so it never meets a real admin. OPS is the cafe id of /ops sessions.
OPERATOR = 0
OPS = 0
LOGIN_TTL = 15 * 60
MAX_QTY = 99
SESSION_TTL = 30 * 24 * 3600


def _time(value: str | None) -> time | None:
    return time.fromisoformat(value) if value else None


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode = wal")  # readers (a backup) do not block the bots
        self.db.execute("pragma foreign_keys = on")
        self._migrate()

    def _migrate(self) -> None:
        if not self.db.execute("select 1 from sqlite_master where name = 'cafes'").fetchone():
            self.db.executescript(f"begin; {SCHEMA} pragma user_version = {len(MIGRATIONS)}; commit;")
            return
        version = self.db.execute("pragma user_version").fetchone()[0]
        for n, sql in enumerate(MIGRATIONS[version:], version + 1):
            self.db.executescript(f"begin; {sql} pragma user_version = {n}; commit;")

    def backup_to(self, path: Path) -> None:
        """A consistent copy of the whole database, safe while the bots are writing."""
        target = sqlite3.connect(path)
        try:
            self.db.backup(target)
        finally:
            target.close()

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
        values = {k: v.strftime("%H:%M") if isinstance(v, time) else v for k, v in fields.items()}
        if "delivery_zones" in values:
            zones = sorted((float(k), int(p)) for k, p in values["delivery_zones"])  # nearest first: delivery.fee
            values["delivery_zones"] = json.dumps(zones)
        return values

    def _cafe(self, row: sqlite3.Row | None) -> Cafe | None:
        if not row:
            return None
        d = dict(row)
        d.update(
            open_time=_time(d["open_time"]),
            close_time=_time(d["close_time"]),
            delivery_enabled=bool(d["delivery_enabled"]),
            paid_until=date.fromisoformat(d["paid_until"]) if d["paid_until"] else None,
            enabled=bool(d["enabled"]),
            delivery_zones=[(float(k), int(p)) for k, p in json.loads(d["delivery_zones"])],
        )
        return Cafe(**d)

    def cafe(self, cafe_id: int) -> Cafe | None:
        return self._cafe(self.db.execute("select * from cafes where id = ?", (cafe_id,)).fetchone())

    def cafe_by_token(self, token: str) -> Cafe | None:
        return self._cafe(self.db.execute("select * from cafes where bot_token = ?", (token,)).fetchone())

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

    def free_slug(self, base: str) -> str:
        """base, or base-2, base-3… — the first name no cafe has."""
        slug, n = base, 1
        while self.cafe_by_slug(slug):
            n += 1
            slug = f"{base}-{n}"
        return slug

    def delete_cafe(self, cafe_id: int) -> list[str]:
        """Everything of the cafe goes; returns its picture files for the caller to delete. Its bot token is free again."""
        photos = [
            r[0] for r in self.db.execute("select photo from items where cafe_id = ? and photo is not null", (cafe_id,))
        ]
        cafe = self.cafe(cafe_id)
        with self.db:
            self.db.execute("delete from login_links where cafe_id = ?", (cafe_id,))
            self.db.execute("delete from sessions where cafe_id = ?", (cafe_id,))
            self.db.execute("delete from reminders where cafe_id = ?", (cafe_id,))
            self.db.execute("delete from cafes where id = ?", (cafe_id,))  # the rest cascades
        return photos + [p for p in (cafe.logo, cafe.cover) if p]

    def set_staff_chat(self, cafe_id: int, chat_id: int | None, role: str = "staff") -> None:
        """The group for a role (models.ROLES): staff — the kitchen, courier, finance. One group, one role."""
        column = ROLES[role][0]
        with self.db:
            if chat_id is not None:  # a group moved to another role leaves the old one
                for other, _ in ROLES.values():
                    self.db.execute(f"update cafes set {other} = null where id = ? and {other} = ?", (cafe_id, chat_id))
            self.db.execute(f"update cafes set {column} = ? where id = ?", (chat_id, cafe_id))

    def replace_chat(self, cafe_id: int, old: int, new: int) -> None:
        """A group became a supergroup and got a new id: whatever role it had stays."""
        with self.db:
            for column, _ in ROLES.values():
                self.db.execute(f"update cafes set {column} = ? where id = ? and {column} = ?", (new, cafe_id, old))

    def set_cafe_image(self, cafe_id: int, kind: str, name: str | None) -> str | None:
        """Logo or cover; returns the previous file name so the caller can delete it."""
        if kind not in CAFE_IMAGES:
            raise ValueError(kind)
        old = self.db.execute(f"select {kind} from cafes where id = ?", (cafe_id,)).fetchone()[0]
        with self.db:
            self.db.execute(f"update cafes set {kind} = ? where id = ?", (name, cafe_id))
        return old

    # --- subscription (operator: /ops or the CLI) ---
    def set_paid_until(self, cafe_id: int, until: date | None) -> None:
        with self.db:
            self.db.execute(
                "update cafes set paid_until = ? where id = ?", (until.isoformat() if until else None, cafe_id)
            )

    def set_enabled(self, cafe_id: int, enabled: bool) -> None:
        with self.db:
            self.db.execute("update cafes set enabled = ? where id = ?", (int(enabled), cafe_id))

    def _payment(self, row: sqlite3.Row) -> Payment:
        d = dict(row)
        d.update(
            until=date.fromisoformat(d["until"]),
            prev_until=date.fromisoformat(d["prev_until"]) if d["prev_until"] else None,
            created_at=datetime.fromisoformat(d["created_at"]),
        )
        return Payment(**d)

    def add_payment(self, cafe_id: int, amount: int, months: int, today: date, note: str = "") -> Payment:
        """The owner paid for `months`: paid_until moves on (billing.extend) and the payment is kept."""
        prev = self.cafe(cafe_id).paid_until
        until = extend(prev, today, months)
        with self.db:
            payment_id = self.db.execute(
                "insert into payments (cafe_id, amount, months, until, prev_until, note, created_at)"
                " values (?, ?, ?, ?, ?, ?, ?)",
                (
                    cafe_id,
                    amount,
                    months,
                    until.isoformat(),
                    prev.isoformat() if prev else None,
                    note,
                    datetime.now(UTC).isoformat(),
                ),
            ).lastrowid
            self.db.execute("update cafes set paid_until = ? where id = ?", (until.isoformat(), cafe_id))
        return self._payment(self.db.execute("select * from payments where id = ?", (payment_id,)).fetchone())

    def payments(self, cafe_id: int) -> list[Payment]:
        rows = self.db.execute("select * from payments where cafe_id = ? order by id desc", (cafe_id,))
        return [self._payment(r) for r in rows]

    def cancel_payment(self, cafe_id: int, payment_id: int) -> bool:
        """A mistaken payment: only the latest one, and paid_until goes back to what it was before it."""
        last = self.payments(cafe_id)[:1]
        if not last or last[0].id != payment_id:
            return False
        prev = last[0].prev_until
        with self.db:
            self.db.execute("delete from payments where id = ?", (payment_id,))
            self.db.execute(
                "update cafes set paid_until = ? where id = ?", (prev.isoformat() if prev else None, cafe_id)
            )
        return True

    def income(self, since: datetime) -> int:
        """All cafes' payments to the operator since then."""
        row = self.db.execute(
            "select sum(amount) from payments where created_at >= ?", (since.astimezone(UTC).isoformat(),)
        )
        return row.fetchone()[0] or 0

    # --- reminders: each one goes out once (cafe 0 — the operator's daily digest) ---
    def reminded(self, cafe_id: int, kind: str, key: str) -> bool:
        q = "select 1 from reminders where cafe_id = ? and kind = ? and key = ?"
        return self.db.execute(q, (cafe_id, kind, key)).fetchone() is not None

    def mark_reminded(self, cafe_id: int, kind: str, key: str) -> None:
        with self.db:
            self.db.execute("insert or ignore into reminders values (?, ?, ?)", (cafe_id, kind, key))

    # --- the operator's service bot ---
    def meta(self, key: str) -> str | None:
        row = self.db.execute("select value from meta where key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self.db:
            if value is None:
                self.db.execute("delete from meta where key = ?", (key,))
            else:
                self.db.execute("insert or replace into meta values (?, ?)", (key, value))

    def operators(self) -> list[tuple[int, str]]:
        return [tuple(r) for r in self.db.execute("select chat_id, name from operators order by chat_id")]

    def add_operator(self, code: str, chat_id: int, name: str) -> bool:
        """The link from /ops (t.me/<service bot>?start=op-<code>) adds whoever opens it; it works once."""
        if not code or code != self.meta("ops_invite"):
            return False
        with self.db:
            self.db.execute("insert or replace into operators values (?, ?)", (chat_id, name))
            self.db.execute("delete from meta where key = 'ops_invite'")
        return True

    def remove_operator(self, chat_id: int) -> None:
        with self.db:
            self.db.execute("delete from operators where chat_id = ?", (chat_id,))

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

    def peek_login(self, token: str) -> tuple[int, int] | None:
        """Who a link is for, without using it up: link previews (Telegram, WhatsApp) open links before people do."""
        row = self.db.execute(
            "select cafe_id, user_id from login_links where token = ? and expires > ?", (token, int(clock.time()))
        ).fetchone()
        return (row[0], row[1]) if row else None

    def consume_login(self, token: str) -> tuple[int, int] | None:
        """A one-time link → (cafe id, user id) it was made for; the link stops working."""
        with self.db:
            row = self.db.execute(
                "delete from login_links where token = ? and expires > ? returning cafe_id, user_id",
                (token, int(clock.time())),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def create_session(self, cafe_id: int, user_id: int) -> str:
        session = secrets.token_urlsafe(32)
        self._insert(
            "sessions", token=session, cafe_id=cafe_id, user_id=user_id, expires=int(clock.time()) + SESSION_TTL
        )
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
        if self.db.execute(
            "select 1 from items where category_id = ? and cafe_id = ?", (category_id, cafe_id)
        ).fetchone():
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
            self.db.execute("update items set photo = ? where id = ?", (photo, item_id))
        return it.photo

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

    # --- choosing ---
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

    def lines(self, cafe_id: int, wanted: list[tuple[int, list[int], int]]) -> list[CartLine | None]:
        """(item, options, qty) picks priced by the current menu; None where the item is gone, stopped or the choice
        is no longer valid. The cart itself lives in the Mini App, so everything it sends is checked here."""
        items = {it.id: it for c in self.menu(cafe_id) for it in c.items}
        result = []
        for item_id, option_ids, qty in wanted:
            it = items.get(item_id)
            options = self.pick(it, option_ids) if it and 0 < qty <= MAX_QTY else None
            result.append(CartLine(it, options, qty) if options is not None else None)
        return result

    # --- customers ---
    def customer(self, cafe_id: int, user_id: int) -> Customer | None:
        row = self.db.execute(
            "select phone, address, lang, lat, lon from customers where cafe_id = ? and user_id = ?",
            (cafe_id, user_id),
        ).fetchone()
        return Customer(*row) if row else None

    def set_lang(self, cafe_id: int, user_id: int, lang: str) -> None:
        with self.db:
            self.db.execute(
                "insert into customers (cafe_id, user_id, lang) values (?, ?, ?)"
                " on conflict (cafe_id, user_id) do update set lang = excluded.lang",
                (cafe_id, user_id, lang),
            )

    def touch_customer(self, cafe_id: int, user_id: int, lang: str) -> None:
        """A guest who opened the bot: known for news from now on, their saved language kept."""
        with self.db:
            self.db.execute(
                "insert into customers (cafe_id, user_id, lang) values (?, ?, ?) on conflict (cafe_id, user_id) do nothing",
                (cafe_id, user_id, lang),
            )

    def set_muted(self, cafe_id: int, user_id: int, muted: bool) -> None:
        with self.db:
            self.db.execute(
                "insert into customers (cafe_id, user_id, muted) values (?, ?, ?)"
                " on conflict (cafe_id, user_id) do update set muted = excluded.muted",
                (cafe_id, user_id, int(muted)),
            )

    # --- news to guests ---
    def audience(self, cafe_id: int) -> list[int]:
        """Guests who opened this cafe's bot and did not opt out of news."""
        rows = self.db.execute(
            "select user_id from customers where cafe_id = ? and muted = 0 order by user_id", (cafe_id,)
        )
        return [r[0] for r in rows]

    def add_broadcast(self, cafe_id: int, text: str, audience: int) -> int:
        return self._insert(
            "broadcasts", cafe_id=cafe_id, text=text, audience=audience, created_at=datetime.now(UTC).isoformat()
        )

    def broadcast_progress(self, cafe_id: int, broadcast_id: int, sent: int, failed: int, status: str) -> None:
        with self.db:
            self.db.execute(
                "update broadcasts set sent = ?, failed = ?, status = ? where id = ? and cafe_id = ?",
                (sent, failed, status, broadcast_id, cafe_id),
            )

    def broadcasts(self, cafe_id: int, limit: int = 20) -> list[dict]:
        rows = self.db.execute("select * from broadcasts where cafe_id = ? order by id desc limit ?", (cafe_id, limit))
        return [dict(r) | {"created_at": datetime.fromisoformat(r["created_at"])} for r in rows]

    def broadcasts_since(self, cafe_id: int, since: datetime) -> int:
        q = "select count(*) from broadcasts where cafe_id = ? and created_at >= ?"
        return self.db.execute(q, (cafe_id, since.astimezone(UTC).isoformat())).fetchone()[0]

    # --- orders ---
    def place(
        self,
        cafe: Cafe,
        user_id: int,
        customer: str,
        lines: list[CartLine],
        *,
        phone: str,
        method: str,
        address: str | None,
        comment: str | None,
        payment_method: str,
        lang: str,
        point: tuple[float, float] | None = None,
    ) -> Order:
        """Saves the order and remembers the phone, the delivery address and pin, and the language for next time.
        The delivery price comes from delivery.fee — the caller has checked that there is one for this point."""
        snapshot = [
            {
                "item_id": ln.item.id,
                "option_ids": [o.id for o in ln.options],
                "name": ln.item.name,
                "options": [o.name for o in ln.options],
                "qty": ln.qty,
                "unit": ln.unit,
            }
            for ln in lines
        ]
        subtotal = sum(ln.sum for ln in lines)
        delivery = method == "delivery"
        fee = delivery_fee(cafe, point) if delivery else 0
        if fee is None:
            raise ValueError("no delivery to this point")
        lat, lon = point if delivery and point else (None, None)
        with self.db:
            number = self.db.execute(
                "select coalesce(max(number), 0) + 1 from orders where cafe_id = ?", (cafe.id,)
            ).fetchone()[0]
            cur = self.db.execute(
                "insert into orders (cafe_id, number, user_id, customer, phone, method, address, comment, lines,"
                " subtotal, delivery_fee, total, payment_method, created_at, lat, lon)"
                " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cafe.id,
                    number,
                    user_id,
                    customer,
                    phone,
                    method,
                    address if delivery else None,
                    comment,
                    json.dumps(snapshot, ensure_ascii=False),
                    subtotal,
                    fee,
                    subtotal + fee,
                    payment_method,
                    datetime.now(UTC).isoformat(),
                    lat,
                    lon,
                ),
            )
            self.db.execute(
                "insert into customers (cafe_id, user_id, phone, address, lang, lat, lon) values (?, ?, ?, ?, ?, ?, ?)"
                " on conflict (cafe_id, user_id) do update set phone = excluded.phone,"
                " address = coalesce(excluded.address, address), lang = excluded.lang,"
                " lat = coalesce(excluded.lat, lat), lon = coalesce(excluded.lon, lon)",
                (cafe.id, user_id, phone, address if delivery else None, lang, lat, lon),
            )
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
        where = f"and status in ({','.join('?' * len(ACTIVE))})" if active_only else ""
        rows = self.db.execute(
            f"select * from orders where cafe_id = ? {where} order by id desc limit ?",
            (cafe_id, *(ACTIVE if active_only else ()), limit),
        )
        return [self._order(r) for r in rows]

    def orders_between(self, cafe_id: int, since: datetime, until: datetime) -> list[Order]:
        """Every order placed in [since, until), cancelled ones too, oldest first: for reports."""
        rows = self.db.execute(
            "select * from orders where cafe_id = ? and created_at >= ? and created_at < ? order by id",
            (cafe_id, since.astimezone(UTC).isoformat(), until.astimezone(UTC).isoformat()),
        )
        return [self._order(r) for r in rows]

    def customer_orders(self, cafe_id: int, user_id: int, limit: int = 5) -> list[Order]:
        rows = self.db.execute(
            "select * from orders where cafe_id = ? and user_id = ? order by id desc limit ?", (cafe_id, user_id, limit)
        )
        return [self._order(r) for r in rows]

    def repeat(self, cafe_id: int, user_id: int, order_id: int) -> tuple[list[CartLine], list[str]] | None:
        """A customer's past order as lines of the current menu, plus the names of what cannot be ordered now."""
        o = self.order(cafe_id, order_id)
        if not o or o.user_id != user_id:
            return None
        wanted = [ln for ln in o.lines if "item_id" in ln]
        found = self.lines(cafe_id, [(ln["item_id"], ln["option_ids"], ln["qty"]) for ln in wanted])
        missing = [ln["name"] for ln in o.lines if "item_id" not in ln]
        missing += [ln["name"] for ln, line in zip(wanted, found) if line is None]
        return [line for line in found if line], missing

    def set_status(self, cafe_id: int, order_id: int, status: str, was: str | None = None) -> bool:
        """With `was`: only if the order is still in that status (one UPDATE — a guest's cancel or another cook's tap
        a moment earlier wins, nothing is overwritten). False if nothing changed."""
        with self.db:
            cur = self.db.execute(
                "update orders set status = ? where id = ? and cafe_id = ? and (? is null or status = ?)",
                (status, order_id, cafe_id, was, was),
            )
            if cur.rowcount != 1:
                return False
            if status in HANDED_OVER:  # cash is collected when the order is handed over
                self.db.execute(
                    "update orders set payment_status = 'paid' where id = ? and payment_method = 'cash'",
                    (order_id,),
                )
        return True

    def cancel_by_customer(self, cafe_id: int, user_id: int, order_id: int) -> bool:
        """Only until cooking starts (models.CUSTOMER_CANCELLABLE); one statement, so it cannot race the staff's button."""
        with self.db:
            cur = self.db.execute(
                "update orders set status = 'cancelled'"
                " where id = ? and cafe_id = ? and user_id = ? and status in ('new', 'accepted') and payment_status = 'pending'",
                (order_id, cafe_id, user_id),
            )
        return cur.rowcount == 1

    def claim_transfer(self, cafe_id: int, user_id: int, order_id: int) -> bool:
        """The customer says the money is sent; staff still have to see it arrive."""
        with self.db:
            cur = self.db.execute(
                "update orders set payment_status = 'claimed' where id = ? and cafe_id = ? and user_id = ?"
                " and payment_method = 'transfer' and payment_status = 'pending' and status != 'cancelled'",
                (order_id, cafe_id, user_id),
            )
        return cur.rowcount == 1

    def confirm_payment(self, cafe_id: int, order_id: int) -> bool:
        with self.db:
            cur = self.db.execute(
                "update orders set payment_status = 'paid' where id = ? and cafe_id = ?"
                " and payment_method = 'transfer' and payment_status != 'paid' and status != 'cancelled'",
                (order_id, cafe_id),
            )
        return cur.rowcount == 1

    def set_customer_message(self, cafe_id: int, order_id: int, message_id: int) -> None:
        with self.db:
            self.db.execute(
                "update orders set customer_message_id = ? where id = ? and cafe_id = ?",
                (message_id, order_id, cafe_id),
            )

    def add_staff_message(self, order_id: int, chat_id: int, message_id: int) -> None:
        with self.db:
            self.db.execute("insert or replace into staff_messages values (?, ?, ?)", (order_id, chat_id, message_id))

    def staff_messages(self, order_id: int) -> list[tuple[int, int]]:
        rows = self.db.execute("select chat_id, message_id from staff_messages where order_id = ?", (order_id,))
        return [(r[0], r[1]) for r in rows]

    def open_orders(self, cafe_id: int, user_id: int, since: datetime) -> tuple[int, int]:
        """(the guest's orders still waiting for the kitchen, all their orders since then): against order spam."""
        waiting = self.db.execute(
            "select count(*) from orders where cafe_id = ? and user_id = ? and status = 'new'", (cafe_id, user_id)
        ).fetchone()[0]
        recent = self.db.execute(
            "select count(*) from orders where cafe_id = ? and user_id = ? and created_at >= ?",
            (cafe_id, user_id, since.astimezone(UTC).isoformat()),
        ).fetchone()[0]
        return waiting, recent

    def rate(self, cafe_id: int, user_id: int, order_id: int, stars: int) -> bool:
        """The guest's own order, once it is in their hands, rated once."""
        with self.db:
            cur = self.db.execute(
                "update orders set rating = ? where id = ? and cafe_id = ? and user_id = ? and rating is null"
                " and status in ('done', 'delivered')",
                (stars, order_id, cafe_id, user_id),
            )
        return cur.rowcount == 1

    def stats(self, cafe_id: int, since: datetime, until: datetime | None = None) -> tuple[int, int]:
        """(orders, their total) placed since then (and before `until`), cancelled ones not counted."""
        end = until.astimezone(UTC).isoformat() if until else "9"
        n, total = self.db.execute(
            "select count(*), coalesce(sum(total), 0) from orders"
            " where cafe_id = ? and status != 'cancelled' and created_at >= ? and created_at < ?",
            (cafe_id, since.astimezone(UTC).isoformat(), end),
        ).fetchone()
        return n, total
