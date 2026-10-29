"""Web admin through HTTP, on the same event loop as the bot code (as in production)."""

import re
from io import BytesIO

import httpx
import pytest
from aiogram.methods import EditMessageText, SendMessage
from PIL import Image

from cafe.bot import send_to_staff
from cafe.template import Template, import_cafe
from cafe.web import create_app
from tests.conftest import item_id

OWNER, CUSTOMER = 7, 100


@pytest.fixture
def web(store, demo, settings, fake):
    bot, session = fake
    store.join(demo.id, store.new_invite(demo.id), OWNER, "Owner")
    app = create_app(store, settings, lambda cafe_id: bot if cafe_id == demo.id else None)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://admin.example.uz")
    return client, session


async def login(client, store, cafe_id, user_id=OWNER):
    r = await client.get(f"/login/{store.create_login(cafe_id, user_id)}")
    assert r.status_code == 303 and r.headers["location"] == "/orders"
    return r


async def test_login_link_is_one_time(web, store, demo):
    client, _ = web
    assert (await client.get("/orders")).status_code == 401
    token = store.create_login(demo.id, OWNER)
    await client.get(f"/login/{token}")
    assert (await client.get("/orders")).status_code == 200
    client.cookies.clear()
    r = await client.get(f"/login/{token}")
    assert r.status_code == 401 and "/admin" in r.text
    await login(client, store, demo.id)
    await client.post("/logout")
    assert (await client.get("/menu")).status_code == 401


async def test_menu_editing_with_photo(web, store, demo, settings):
    client, _ = web
    await login(client, store, demo.id)
    r = await client.post("/categories", data={"name": "Завтраки"})
    assert r.status_code == 303
    cat = store.menu(demo.id, only_available=False)[-1]
    size = next(g.id for g in store.groups(demo.id) if g.name == "Размер")
    img = BytesIO()
    Image.new("RGB", (3000, 2000), "orange").save(img, "PNG")
    r = await client.post(
        "/items/new",
        data={
            "name": "Сырники",
            "price": "32000",
            "category_id": str(cat.id),
            "description": "Со сметаной",
            "groups": [str(size)],
        },
        files={"photo": ("p.png", img.getvalue(), "image/png")},
    )
    assert r.status_code == 303
    it = store.item(demo.id, item_id(store, demo.id, "Сырники"))
    assert (it.price, it.description, [g.name for g in it.groups]) == (32000, "Со сметаной", ["Размер"])
    with Image.open(settings.media_dir / it.photo) as saved:
        assert max(saved.size) == 1280  # resized for Telegram
    assert (await client.get(f"/media/{it.photo}")).status_code == 200
    page = (await client.get("/menu")).text
    assert "Сырники" in page and f"/media/{it.photo}" in page

    bad = await client.post(
        f"/items/{it.id}",
        data={"name": "Сырники", "price": "1", "category_id": str(cat.id)},
        files={"photo": ("x.jpg", b"not an image", "image/jpeg")},
    )
    assert "msg=" in bad.headers["location"] and store.item(demo.id, it.id).photo == it.photo
    await client.post(
        f"/items/{it.id}", data={"name": "Сырники", "price": "33000", "category_id": str(cat.id), "remove_photo": "1"}
    )
    assert store.item(demo.id, it.id).photo is None and not (settings.media_dir / it.photo).exists()

    await client.post(f"/items/{it.id}/available", data={})
    assert store.item(demo.id, it.id) is None  # stop-list
    await client.post(f"/items/{it.id}/available", data={"available": "1"})
    assert store.item(demo.id, it.id)
    r = await client.post(f"/categories/{cat.id}/delete")
    assert "msg=" in r.headers["location"]  # not empty
    await client.post(f"/items/{it.id}/delete")
    await client.post(f"/categories/{cat.id}/delete")
    assert "Завтраки" not in [c.name for c in store.menu(demo.id, only_available=False)]


async def test_modifiers(web, store, demo):
    client, _ = web
    await login(client, store, demo.id)
    await client.post("/modifiers", data={"name": "Топпинг", "multiple": "1"})
    group = store.groups(demo.id)[-1]
    assert (group.name, group.multiple) == ("Топпинг", True)
    await client.post(f"/modifiers/{group.id}/options", data={"name": "Маршмеллоу", "price": "3000"})
    option = store.groups(demo.id)[-1].options[0]
    await client.post(f"/options/{option.id}", data={"name": "Маршмеллоу", "price": "3500"})
    assert store.groups(demo.id)[-1].options[0].price == 3500
    assert "Маршмеллоу" in (await client.get("/modifiers")).text
    await client.post(f"/options/{option.id}/delete")
    await client.post(f"/modifiers/{group.id}/delete")
    assert "Топпинг" not in [g.name for g in store.groups(demo.id)]


