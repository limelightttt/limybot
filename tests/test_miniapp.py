"""The Mini App over HTTP on the same event loop as the bot code: the public page, Telegram-signed customer calls,
checkout rules, saved phone and address, repeating an order."""

import json
import re

import httpx
import pytest

from cafe.web import create_app
from tests.conftest import init_data, item_id, option_id, order, pick

CUSTOMER, OWNER = 100, 7


@pytest.fixture
def app(store, demo, settings, fake):
    bot, session = fake
    store.join(demo.id, store.new_invite(demo.id), OWNER, "Owner")
    running = {demo.id: bot}
    app = create_app(store, settings, running.get)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://admin.example.uz")
    return client, session, running


def signed(uid: int = CUSTOMER, token: str = "1:demo", **kw) -> dict:
    return {"X-Telegram-Init-Data": init_data(token, uid, **kw)}


def body(*lines: dict, **kw) -> dict:
    return {
        "lines": list(lines),
        "method": "pickup",
        "phone": "+998 90 123 45 67",
        "payment": "cash",
        "lang": "ru",
    } | kw


def page_data(html: str) -> dict:
    return json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.DOTALL)[1])


async def test_page_has_the_menu_and_the_brand(app, store, demo):
    client, _, _ = app
    store.set_available(demo.id, item_id(store, demo.id, "Круассан"), False)
    store.update_cafe(demo.id, brand_color="#0a7d55")
    r = await client.get("/app/demo")
    assert r.status_code == 200 and "--brand: #0a7d55" in r.text and "telegram-web-app.js" in r.text
    data = page_data(r.text)
    names = [it["name"] for c in data["menu"] for it in c["items"]]
    assert "Латте" in names and "Круассан" not in names  # the stop-list is hidden
    latte = next(it for c in data["menu"] for it in c["items"] if it["name"] == "Латте")
    assert [g["name"] for g in latte["groups"]] == ["Размер", "Молоко", "Сироп"]
    assert data["delivery"] == {"enabled": True, "min": 50000, "fee": 15000} and data["payments"] == ["cash"]
    assert set(data["texts"]) == {"ru", "uz"} and data["texts"]["uz"].keys() == data["texts"]["ru"].keys()
    assert (await client.get("/app/nope")).status_code == 404


async def test_customer_calls_need_telegrams_signature(app, store, demo):
    client, _, _ = app
    espresso = pick(store, demo.id, "Эспрессо")
    assert (await client.post("/app/demo/orders", json=body(espresso))).status_code == 401
    forged = await client.post("/app/demo/orders", json=body(espresso), headers=signed(token="2:other-bot"))
    assert forged.status_code == 401
    stale = await client.post("/app/demo/orders", json=body(espresso), headers=signed(age=2 * 24 * 3600))
    assert stale.status_code == 401
    assert store.orders(demo.id, active_only=False) == []


async def test_order_from_the_mini_app(app, store, demo):
    client, session, _ = app
    latte = pick(store, demo.id, "Латте", ["M", "овсяное", "ваниль"], 2)
    r = await client.post(
        "/app/demo/orders",
        json=body(latte, method="delivery", address="Чиланзар 9, кв 12", comment="без сахара"),
        headers=signed(),
    )
    assert r.json() == {"number": 1, "total": 88000 + 15000}
    o = store.orders(demo.id, active_only=True)[0]
    assert (o.method, o.address, o.comment, o.customer) == ("delivery", "Чиланзар 9, кв 12", "без сахара", "User100")
    staff = session.texts(OWNER)[-1]
    assert "Заказ №1 · Новый" in staff and "Латте (M, овсяное, ваниль) × 2 — 88 000 сум" in staff
    # what the customer gets in the chat is the full summary: lines with options, delivery, total, address, payment
    sent = session.texts(CUSTOMER)[-1]
    for part in (
        "Латте (M, овсяное, ваниль) × 2 — 88 000 сум",
        "Доставка — 15 000 сум",
        "<b>Итого: 103 000 сум</b>",
        "🛵 Доставка: Чиланзар 9, кв 12",
        "📱 +998 90 123 45 67",
        "💬 без сахара",
        "💳 Оплата при получении",
    ):
        assert part in sent

    me = (await client.get("/app/demo/me", headers=signed())).json()
    assert me == {"phone": "+998 90 123 45 67", "address": "Чиланзар 9, кв 12", "lang": "ru"}
    other = (await client.get("/app/demo/me", headers=signed(200, lang="uz"))).json()
    assert other == {"phone": None, "address": None, "lang": "uz"}


