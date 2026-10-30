import sqlite3
import time as clock
import zipfile
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from cafe import backup as backup_module
from cafe import store as store_module
from cafe.backup import make_backup
from cafe.media import ensure_thumbs, save_photo, thumb
from cafe.store import MIGRATIONS, Store
from cafe.template import Template, import_cafe
from tests.conftest import item_id, option_id, order, pick

V0 = Path(__file__).with_name("schema_v0.sql")


def wanted(*picks: dict) -> list[tuple[int, list[int], int]]:
    return [(p["item_id"], p["option_ids"], p["qty"]) for p in picks]


def test_demo_import(store, demo):
    menu = store.menu(demo.id)
    assert [c.name for c in menu] == ["Кофе", "Не кофе", "Десерты"]
    latte = store.item(demo.id, item_id(store, demo.id, "Латте"))
    assert [g.name for g in latte.groups] == ["Размер", "Молоко", "Сироп"]
    assert latte.description and demo.delivery_min == 50000 and demo.greeting.startswith("Привет")
    assert demo.active() and demo.payment_methods() == ["cash"]  # no card: no transfers


def test_template_rejects_unknown_modifier():
    with pytest.raises(ValidationError):
        Template.model_validate(
            {"name": "X", "menu": [{"category": "A", "items": [{"name": "Чай", "price": 1, "modifiers": ["Нет"]}]}]}
        )


def test_lines_are_priced_and_checked_by_the_menu(store, demo):
    latte = store.item(demo.id, item_id(store, demo.id, "Латте"))
    assert latte.default_options() == [option_id(store, demo.id, "S"), option_id(store, demo.id, "обычное")]
    (line,) = store.lines(demo.id, wanted(pick(store, demo.id, "Латте", ["карамель", "ваниль", "овсяное", "M"], 2)))
    assert line.title == "Латте (M, овсяное, ваниль, карамель)"  # options in menu order, whatever the input order
    assert (line.unit, line.sum) == (30000 + 4000 + 6000 + 4000 + 4000, 2 * 48000)
    refused = store.lines(
        demo.id,
        wanted(
            pick(store, demo.id, "Латте", ["обычное"]),  # size missing
            pick(store, demo.id, "Латте", ["S", "M", "обычное"]),  # two sizes
            pick(store, demo.id, "Круассан", ["S"]),  # an option of another item
            pick(store, demo.id, "Круассан", [], 0),
            pick(store, demo.id, "Круассан", [], 100),
        ),
    )
    assert refused == [None] * 5
    croissant = item_id(store, demo.id, "Круассан")
    store.set_available(demo.id, croissant, False)
    assert store.lines(demo.id, [(croissant, [], 1)]) == [None]  # stop-list


def test_order_snapshot_and_customer_profile(store, demo):
    o = order(
        store,
        demo,
        1,
        pick(store, demo.id, "Латте", ["S", "обычное"], 2),
        method="delivery",
        address="Chilonzor 1",
    )
    assert (o.number, o.subtotal, o.delivery_fee, o.total) == (1, 60000, 15000, 75000)
    assert o.lines_text(demo) == "Латте (S, обычное) × 2 — 60 000 сум"
    assert o.lines[0]["item_id"] == item_id(store, demo.id, "Латте")  # ids make "repeat" possible
    assert (o.payment_method, o.payment_status, o.status) == ("cash", "pending", "new")
    c = store.customer(demo.id, 1)
    assert (c.phone, c.address, c.lang) == ("+998901234567", "Chilonzor 1", "ru")

    # pickup never keeps an address (it once leaked from an abandoned delivery checkout) and keeps the saved one
    o = order(store, demo, 1, phone="+998 90 000 00 00", address="Chilonzor 1", lang="uz")
    assert (o.method, o.address, o.delivery_fee) == ("pickup", None, 0)
    c = store.customer(demo.id, 1)
    assert (c.phone, c.address, c.lang) == ("+998 90 000 00 00", "Chilonzor 1", "uz")


