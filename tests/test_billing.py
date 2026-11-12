"""Subscriptions end to end: reminders to owners in their bot (each once), the operator's service bot (joining, the
morning digest, alerts, /panel), recording payments in /ops and the owner's "payment received"."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from aiogram import Bot

from cafe.notify import DIGEST_HOUR, TZ, Notifier, build_ops
from cafe.store import OPERATOR, OPS
from cafe.web import create_app
from tests.conftest import FakeBots, FakeSession, message, order

OWNER, ME = 7, 42


@pytest.fixture
def world(store, demo, settings, fake):
    """The demo cafe's bot (owner joined) and the operator's service bot."""
    bot, session = fake
    store.join(demo.id, store.new_invite(demo.id), OWNER, "Owner")
    ops_session = FakeSession()
    ops_bot = Bot("9:ops", session=ops_session)
    state = {}  # cafe id → bot state other than "running"
    bots = {demo.id: bot, OPS: ops_bot}
    notifier = Notifier(store, settings, bots.get, lambda cafe_id: state.get(cafe_id, "running"))
    return notifier, session, ops_session, state


def days_from_today(store, cafe_id: int, n: int) -> None:
    store.set_paid_until(cafe_id, store.cafe(cafe_id).now().date() + timedelta(days=n))


async def test_owner_reminders_go_out_once_each(world, store, demo):
    notifier, session, _, _ = world
    days_from_today(store, demo.id, 10)
    await notifier.tick()
    assert session.texts(OWNER) == []  # nothing to say yet

    days_from_today(store, demo.id, 3)
    await notifier.tick()
    await notifier.tick()  # every 15 seconds — but once
    texts = session.texts(OWNER)
    assert (
        len(texts) == 1 and "Пробный период бота «Demo Coffee» заканчивается" in texts[0] and "через 3 дня" in texts[0]
    )
    assert "299 000 сум в месяц" in texts[0]

    days_from_today(store, demo.id, 0)
    await notifier.tick()
    assert "заканчивается сегодня" in session.texts(OWNER)[-1]
    days_from_today(store, demo.id, -1)
    await notifier.tick()
    assert "Бот ещё принимает заказы до" in session.texts(OWNER)[-1] and store.cafe(demo.id).active()
    days_from_today(store, demo.id, -3)
    await notifier.tick()
    assert "перестал принимать заказы" in session.texts(OWNER)[-1] and not store.cafe(demo.id).active()
    assert len(session.texts(OWNER)) == 4


async def test_reminders_wait_for_a_stopped_bot(store, demo, settings, fake):
    bot, session = fake
    store.join(demo.id, store.new_invite(demo.id), OWNER, "Owner")
    running = {}
    notifier = Notifier(store, settings, running.get, lambda _: "offline")
    days_from_today(store, demo.id, 2)
    await notifier.tick()
    running[demo.id] = bot  # back online: the reminder is still due
    await notifier.tick()
    assert len(session.texts(OWNER)) == 1


async def test_the_operator_gets_alerts_and_a_morning_digest(world, store, demo):
    notifier, _, ops_session, state = world
    store.set_meta("ops_invite", "code")
    store.add_operator("code", ME, "Me")
    order(store, demo, 100)  # placed today: counted in tomorrow's digest
    days_from_today(store, demo.id, -3)
    state[demo.id] = "rejected"
    early = datetime.now(TZ).replace(hour=DIGEST_HOUR - 1).astimezone(UTC)
    await notifier.tick(early)
    texts = ops_session.texts(ME)
    assert any("отключено за неуплату" in t for t in texts) and any("Telegram не принял токен" in t for t in texts)
    assert not any("Вчера" in t for t in texts)  # the digest waits for the morning

    tomorrow = datetime.now(TZ).replace(hour=DIGEST_HOUR, minute=5) + timedelta(days=1)
    await notifier.tick(tomorrow.astimezone(UTC))
    await notifier.tick(tomorrow.astimezone(UTC))
    digests = [t for t in ops_session.texts(ME) if t.startswith("limybot,")]
    assert len(digests) == 1
    assert "Вчера: 1 заказов" in digests[0] and "Отключены за неуплату" in digests[0] and "Demo Coffee" in digests[0]
    assert len(ops_session.texts(ME)) == 3  # the alerts were not repeated


async def test_service_bot(store, settings):
    session = FakeSession()
    bot = Bot("9:ops", session=session)
    dp = build_ops(store, settings, lambda _: "running")
    await dp.feed_update(bot, message(ME, "/panel"))
    assert "только по ссылке из панели" in session.texts(ME)[-1]  # a stranger gets nothing
    await dp.feed_update(bot, message(ME, "/start op-wrong"))
    assert "устарела" in session.texts(ME)[-1]
    store.set_meta("ops_invite", "code")
    await dp.feed_update(bot, message(ME, "/start op-code"))
    assert store.operators() == [(ME, "User42")] and "утренняя сводка" in session.texts(ME)[-1]
    await dp.feed_update(bot, message(ME, "/panel"))
    link = session.texts(ME)[-1].split("\n")[-1]
    assert link.startswith("https://admin.example.uz/ops/login/")
    assert store.consume_login(link.rsplit("/", 1)[1]) == (OPS, OPERATOR)
    await dp.feed_update(bot, message(ME, "/status"))
    assert session.texts(ME)[-1].startswith("limybot,")


@pytest.fixture
def panel(store, demo, settings, fake):
    bot, session = fake
    store.join(demo.id, store.new_invite(demo.id), OWNER, "Owner")
    bots = FakeBots({demo.id: bot})
    bots.tokens["5:new"] = ("new_cafe_bot", "New Cafe")
    bots.tokens["9:ops"] = ("limy_ops_bot", "limybot ops")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(store, settings, bots)), base_url="https://admin.example.uz"
    )
    return client, bots, session


async def ops_login(client, store):
    await client.get(f"/ops/login/{store.create_login(OPS, OPERATOR)}")


async def test_recording_payments(panel, store, demo):
    client, _, session = panel
    await ops_login(client, store)
    today = demo.now().date()
    days_from_today(store, demo.id, -1)  # in the grace days
    url = f"/ops/cafes/{demo.id}"
    assert "Не оплачено" in (await client.get(url)).text
    r = await client.post(f"{url}/payment", data={"amount": "299000", "months": "1", "note": "Click"})
    until = store.cafe(demo.id).paid_until
    assert until.month != today.month or until.year != today.year  # a month on from the old date
    assert r.headers["location"].startswith(f"{url}?msg=") and r.headers["location"].endswith("#subscription")
    assert "Оплата получена, спасибо" in session.texts(OWNER)[-1]
    page = (await client.get(url)).text
    assert "299 000 сум · 1 мес." in page and "Click" in page

    index = (await client.get("/ops")).text
    assert "Оплат в этом месяце" in index and "299 000 сум" in index

    payment = store.payments(demo.id)[0]
    await client.post(f"{url}/payments/{payment.id}/cancel")
    assert store.payments(demo.id) == [] and store.cafe(demo.id).paid_until == today - timedelta(days=1)

    await client.post(f"{url}/enabled", data={})  # paused for not paying…
    assert not store.cafe(demo.id).active()
    await client.post(f"{url}/payment", data={"amount": "299000"})
    assert store.cafe(demo.id).active()  # …and paying switches it back on


async def test_new_cafes_get_a_trial_or_no_limit(panel, store):
    client, _, _ = panel
    await ops_login(client, store)
    await client.post("/ops/new", data={"token": "5:new"})
    cafe = store.cafe_by_token("5:new")
    assert (cafe.paid_until - cafe.now().date()).days == 14
    assert "Пробный период до" in (await client.get(f"/ops/cafes/{cafe.id}")).text
    await client.post(f"/ops/cafes/{cafe.id}/delete", data={"confirm": cafe.slug})
    await client.post("/ops/new", data={"token": "5:new", "plan": "demo"})
    assert store.cafe_by_token("5:new").paid_until is None


async def test_operator_settings_and_the_service_bot(panel, store, demo):
    client, bots, _ = panel
    await ops_login(client, store)
    assert "служебный бот" in (await client.get("/ops")).text
    await client.post("/ops/settings", data={"support": "@limy_support", "price": "350000"})
    days_from_today(store, demo.id, 2)
    await client.get(f"/login/{store.create_login(demo.id, OWNER)}")
    banner = (await client.get("/orders")).text
    assert "350 000 сум в месяц" in banner and "@limy_support" in banner

    r = await client.post("/ops/settings/bot", data={"token": "1:demo"})
    assert "уже работает у заведения" in r.headers["location"] or "%D1%83%D0%B6%D0%B5" in r.headers["location"]
    await client.post("/ops/settings/bot", data={"token": "9:ops"})
    assert store.meta("ops_bot_token") == "9:ops" and bots.synced
    page = (await client.get("/ops/settings")).text
    code = store.meta("ops_invite")
    assert f"https://t.me/limy_ops_bot?start=op-{code}" in page
    store.add_operator(code, ME, "Me")
    assert "Me" in (await client.get("/ops/settings")).text
    await client.post(f"/ops/settings/operators/{ME}/remove")
    assert store.operators() == []
    await client.post("/ops/settings/bot", data={"token": ""})
    assert store.meta("ops_bot_token") is None
