"""Bugs found by the review of everything at once, each pinned by a test so it stays fixed."""

import asyncio
import io
import json
import zipfile
from datetime import UTC, datetime

import httpx
import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import SendMessage
from PIL import Image

from cafe import geocode
from cafe.bot import change_status, staff_card
from cafe.media import BadImage, save_photo
from cafe.notify import TZ, Notifier
from cafe.store import OPERATOR, OPS
from cafe.web import create_app
from tests.conftest import FakeBots, FakeSession, init_data, order, pick

OWNER = 7


def client_for(store, settings, bots):
    app = create_app(store, settings, bots)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://admin.example.uz")


async def test_a_telegram_failure_does_not_double_the_order(store, demo, settings, fake):
    """The order is saved before the messages go: a timeout then must not show "no connection" to the guest, who
    would tap again and order twice."""
    bot, session = fake
    session.fail[SendMessage] = TelegramNetworkError(method=None, message="timeout")
    client = client_for(store, settings, FakeBots({demo.id: bot}))
    body = {
        "lines": [pick(store, demo.id, "Эспрессо")],
        "method": "pickup",
        "phone": "+998901234567",
        "payment": "cash",
        "lang": "ru",
    }
    r = await client.post("/app/demo/orders", json=body, headers={"X-Telegram-Init-Data": init_data("1:demo", 100)})
    assert r.status_code == 200 and len(store.orders(demo.id, active_only=False)) == 1


async def test_one_broken_bot_does_not_silence_the_others(store, demo, other, settings):
    """A revoked token in one cafe used to stop every reminder and summary after it."""
    noon = datetime.now(TZ).replace(hour=12).astimezone(UTC)
    for cafe in (demo, other):
        store.join(cafe.id, store.new_invite(cafe.id), OWNER, "Owner")
        store.set_paid_until(cafe.id, store.cafe(cafe.id).now().date())
    broken, working = FakeSession(), FakeSession()
    broken.fail[SendMessage] = TelegramUnauthorizedError(method=None, message="Unauthorized")
    bots = {demo.id: Bot("1:demo", session=broken), other.id: Bot("2:other", session=working)}
    await Notifier(store, settings, bots.get, lambda _: "running").tick(noon)
    assert len(working.texts(OWNER)) == 1


async def test_a_stale_tap_does_not_overwrite_a_cancel(store, demo, fake):
    """The guest cancels while a cook taps "accept" on the same order: the cancel stands, couriers get nothing."""
    bot, session = fake
    store.update_cafe(demo.id, delivery_enabled=True)
    store.set_staff_chat(demo.id, -700, "courier")
    o = order(store, store.cafe(demo.id), 100, method="delivery", address="x")
    seen_by_cook = store.order(demo.id, o.id)  # the card the cook is looking at
    assert store.cancel_by_customer(demo.id, 100, o.id)
    assert not await change_status(bot, store, store.cafe(demo.id), seen_by_cook, "accepted")
    assert store.order(demo.id, o.id).status == "cancelled" and session.texts(-700) == []


async def test_paid_transfers_are_cancelled_by_the_cafe_with_a_refund_note(store, demo, fake):
    bot, session = fake
    store.update_cafe(demo.id, card_number="8600 1234 5678 9012", card_holder="A")
    store.join(demo.id, store.new_invite(demo.id), OWNER, "Owner")
    o = order(store, store.cafe(demo.id), 100, payment_method="transfer")
    assert store.claim_transfer(demo.id, 100, o.id)
    assert not store.cancel_by_customer(demo.id, 100, o.id)  # the guest said they paid: the cafe decides
    assert not store.order(demo.id, o.id).customer_can_cancel
    store.add_staff_message(o.id, OWNER, 1)
    assert await change_status(bot, store, store.cafe(demo.id), store.order(demo.id, o.id), "cancelled")
    assert any("верните деньги" in t for t in session.texts(OWNER))


def test_a_huge_order_still_fits_a_telegram_message(store, demo):
    long_name = "Очень длинное название позиции " * 4
    o = order(store, store.cafe(demo.id), 100)
    o.lines = [dict(o.lines[0], name=long_name, options=["большой", "двойной"]) for _ in range(50)]
    text, _ = staff_card(store.cafe(demo.id), o)
    assert len(text) < 4096 and "и ещё 30 поз." in text


def png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("1", (width, height)).save(buf, "PNG")
    return buf.getvalue()