def test_status_flows_by_how_the_order_is_received(store, demo):
    pickup = order(store, demo, 1)
    delivery = order(store, demo, 1, pick(store, demo.id, "Чизкейк Нью-Йорк", qty=3), method="delivery", address="A")
    steps = {}
    for o in (pickup, delivery):
        path = []
        while o.next_statuses:
            path.append((o.status, o.next_statuses))
            store.set_status(demo.id, o.id, o.next_statuses[0])
            o = store.order(demo.id, o.id)
        steps[o.method] = path
        assert o.payment_status == "paid"  # cash is collected when the order is handed over
    assert steps["pickup"] == [
        ("new", ["accepted", "cancelled"]),
        ("accepted", ["cooking", "cancelled"]),
        ("cooking", ["ready", "cancelled"]),
        ("ready", ["done"]),
    ]
    assert [s for s, _ in steps["delivery"]] == ["new", "accepted", "cooking", "courier"]
    assert store.order(demo.id, delivery.id).status == "delivered"
    assert store.orders(demo.id, active_only=True) == []


def test_customer_cancels_only_a_new_own_order(store, demo):
    o = order(store, demo, 1)
    assert not store.cancel_by_customer(demo.id, 2, o.id)  # somebody else's order
    assert store.cancel_by_customer(demo.id, 1, o.id)
    assert store.order(demo.id, o.id).status == "cancelled"
    assert not store.cancel_by_customer(demo.id, 1, o.id)
    accepted = order(store, demo, 1)
    store.set_status(demo.id, accepted.id, "accepted")
    assert not store.cancel_by_customer(demo.id, 1, accepted.id)  # staff were first


def test_card_transfer_payment(store, demo):
    store.update_cafe(demo.id, card_number="8600 1234 5678 9012", card_holder="Ali")
    assert store.cafe(demo.id).payment_methods() == ["cash", "transfer"]
    o = order(store, demo, 1, payment_method="transfer")
    assert o.awaits_transfer and not store.claim_transfer(demo.id, 2, o.id)
    assert store.claim_transfer(demo.id, 1, o.id) and not store.claim_transfer(demo.id, 1, o.id)
    assert store.order(demo.id, o.id).payment_status == "claimed"
    for status in ("accepted", "cooking", "ready", "done"):
        store.set_status(demo.id, o.id, status)
    assert store.order(demo.id, o.id).payment_status == "claimed"  # handing over does not prove a transfer
    assert store.confirm_payment(demo.id, o.id) and not store.confirm_payment(demo.id, o.id)
    assert not store.order(demo.id, o.id).awaits_transfer
    cash = order(store, demo, 1)
    assert not store.confirm_payment(demo.id, cash.id)


def test_repeat_uses_todays_menu(store, demo):
    o = order(store, demo, 1, pick(store, demo.id, "Латте", ["M", "овсяное"], 2), pick(store, demo.id, "Круассан"))
    store.set_available(demo.id, item_id(store, demo.id, "Круассан"), False)
    store.update_item(
        demo.id,
        item_id(store, demo.id, "Латте"),
        name="Латте",
        description="",
        price=32000,
        category_id=store.menu(demo.id)[0].id,
        group_ids=[g.id for g in store.groups(demo.id) if g.name in ("Размер", "Молоко")],
    )
    lines, missing = store.repeat(demo.id, 1, o.id)
    assert [(ln.title, ln.qty, ln.unit) for ln in lines] == [("Латте (M, овсяное)", 2, 42000)]
    assert missing == ["Круассан"]
    assert store.repeat(demo.id, 2, o.id) is None  # not their order


def test_categories_order_and_delete(store, demo):
    cats = store.menu(demo.id, only_available=False)
    store.move_category(demo.id, cats[2].id, -1)
    assert [c.name for c in store.menu(demo.id)] == ["Кофе", "Десерты", "Не кофе"]
    assert not store.delete_category(demo.id, cats[0].id)  # not empty
    empty = store.add_category(demo.id, "Завтраки")
    assert [c.name for c in store.menu(demo.id)][-1] == "Не кофе"  # empty categories are hidden from customers
    assert store.delete_category(demo.id, empty)