async def test_checkout_rules(app, store, demo):
    client, _, running = app
    espresso = pick(store, demo.id, "Эспрессо")

    async def refused(payload: dict) -> str:
        r = await client.post("/app/demo/orders", json=payload, headers=signed())
        assert r.status_code == 409
        return r.json()["error"]

    assert await refused(body(espresso, method="delivery", address="A")) == "min_delivery"
    cakes = pick(store, demo.id, "Чизкейк Нью-Йорк", qty=3)
    assert await refused(body(cakes, method="delivery", address="  ")) == "address"
    assert await refused(body(espresso, phone="12")) == "phone"
    assert await refused(body(espresso, payment="transfer")) == "payment"  # no card in the settings
    assert await refused(body(pick(store, demo.id, "Латте", ["обычное"]))) == "menu_changed"  # no size
    store.set_available(demo.id, espresso["item_id"], False)
    assert await refused(body(espresso)) == "menu_changed"
    store.set_available(demo.id, espresso["item_id"], True)
    store.update_cafe(demo.id, delivery_enabled=False)
    assert await refused(body(cakes, method="delivery", address="A")) == "no_delivery"
    bot = running.pop(demo.id)
    assert await refused(body(espresso)) == "unavailable"  # staff would never see it
    running[demo.id] = bot
    now = demo.now()
    store.update_cafe(
        demo.id,
        open_time=now.replace(hour=(now.hour + 2) % 24).time(),
        close_time=now.replace(hour=(now.hour + 3) % 24).time(),
    )
    assert await refused(body(espresso)) == "closed"
    store.set_enabled(demo.id, False)
    assert await refused(body(espresso)) == "inactive"
    assert store.orders(demo.id, active_only=False) == []


async def test_pickup_drops_a_typed_address(app, store, demo):
    """The customer typed an address, then switched to pickup: the order is pickup and says where to come."""
    client, session, _ = app
    r = await client.post(
        "/app/demo/orders", json=body(pick(store, demo.id, "Эспрессо"), address="Чиланзар 9"), headers=signed()
    )
    o = store.order(demo.id, store.orders(demo.id, active_only=True)[0].id)
    assert r.status_code == 200 and (o.method, o.address) == ("pickup", None)
    assert "🏃 Самовывоз: Ташкент, ул. Примерная, 1" in session.texts(CUSTOMER)[-1]
    assert store.customer(demo.id, CUSTOMER).address is None


async def test_card_transfer_order(app, store, demo):
    client, session, _ = app
    store.update_cafe(demo.id, card_number="8600 1234 5678 9012", card_holder="Ali")
    assert page_data((await client.get("/app/demo")).text)["payments"] == ["cash", "transfer"]
    r = await client.post(
        "/app/demo/orders", json=body(pick(store, demo.id, "Эспрессо"), payment="transfer"), headers=signed()
    )
    assert r.status_code == 200
    assert store.orders(demo.id, active_only=True)[0].payment_method == "transfer"
    assert "8600 1234 5678 9012" in session.texts(CUSTOMER)[-1]


async def test_repeat_an_order(app, store, demo):
    client, _, _ = app
    past = order(
        store, demo, CUSTOMER, pick(store, demo.id, "Латте", ["L", "кокосовое"], 2), pick(store, demo.id, "Круассан")
    )
    store.set_available(demo.id, item_id(store, demo.id, "Круассан"), False)
    me = (await client.get(f"/app/demo/me?repeat={past.id}", headers=signed())).json()
    latte = [option_id(store, demo.id, "L"), option_id(store, demo.id, "кокосовое")]
    assert me["repeat"] == {
        "lines": [{"item_id": item_id(store, demo.id, "Латте"), "option_ids": latte, "qty": 2}],
        "missing": ["Круассан"],
    }
    stranger = (await client.get(f"/app/demo/me?repeat={past.id}", headers=signed(200))).json()
    assert "repeat" not in stranger


async def test_language_choice_reaches_the_bot(app, store, demo):
    client, session, _ = app
    await client.post("/app/demo/lang", json={"lang": "uz"}, headers=signed())
    assert (await client.get("/app/demo/me", headers=signed())).json()["lang"] == "uz"
    await client.post("/app/demo/orders", json=body(pick(store, demo.id, "Эспрессо"), lang="uz"), headers=signed())
    assert "✅ №1 buyurtma «Demo Coffee»ga yuborildi" in session.texts(CUSTOMER)[-1]
    assert "18 000 soʻm" in session.texts(CUSTOMER)[-1]
