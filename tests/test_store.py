from datetime import UTC, datetime, time, timedelta

import pytest

from cafe.bot import build
from cafe.config import Hours, Settings, load_client
from cafe.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "cafe.db", load_client("demo_coffee"))


def test_cart_and_order(store):
    store.add(1, "latte")
    store.add(1, "latte")
    store.add(1, "croissant")
    store.add(1, "croissant", -1)  # down to zero: removed
    assert [(ln.item.id, ln.qty) for ln in store.cart(1)] == [("latte", 2)]
    o = store.place(1, "Ali", "+998901234567", "delivery", "Chilonzor 1", None, fee=15000)
    assert o.total == 2 * 30000 + 15000 and "Латте × 2" in o.lines and o.status == "new"
    assert store.cart(1) == []  # placing an order empties the cart
    assert store.place(1, "Ali", "x", "pickup", None, None, fee=0) is None  # nothing to order


def test_owner_edits_price_and_stop_list(store):
    store.add(1, "latte")
    store.set_price("latte", 32000)
    assert store.item("latte").price == 32000 and store.cart(1)[0].sum == 32000
    store.set_available("latte", False)
    assert store.item("latte") is None and store.cart(1) == []  # stopped items leave menu and carts
    assert any(i.id == "latte" for _, items in store.menu(only_available=False) for i in items)
    store.set_available("latte", True)
    assert store.item("latte").price == 32000  # price edit survives the stop-list


def test_stats_skip_cancelled(store):
    for _ in range(2):
        store.add(1, "espresso")
        o = store.place(1, "A", "1234567", "pickup", None, None, fee=0)
    store.set_status(o.id, "cancelled")
    assert store.stats(datetime.now(UTC) - timedelta(hours=1)) == (1, 18000)


def test_opening_hours_and_money():
    c = load_client("demo_coffee")
    c.hours = Hours(open=time(8), close=time(22))
    assert c.is_open(datetime(2026, 1, 1, 12)) and not c.is_open(datetime(2026, 1, 1, 23))
    c.hours = Hours(open=time(18), close=time(2))  # past midnight
    assert c.is_open(datetime(2026, 1, 1, 1)) and not c.is_open(datetime(2026, 1, 1, 12))
    assert c.money(1250000) == "1 250 000 сум"


def test_bot_builds(store):
    assert build(Settings(bot_token="1:x", admin_ids="1, 2"), store)
    assert Settings(admin_ids="1, 2").admins == {1, 2}