def test_cafes_are_isolated(store, demo):
    other = import_cafe(
        store,
        "other",
        "2:other",
        Template(name="Other", menu=[{"category": "Чай", "items": [{"name": "Чай", "price": 5000}]}]),
    )
    latte = item_id(store, demo.id, "Латте")
    assert store.item(other, latte) is None
    assert store.lines(other, [(latte, [], 1)]) == [None]
    first = order(store, demo, 1)
    second = order(store, store.cafe(other), 1, pick(store, other, "Чай"))
    assert second.number == 1  # numbering is per cafe
    assert store.order(other, first.id) is None and store.order(demo.id, second.id) is None
    assert store.customer(other, 1).phone and not store.cancel_by_customer(other, 1, first.id)
    assert store.repeat(other, 1, first.id) is None


def test_stats_skip_cancelled(store, demo):
    for _ in range(2):
        o = order(store, demo, 1)
    store.set_status(demo.id, o.id, "cancelled")
    assert store.stats(demo.id, datetime.now(UTC) - timedelta(hours=1)) == (1, 18000)


def test_opening_hours_subscription_and_money(store, demo):
    store.update_cafe(demo.id, open_time=time(8), close_time=time(22))
    c = store.cafe(demo.id)
    assert c.is_open(datetime(2026, 1, 1, 12)) and not c.is_open(datetime(2026, 1, 1, 23))
    store.update_cafe(demo.id, open_time=time(18), close_time=time(2))  # past midnight
    c = store.cafe(demo.id)
    assert c.is_open(datetime(2026, 1, 1, 1)) and not c.is_open(datetime(2026, 1, 1, 12))
    assert c.money(1250000) == "1 250 000 сум" and c.hours() == "18:00–02:00"

    today = c.now().date()
    store.set_paid_until(demo.id, today)
    assert store.cafe(demo.id).active()  # the last paid day still works
    store.set_paid_until(demo.id, today - timedelta(days=1))
    assert not store.cafe(demo.id).active()
    store.set_paid_until(demo.id, None)
    store.set_enabled(demo.id, False)
    assert not store.cafe(demo.id).active()


def test_invite_and_login_links(store, demo, monkeypatch):
    code = store.new_invite(demo.id)
    assert not store.join(demo.id, "wrong", 7, "Owner")
    assert store.join(demo.id, code, 7, "Owner")
    assert not store.join(demo.id, code, 8, "Stranger")  # the code works once
    assert store.admins(demo.id) == [(7, "Owner")]

    assert store.consume_login(store.create_login(demo.id, 8)) is None  # not an admin
    token = store.create_login(demo.id, 7)
    session = store.consume_login(token)
    assert store.session(session) == (demo.id, 7)
    assert store.consume_login(token) is None  # one-time
    late = store.create_login(demo.id, 7)
    later = clock.time() + store_module.LOGIN_TTL + 1
    monkeypatch.setattr(store_module.clock, "time", lambda: later)
    assert store.consume_login(late) is None  # expired
    store.remove_admin(demo.id, 7)
    assert store.session(session) is None  # removed admins are logged out


def schema(db: sqlite3.Connection) -> dict[str, list[tuple]]:
    tables = [r[0] for r in db.execute("select name from sqlite_master where type = 'table' order by name")]
    return {t: [tuple(c) for c in db.execute(f"pragma table_info({t})")] for t in tables}


