"""Delivery by the map: distances, zones, the guest's pin through the Mini App, what staff get, the owner's settings."""

import httpx
import pytest
from aiogram.methods import SendLocation

from cafe import delivery
from cafe.web import create_app
from tests.conftest import FakeBots, init_data, pick

CUSTOMER, OWNER = 100, 7
CAFE = (41.3111, 69.2797)  # Amir Temur square
NEAR = (41.3200, 69.2797)  # ~1 km north
MID = (41.3560, 69.2797)  # ~5 km
FAR = (41.4500, 69.2797)  # ~15 km


def test_distances_and_zones(store, demo):
    assert delivery.km(CAFE, CAFE) == 0
    assert 0.95 < delivery.km(CAFE, NEAR) < 1.05 and 4.9 < delivery.km(CAFE, MID) < 5.1
    flat = store.cafe(demo.id)
    assert delivery.fee(flat, None) == flat.delivery_fee == delivery.fee(flat, FAR)  # no zones: one price anywhere
    store.update_cafe(demo.id, lat=CAFE[0], lon=CAFE[1], delivery_zones=[(7, 15000), (3, 10000)])
    cafe = store.cafe(demo.id)
    assert cafe.delivery_zones == [(3.0, 10000), (7.0, 15000)]  # kept nearest first, however they were given
    assert delivery.fee(cafe, NEAR) == 10000
    assert delivery.parse_zones([("7", "15 000"), ("", ""), ("3", "10000")]) == [(3.0, 10000), (7.0, 15000)]
    assert delivery.parse_zones([("три", "10000")]) is None and delivery.parse_zones([("0", "100")]) is None
    store.update_cafe(demo.id, delivery_zones=delivery.parse_zones([("7", "15000"), ("3", "10000")]))
    cafe = store.cafe(demo.id)
    assert (delivery.fee(cafe, NEAR), delivery.fee(cafe, MID), delivery.fee(cafe, FAR)) == (10000, 15000, None)
    assert delivery.fee(cafe, None) is None  # the price depends on the pin: no pin, no price
    assert delivery.map_link(NEAR) == "https://yandex.uz/maps/?pt=69.279700,41.320000&z=17&l=map"


@pytest.fixture
def app(store, demo, settings, fake):
    bot, session = fake
    store.join(demo.id, store.new_invite(demo.id), OWNER, "Owner")
    store.update_cafe(demo.id, lat=CAFE[0], lon=CAFE[1], delivery_zones=[(3, 10000), (7, 15000)])
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(store, settings, FakeBots({demo.id: bot}))),
        base_url="https://admin.example.uz",
    )
    return client, session


def order_body(store, demo, **kw) -> dict:
    latte = pick(store, demo.id, "Латте", ["M", "овсяное", "ваниль"], 2)  # 88 000, over the minimum
    return {
        "lines": [latte],
        "method": "delivery",
        "phone": "+998 90 123 45 67",
        "address": "Юнусабад 4, подъезд 2, этаж 5, кв 18",
        "payment": "cash",
        "lang": "ru",
    } | kw


async def test_the_pin_sets_the_delivery_price(app, store, demo):
    client, session = app
    headers = {"X-Telegram-Init-Data": init_data("1:demo", CUSTOMER)}
    r = await client.post("/app/demo/orders", json=order_body(store, demo), headers=headers)
    assert r.status_code == 409 and r.json() == {"error": "point"}
    r = await client.post("/app/demo/orders", json=order_body(store, demo, lat=FAR[0], lon=FAR[1]), headers=headers)
    assert r.json() == {"error": "far"}
    r = await client.post("/app/demo/orders", json=order_body(store, demo, lat=999, lon=0), headers=headers)
    assert r.status_code == 422
    r = await client.post("/app/demo/orders", json=order_body(store, demo, lat=MID[0], lon=MID[1]), headers=headers)
    assert r.json() == {"number": 1, "total": 88000 + 15000}  # the 7 km zone, priced on the server
    o = store.orders(demo.id, active_only=True)[0]
    assert o.point == MID and o.delivery_fee == 15000

    card = next(m for m in session.messages(OWNER) if m.text.startswith("Заказ №1"))
    assert "📍 5,0 км от вас" in card.text
    assert card.reply_markup.inline_keyboard[-1][0].url == delivery.map_link(MID)
    pin = next(m for m in session.sent if isinstance(m, SendLocation))
    assert (pin.chat_id, pin.latitude, pin.longitude) == (OWNER, *MID)

    me = (await client.get("/app/demo/me", headers=headers)).json()
    assert me["point"] == list(MID)  # next time the map opens there
    data = (await client.get("/app/demo")).text
    assert '"zones": [[3.0, 10000], [7.0, 15000]]' in data


async def test_pickup_ignores_the_pin(app, store, demo):
    client, _ = app
    headers = {"X-Telegram-Init-Data": init_data("1:demo", CUSTOMER)}
    body = order_body(store, demo, method="pickup", lat=FAR[0], lon=FAR[1])
    assert (await client.post("/app/demo/orders", json=body, headers=headers)).json()["total"] == 88000
    assert store.orders(demo.id, active_only=True)[0].point is None


async def test_the_owner_sets_the_point_and_zones(app, store, demo):
    client, _ = app
    await client.post(f"/login/{store.create_login(demo.id, OWNER)}")
    page = (await client.get("/settings")).text
    assert 'id="cafe-map"' in page and 'value="3"' in page and 'value="10000"' in page
    form = {
        "name": "Demo Coffee",
        "greeting": "Привет",
        "always_open": "1",
        "delivery_enabled": "1",
        "lat": "41.300000",
        "lon": "69.250000",
        "zone_km": ["2,5", "6", ""],
        "zone_fee": ["9 000", "14000", ""],
    }
    await client.post("/settings", data=form)
    cafe = store.cafe(demo.id)
    assert cafe.point == (41.3, 69.25) and cafe.delivery_zones == [(2.5, 9000), (6.0, 14000)]
    r = await client.post("/settings", data=form | {"zone_km": ["много"], "zone_fee": ["1"]})
    assert "where" in r.headers["location"] and store.cafe(demo.id).delivery_zones == [(2.5, 9000), (6.0, 14000)]
    r = await client.post("/settings", data=form | {"lat": "", "lon": ""})
    assert store.cafe(demo.id).point is None and "where" in r.headers["location"]  # zones without a point: told
