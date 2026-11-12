"""News to guests, opting out, ratings after the order is in the guest's hands."""

import asyncio

import httpx
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from cafe import news
from cafe.bot import build, change_status
from cafe.reports import build as report
from cafe.web import create_app
from tests.conftest import FakeBots, message, order, tap

OWNER = 7


async def test_news_reach_guests_and_respect_opting_out(store, demo, settings, fake, monkeypatch):
    bot, session = fake
    monkeypatch.setattr(news, "PACE", 0)
    store.join(demo.id, store.new_invite(demo.id), OWNER, "Owner")
    dp = build(store, demo.id, settings)
    for uid in (100, 101, 102):
        await dp.feed_update(bot, message(uid, "/start"))  # opening the bot is enough to hear the news
    await dp.feed_update(bot, tap(101, "news:off"))
    assert "не будем присылать" in session.texts(101)[-1]
    assert store.audience(demo.id) == [100, 102]

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(store, settings, FakeBots({demo.id: bot}))),
        base_url="https://admin.example.uz",
    )
    await client.post(f"/login/{store.create_login(demo.id, OWNER)}")
    page = (await client.get("/promo")).text
    assert "Рассылка гостям" in page and "Не присылать новости" in page
    r = await client.post("/promo/news", data={"text": "  Новый бургер «Чили-чиз» −20% до воскресенья  "})
    assert r.headers["location"].startswith("/promo?msg=")
    await asyncio.gather(*news.running)
    got = [m for m in session.sent if isinstance(m, SendMessage) and m.text.startswith("Новый бургер")]
    assert sorted(m.chat_id for m in got) == [100, 102]  # 101 opted out
    assert got[0].reply_markup.inline_keyboard[0][0].callback_data == "news:off"
    first = store.broadcasts(demo.id)[0]
    assert (first["status"], first["sent"], first["audience"]) == ("done", 2, 2)
    assert first["text"] == "Новый бургер «Чили-чиз» −20% до воскресенья"

    await client.post("/promo/news", data={"text": "Второе"})
    await asyncio.gather(*news.running)
    r = await client.post("/promo/news", data={"text": "Третье"})
    assert "msg=" in r.headers["location"] and len(store.broadcasts(demo.id)) == 2  # two a day at most

    await dp.feed_update(bot, message(101, "/news"))
    assert 101 in store.audience(demo.id)


async def test_guests_who_blocked_the_bot_are_not_retried(store, demo, fake, monkeypatch):
    bot, session = fake
    monkeypatch.setattr(news, "PACE", 0)
    for uid in (100, 101):
        store.touch_customer(demo.id, uid, "ru")
    session.fail[SendMessage] = TelegramForbiddenError(method=None, message="bot was blocked by the user")
    news.start(bot, store, demo.id, "Акция")
    await asyncio.gather(*news.running)
    b = store.broadcasts(demo.id)[0]
    assert (b["sent"], b["failed"], b["status"]) == (0, 2, "done") and store.audience(demo.id) == []


async def test_rating_after_hand_over(store, demo, settings, fake):
    bot, session = fake
    store.join(demo.id, store.new_invite(demo.id), OWNER, "Owner")
    dp = build(store, demo.id, settings)
    cafe = store.cafe(demo.id)
    o = order(store, cafe, 100)
    await dp.feed_update(bot, tap(100, f"rate:{o.id}:5"))
    assert store.order(demo.id, o.id).rating is None  # not in the guest's hands yet
    for status in ("accepted", "cooking", "ready", "done"):
        await change_status(bot, store, cafe, store.order(demo.id, o.id), status)
    ask = session.messages(100)[-1]
    assert ask.text.startswith("Как вам заказ №1") and len(ask.reply_markup.inline_keyboard[0]) == 5
    await dp.feed_update(bot, tap(200, f"rate:{o.id}:1"))  # somebody else's order
    assert store.order(demo.id, o.id).rating is None
    await dp.feed_update(bot, tap(100, f"rate:{o.id}:2"))
    assert store.order(demo.id, o.id).rating == 2
    assert "оценка заказа №1" in session.texts(OWNER)[-1]  # a low one reaches the owner today
    await dp.feed_update(bot, tap(100, f"rate:{o.id}:5"))
    assert store.order(demo.id, o.id).rating == 2  # once
    today = cafe.now().date()
    assert report(store, cafe, today, today).rating == 2.0