def test_pictures_that_would_eat_the_server(tmp_path):
    bomb = png(13000, 13000)  # ~50 KB of PNG, gigabytes to decode
    assert len(bomb) < 200_000
    with pytest.raises(BadImage, match="слишком большая"):
        save_photo(bomb, tmp_path)
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), "red").save(buf, "JPEG")
    with pytest.raises(BadImage, match="JPG или PNG"):
        save_photo(buf.getvalue()[:2000], tmp_path)  # cut off half way
    with pytest.raises(BadImage):
        save_photo(b"\x00" * 100, tmp_path)
    assert list(tmp_path.iterdir()) == []


async def test_a_pack_with_a_bad_picture_creates_nothing(store, settings):
    bots = FakeBots()
    bots.tokens["5:new"] = ("new_bot", "New")
    client = client_for(store, settings, bots)
    await client.post(f"/ops/login/{store.create_login(OPS, OPERATOR)}")
    menu = {"name": "X", "logo": "logo.jpg", "menu": [{"category": "A", "items": [{"name": "B", "price": 1}]}]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x/x.json", json.dumps(menu))
        z.writestr("x/logo.jpg", b"HEIC, not a jpeg")
    r = await client.post(
        "/ops/new", data={"token": "5:new"}, files={"pack": ("x.zip", buf.getvalue(), "application/zip")}
    )
    assert r.status_code == 200 and "Картинка в архиве не подошла" in r.text
    assert store.cafe_by_token("5:new") is None  # nothing half-made: the same token can be tried again
    assert not any(settings.media_dir.glob("*.jpg")) if settings.media_dir.exists() else True


async def test_reports_survive_odd_dates(store, demo, settings, fake):
    bot, _ = fake
    store.join(demo.id, store.new_invite(demo.id), OWNER, "Owner")
    client = client_for(store, settings, FakeBots({demo.id: bot}))
    await client.post(f"/login/{store.create_login(demo.id, OWNER)}")
    for q in (
        "since=2023-01-01&until=2024-02-29",
        "since=0001-01-01&until=9999-12-31",
        "since=9999-12-31&until=0001-01-01",
    ):
        assert (await client.get(f"/reports?{q}")).status_code == 200
        assert (await client.get(f"/reports.csv?{q}")).status_code == 200


async def test_address_lookups_never_queue_up(monkeypatch):
    """Hundreds of pins at once: a few are looked up, the rest get no suggestion instead of waiting minutes."""
    started = 0

    async def slow(lat, lon, lang):
        nonlocal started
        started += 1
        await asyncio.sleep(0.05)
        return {"address": {"road": "улица"}}

    monkeypatch.setattr(geocode, "fetch", slow)
    monkeypatch.setattr(geocode, "SPACING", 0)
    geocode._cache.clear()
    results = await asyncio.gather(*(geocode.address(41 + i / 1000, 69.0) for i in range(100)))
    assert started <= geocode.MAX_WAITING and results.count(None) >= 100 - geocode.MAX_WAITING


async def test_a_greeting_of_spaces_is_refused(store, demo, settings, fake):
    bot, _ = fake
    store.join(demo.id, store.new_invite(demo.id), OWNER, "Owner")
    client = client_for(store, settings, FakeBots({demo.id: bot}))
    await client.post(f"/login/{store.create_login(demo.id, OWNER)}")
    await client.post("/settings", data={"name": "Demo", "greeting": "   ", "always_open": "1"})
    assert store.cafe(demo.id).greeting.strip()


async def test_order_spam_is_limited(store, demo, settings, fake):
    bot, _ = fake
    client = client_for(store, settings, FakeBots({demo.id: bot}))
    body = {
        "lines": [pick(store, demo.id, "Эспрессо")],
        "method": "pickup",
        "phone": "+998901234567",
        "payment": "cash",
        "lang": "ru",
    }
    headers = {"X-Telegram-Init-Data": init_data("1:demo", 100)}
    codes = [(await client.post("/app/demo/orders", json=body, headers=headers)).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 409, 409]  # three waiting for the kitchen is the most
    r = await client.post("/app/demo/orders", json=body, headers=headers)
    assert r.json() == {"error": "too_many"}
    other_guest = {"X-Telegram-Init-Data": init_data("1:demo", 101)}
    assert (await client.post("/app/demo/orders", json=body, headers=other_guest)).status_code == 200