def test_migration_from_the_first_release(tmp_path):
    old = sqlite3.connect(tmp_path / "old.db")
    old.executescript(V0.read_text())
    old.executescript("""
        insert into cafes (id, slug, bot_token, name) values (1, 'demo', '1:x', 'Demo');
        insert into categories (id, cafe_id, name) values (1, 1, 'Кофе');
        insert into items (id, cafe_id, category_id, name, price, photo_file_id) values (1, 1, 1, 'Эспрессо', 18000, 'f');
        insert into carts (cafe_id, user_id, item_id, options, qty) values (1, 5, 1, '', 1);
    """)
    statuses = [("pickup", "accepted"), ("delivery", "accepted"), ("pickup", "ready"), ("delivery", "ready")]
    statuses += [("pickup", "done"), ("delivery", "done"), ("delivery", "cancelled"), ("pickup", "new")]
    for n, (method, status) in enumerate(statuses, 1):
        old.execute(
            "insert into orders (cafe_id, number, user_id, customer, phone, method, lines, subtotal, delivery_fee,"
            " total, status, created_at) values (1, ?, 5, 'A', '1', ?, '[]', 1, 0, 1, ?, '2026-01-01T00:00:00+00:00')",
            (n, method, status),
        )
    old.commit()
    old.close()

    store = Store(tmp_path / "old.db")
    assert store.db.execute("pragma user_version").fetchone()[0] == len(MIGRATIONS)
    got = [r[0] for r in store.db.execute("select status from orders order by number")]
    # what staff saw as «Готовится» stays so; delivery steps get their own names
    assert got == ["cooking", "cooking", "ready", "courier", "done", "delivered", "cancelled", "new"]
    c = store.cafe(1)
    assert (c.brand_color, c.card_number, c.enabled, c.paid_until) == ("#8b5e3c", "", True, None)
    assert store.item(1, 1).name == "Эспрессо"

    fresh = Store(tmp_path / "fresh.db")
    assert schema(store.db) == schema(fresh.db)  # migrations end exactly where a new database starts
    Store(tmp_path / "old.db")  # reopening runs nothing twice


def test_photos_have_thumbnails(tmp_path):
    from io import BytesIO

    from PIL import Image

    png = BytesIO()
    Image.new("RGBA", (2000, 1000), (255, 0, 0, 0)).save(png, "PNG")  # a transparent logo
    name = save_photo(png.getvalue(), tmp_path)
    with Image.open(tmp_path / name) as big, Image.open(tmp_path / thumb(name)) as small:
        assert (max(big.size), max(small.size), big.getpixel((0, 0))) == (1280, 480, (255, 255, 255))
    (tmp_path / thumb(name)).unlink()
    ensure_thumbs(tmp_path)  # photos from before thumbnails get one at startup
    assert (tmp_path / thumb(name)).exists()


async def test_daily_backup_keeps_two_weeks(store, demo, settings, monkeypatch):
    settings.media_dir.mkdir(parents=True)
    (settings.media_dir / "a.jpg").write_bytes(b"jpeg")
    path = await make_backup(store, settings, date(2026, 10, 1))
    with zipfile.ZipFile(path) as z:
        assert sorted(z.namelist()) == ["limybot.db", "photos/a.jpg"]
        z.extract("limybot.db", settings.backup_dir / "restored")
    restored = Store(settings.backup_dir / "restored" / "limybot.db")
    assert restored.cafe(demo.id).name == demo.name
    monkeypatch.setattr(backup_module, "KEEP", 3)
    for day in range(2, 6):
        await make_backup(store, settings, date(2026, 10, day))
    assert [p.name for p in sorted(settings.backup_dir.glob("*.zip"))] == [
        "limybot-2026-10-03.zip",
        "limybot-2026-10-04.zip",
        "limybot-2026-10-05.zip",
    ]


def test_business_day(store, demo):
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Tashkent")
    store.update_cafe(demo.id, open_time=time(18), close_time=time(2))  # a night cafe is one day, not two
    c = store.cafe(demo.id)
    began = c.day_began(datetime(2026, 1, 2, 1, 30, tzinfo=tz))
    assert began == datetime(2026, 1, 1, 18, tzinfo=tz) and c.day_ends(began) == datetime(2026, 1, 2, 2, tzinfo=tz)
    store.update_cafe(demo.id, open_time=None, close_time=None, day_start=5)  # around the clock: from 05:00
    c = store.cafe(demo.id)
    began = c.day_began(datetime(2026, 1, 2, 4, tzinfo=tz))
    assert began == datetime(2026, 1, 1, 5, tzinfo=tz) and c.day_ends(began) == datetime(2026, 1, 2, 5, tzinfo=tz)
