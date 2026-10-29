import time as clock
from datetime import UTC, datetime, time, timedelta

import pytest
from pydantic import ValidationError

from cafe import store as store_module
from cafe.template import Template, import_cafe
from tests.conftest import item_id, option_id


def test_demo_import(store, demo):
    menu = store.menu(demo.id)
    assert [c.name for c in menu] == ["Кофе", "Не кофе", "Десерты"]
    latte = store.item(demo.id, item_id(store, demo.id, "Латте"))
    assert [g.name for g in latte.groups] == ["Размер", "Молоко", "Сироп"]
    assert latte.description and demo.delivery_min == 50000 and demo.greeting.startswith("Привет")


def test_template_rejects_unknown_modifier():
    with pytest.raises(ValidationError):
        Template.model_validate(
            {"name": "X", "menu": [{"category": "A", "items": [{"name": "Чай", "price": 1, "modifiers": ["Нет"]}]}]}
        )


def test_modifiers_price_and_cart_lines(store, demo):
    latte = store.item(demo.id, item_id(store, demo.id, "Латте"))
    m, oat, vanilla, caramel = (option_id(store, demo.id, n) for n in ("M", "овсяное", "ваниль", "карамель"))
    s = option_id(store, demo.id, "S")
    milk = option_id(store, demo.id, "обычное")
    assert latte.default_options() == [s, milk]  # first option of every single-choice group
    line = store.add_to_cart(demo.id, 1, latte.id, [m, oat, vanilla, caramel])
    assert line.unit == 30000 + 4000 + 6000 + 4000 + 4000
    assert line.title == "Латте (M, овсяное, ваниль, карамель)"
    store.add_to_cart(demo.id, 1, latte.id, [caramel, vanilla, oat, m])  # same choice, other order: same line
    store.add_to_cart(demo.id, 1, latte.id, latte.default_options())  # different choice: new line
    assert [(ln.title, ln.qty) for ln in store.cart(demo.id, 1)] == [
        ("Латте (M, овсяное, ваниль, карамель)", 2),
        ("Латте (S, обычное)", 1),
    ]


def test_invalid_choices_are_refused(store, demo):
    latte = item_id(store, demo.id, "Латте")
    s, m, milk = (option_id(store, demo.id, n) for n in ("S", "M", "обычное"))
    assert store.add_to_cart(demo.id, 1, latte, [milk]) is None  # size missing
    assert store.add_to_cart(demo.id, 1, latte, [s, m, milk]) is None  # two sizes
    croissant = item_id(store, demo.id, "Круассан")
    assert store.add_to_cart(demo.id, 1, croissant, [s]) is None  # option of another item
    assert store.add_to_cart(demo.id, 1, croissant, []).title == "Круассан"


def test_cart_change_and_order_snapshot(store, demo):
    latte = store.item(demo.id, item_id(store, demo.id, "Латте"))
    line = store.add_to_cart(demo.id, 1, latte.id, latte.default_options())
    store.change(demo.id, 1, line.id, 1)
    croissant = store.add_to_cart(demo.id, 1, item_id(store, demo.id, "Круассан"), [])
    store.change(demo.id, 1, croissant.id, -1)  # down to zero: removed
    o = store.place(demo, 1, "Ali", "+998901234567", "delivery", "Chilonzor 1", None)
    assert (o.number, o.subtotal, o.delivery_fee, o.total) == (1, 60000, 15000, 75000)
    assert o.lines_text(demo) == "Латте (S, обычное) × 2 — 60 000 сум"
    assert (o.payment_method, o.payment_status) == ("cash", "pending")
    assert store.cart(demo.id, 1) == []  # placing an order empties the cart
    assert store.place(demo, 1, "Ali", "x", "pickup", None, None) is None  # nothing to order


def test_menu_edits_reach_carts(store, demo):
    croissant = item_id(store, demo.id, "Круассан")
    store.add_to_cart(demo.id, 1, croissant, [])
    store.update_item(
        demo.id,
        croissant,
        name="Круассан",
        description="",
        price=22000,
        category_id=store.menu(demo.id)[2].id,
        group_ids=[],
    )
    assert store.cart(demo.id, 1)[0].sum == 22000
    store.set_available(demo.id, croissant, False)
    assert store.item(demo.id, croissant) is None and store.cart(demo.id, 1) == []  # stopped items leave carts
    store.set_available(demo.id, croissant, True)
    assert store.cart(demo.id, 1)[0].sum == 22000
    # an item gets a required group after it was put in the cart: the stale line drops out
    size = next(g.id for g in store.groups(demo.id) if g.name == "Размер")
    store.update_item(
        demo.id,
        croissant,
        name="Круассан",
        description="",
        price=22000,
        category_id=store.menu(demo.id)[2].id,
        group_ids=[size],
    )
    assert store.cart(demo.id, 1) == []


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
    assert store.add_to_cart(other, 1, latte, []) is None
    store.set_available(other, latte, False)  # someone else's id: no effect
    assert store.item(demo.id, latte)
    tea = item_id(store, other, "Чай")
    for cafe_id, it in ((demo.id, item_id(store, demo.id, "Круассан")), (other, tea)):
        store.add_to_cart(cafe_id, 1, it, [])
        o = store.place(store.cafe(cafe_id), 1, "A", "1234567", "pickup", None, None)
        assert o.number == 1  # numbering is per cafe
    assert store.order(other, o.id) and store.order(demo.id, o.id) is None


def test_stats_skip_cancelled(store, demo):
    for _ in range(2):
        store.add_to_cart(demo.id, 1, item_id(store, demo.id, "Эспрессо"), [])
        o = store.place(demo, 1, "A", "1234567", "pickup", None, None)
    store.set_status(demo.id, o.id, "cancelled")
    assert store.stats(demo.id, datetime.now(UTC) - timedelta(hours=1)) == (1, 18000)


def test_done_marks_cash_paid(store, demo):
    store.add_to_cart(demo.id, 1, item_id(store, demo.id, "Эспрессо"), [])
    o = store.place(demo, 1, "A", "1234567", "pickup", None, None)
    store.set_status(demo.id, o.id, "done")
    assert store.order(demo.id, o.id).payment_status == "paid"


def test_opening_hours_and_money(store, demo):
    store.update_cafe(demo.id, open_time=time(8), close_time=time(22))
    c = store.cafe(demo.id)
    assert c.is_open(datetime(2026, 1, 1, 12)) and not c.is_open(datetime(2026, 1, 1, 23))
    store.update_cafe(demo.id, open_time=time(18), close_time=time(2))  # past midnight
    c = store.cafe(demo.id)
    assert c.is_open(datetime(2026, 1, 1, 1)) and not c.is_open(datetime(2026, 1, 1, 12))
    assert c.money(1250000) == "1 250 000 сум"


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