async def test_settings(web, store, demo):
    client, _ = web
    await login(client, store, demo.id)
    form = {
        "name": "Limy Coffee",
        "greeting": "Салют!",
        "pickup_address": "Юнусабад 5",
        "payment_note": "Наличные",
        "open_time": "08:00",
        "close_time": "02:00",
        "delivery_min": "70000",
        "delivery_fee": "10000",
    }
    await client.post("/settings", data=form)
    c = store.cafe(demo.id)
    assert (c.name, c.greeting, c.open_time.hour, c.close_time.hour, c.delivery_enabled, c.delivery_min) == (
        "Limy Coffee",
        "Салют!",
        8,
        2,
        False,
        70000,
    )
    r = await client.post("/settings", data={**form, "open_time": ""})
    assert "msg=" in r.headers["location"] and store.cafe(demo.id).open_time.hour == 8
    await client.post("/settings", data={**form, "always_open": "1", "delivery_enabled": "1"})
    c = store.cafe(demo.id)
    assert c.open_time is None and c.delivery_enabled

    store.set_bot_username(demo.id, "demo_coffee_bot")
    await client.post("/settings/invite")
    assert re.search(r"https://t\.me/demo_coffee_bot\?start=join-\S+", (await client.get("/settings")).text)
    store.join(demo.id, store.new_invite(demo.id), 8, "Barista")
    await client.post(f"/admins/{OWNER}/delete")  # cannot remove yourself
    await client.post("/admins/8/delete")
    assert store.admins(demo.id) == [(OWNER, "Owner")]


async def test_order_status_from_web_reaches_telegram(web, store, demo, fake):
    client, session = web
    bot, _ = fake
    store.add_to_cart(demo.id, CUSTOMER, item_id(store, demo.id, "Эспрессо"), [])
    o = store.place(demo, CUSTOMER, "Ali", "+998901234567", "pickup", None, "без сахара")
    await send_to_staff(bot, store, demo, o)
    await login(client, store, demo.id)
    page = (await client.get("/orders")).text
    assert "№1" in page and "без сахара" in page and "Сегодня: 1 заказов на 18 000 сум" in page

    await client.post(f"/orders/{o.id}/status", data={"status": "accepted"})
    await client.post(f"/orders/{o.id}/status", data={"status": "ready"})
    assert store.order(demo.id, o.id).status == "ready"
    to_customer = [m.text for m in session.sent if isinstance(m, SendMessage) and m.chat_id == CUSTOMER]
    assert to_customer[-1].startswith("Заказ №1 готов!")
    card = [m for m in session.sent if isinstance(m, EditMessageText) and m.chat_id == OWNER][-1]
    assert "Заказ №1 · Готов" in card.text  # the Telegram card follows changes made on the web
    r = await client.post(f"/orders/{o.id}/status", data={"status": "accepted"})
    assert "msg=" in r.headers["location"]  # stale
    await client.post(f"/orders/{o.id}/status", data={"status": "done"})
    o = store.order(demo.id, o.id)
    assert (o.status, o.payment_status) == ("done", "paid")
    assert "№1" not in (await client.get("/orders")).text and "№1" in (await client.get("/orders?all=1")).text


async def test_admin_sees_only_own_cafe(web, store, demo):
    client, _ = web
    other = import_cafe(
        store,
        "tea",
        "2:tea",
        Template(name="Tea", menu=[{"category": "Чай", "items": [{"name": "Чай", "price": 5000}]}]),
    )
    tea = item_id(store, other, "Чай")
    store.add_to_cart(other, CUSTOMER, tea, [])
    order = store.place(store.cafe(other), CUSTOMER, "A", "1234567", "pickup", None, None)
    await login(client, store, demo.id)
    assert (await client.get(f"/items/{tea}")).headers["location"] == "/menu"
    await client.post(f"/items/{tea}/available", data={})
    await client.post(f"/items/{tea}/delete")
    await client.post(f"/orders/{order.id}/status", data={"status": "cancelled"})
    assert store.item(other, tea) and store.order(other, order.id).status == "new"
    assert store.consume_login(store.create_login(other, OWNER)) is None  # not an admin of the tea house
