"""The chat side through aiogram with a fake Telegram (no network): greeting, order updates by how the order is
received, cancelling, "my orders", card transfers, staff buttons. Orders themselves come from the Mini App
(tests/test_miniapp.py); here they are placed in the store and announced the same way."""

import pytest
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Chat

from cafe.bot import build, send_to_customer, send_to_staff
from cafe.template import Template, import_cafe
from tests.conftest import FakeSession, message, order, pick, tap

CUSTOMER, OWNER, GROUP = 100, 7, -500


@pytest.fixture
def env(store, demo, settings, fake):
    bot, session = fake
    dp = build(store, demo.id, settings)
    store.join(demo.id, store.new_invite(demo.id), OWNER, "Owner")

    async def feed(update):
        await dp.feed_update(bot, update)

    async def place(**kw):
        """An order from the Mini App: saved, sent to staff and to the customer."""
        o = order(store, store.cafe(demo.id), CUSTOMER, *kw.pop("picks", ()), **kw)
        await send_to_staff(bot, store, store.cafe(demo.id), o)
        await send_to_customer(bot, store, store.cafe(demo.id), o)
        return o

    return feed, place, session


def buttons(m) -> list:
    return [b for row in m.reply_markup.inline_keyboard for b in row] if m.reply_markup else []


def alerts(session: FakeSession) -> list[str]:
    return [m.text for m in session.sent if isinstance(m, AnswerCallbackQuery) and m.text]


async def test_greeting_opens_the_mini_app(env, store, demo):
    feed, _, session = env
    await feed(message(CUSTOMER, "/start"))
    greeting = session.messages(CUSTOMER)[-1]
    assert greeting.text.startswith("Привет! Это Demo Coffee")
    menu = buttons(greeting)[0]
    assert (menu.text, menu.web_app.url, menu.style) == (
        "☕️ Открыть меню",
        "https://admin.example.uz/app/demo",
        "primary",
    )

    await feed(tap(CUSTOMER, "lang:uz"))
    assert [b.text for b in buttons(session.messages(CUSTOMER)[-1])][:2] == ["☕️ Menyuni ochish", "📋 Buyurtmalarim"]
    assert store.customer(demo.id, CUSTOMER).lang == "uz"
    await feed(message(CUSTOMER, "🛒 Корзина"))  # a leftover keyboard from the old bot, or just "hi"
    assert buttons(session.messages(CUSTOMER)[-1])[0].web_app

    await feed(message(200, "/start", lang="uz"))  # Telegram language decides until the customer picks one
    assert buttons(session.messages(200)[-1])[0].text == "☕️ Menyuni ochish"

    store.set_enabled(demo.id, False)  # e.g. the subscription is not paid
    await feed(message(CUSTOMER, "/start"))
    assert "bot orqali buyurtma qabul qilmayapti" in session.texts(CUSTOMER)[-1]
    assert not any(b.web_app for b in buttons(session.messages(CUSTOMER)[-1]))


async def test_delivery_steps_and_texts(env, store, demo):
    """Delivery has its own steps; the customer is never told «Ждём вас» (that was the pickup text)."""
    feed, place, session = env
    o = await place(picks=[pick(store, demo.id, "Чизкейк Нью-Йорк", qty=3)], method="delivery", address="Чиланзар 9")
    card = session.messages(OWNER)[-1]
    assert "Заказ №1 · Новый" in card.text and "🛵 Доставка: Чиланзар 9" in card.text
    assert [(b.text, b.style) for b in buttons(card)] == [("✅ Принять", "success"), ("✖️ Отменить", "danger")]
    before = len(session.texts(CUSTOMER))
    for status in ("accepted", "cooking", "courier", "delivered"):
        await feed(tap(OWNER, f"st:{o.id}:{status}"))
    told = session.texts(CUSTOMER)[before:]
    assert told == [
        "Заказ №1 принят ✅ Скоро начнём готовить.",
        "Заказ №1 готовится ☕️",
        "Заказ №1 передан курьеру 🛵 Скоро будет у вас.",
    ]  # nothing on «Доставлен»: the customer has it in hand
    assert "Заказ №1 · Доставлен" in session.texts(OWNER)[-1]
    assert store.order(demo.id, o.id).payment_status == "paid"
    await feed(tap(OWNER, f"st:{o.id}:ready"))  # a pickup step does not exist for delivery
    assert alerts(session)[-1] == "Статус уже изменён"


async def test_pickup_steps_in_the_customers_language(env, store, demo):
    feed, place, session = env
    o = await place(lang="uz")
    await feed(tap(OWNER, f"st:{o.id}:accepted"))
    await feed(tap(OWNER, f"st:{o.id}:cooking"))
    card = [m for m in session.messages(OWNER) if isinstance(m, EditMessageText)][-1]
    assert [b.text for b in buttons(card)] == ["🔔 Готов", "✖️ Отменить"]
    await feed(tap(OWNER, f"st:{o.id}:ready"))
    assert session.texts(CUSTOMER)[-1] == "№1 buyurtma tayyor! Sizni kutamiz: Ташкент, ул. Примерная, 1 🙌"
    await feed(tap(OWNER, f"st:{o.id}:done"))
    assert store.order(demo.id, o.id).status == "done"


async def test_customer_cancels_while_new(env, store, demo):
    feed, place, session = env
    o = await place()
    sent = session.messages(CUSTOMER)[-1]
    assert sent.parse_mode == "HTML" and "✅ Заказ №1 отправлен в «Demo Coffee»" in sent.text
    assert "Эспрессо × 1 — 18 000 сум" in sent.text and "<b>Итого: 18 000 сум</b>" in sent.text
    assert [b.callback_data for b in buttons(sent)] == [f"oc:{o.id}"]

    await feed(tap(CUSTOMER, f"oc:{o.id}"))
    assert store.order(demo.id, o.id).status == "cancelled" and alerts(session)[-1] == "Заказ №1 отменён"
    staff = session.messages(OWNER)
    assert "Заказ №1 · Отменён" in staff[-2].text and not staff[-2].reply_markup  # the card follows
    assert staff[-1].text == "❗️ Клиент отменил заказ №1" and staff[-1].reply_parameters  # a reply: staff get a ping
    assert "Статус: Отменён" in session.texts(CUSTOMER)[-1] and not buttons(session.messages(CUSTOMER)[-1])

    accepted = await place()
    await feed(tap(OWNER, f"st:{accepted.id}:accepted"))
    await feed(tap(CUSTOMER, f"oc:{accepted.id}"))  # the button is still on the customer's screen
    assert (
        alerts(session)[-1].startswith("Заказ уже готовят") and store.order(demo.id, accepted.id).status == "accepted"
    )


async def test_my_orders_show_the_last_five(env, store, demo):
    feed, place, session = env
    for _ in range(6):
        o = await place()
    await feed(tap(OWNER, f"st:{o.id}:accepted"))
    await feed(message(CUSTOMER, "/orders"))
    view = session.messages(CUSTOMER)[-1]
    assert view.text.startswith("📋 Ваши последние заказы")
    assert "<b>№6</b>" in view.text and "<b>№2</b>" in view.text and "<b>№1</b>" not in view.text
    assert "№6</b> · " in view.text and " · Принят\n" in view.text
    rows = view.reply_markup.inline_keyboard
    assert [b.text for b in rows[0]] == ["🔁 Повторить №6"]  # accepted: no cancel
    assert rows[0][0].web_app.url == f"https://admin.example.uz/app/demo?repeat={o.id}"
    assert [b.text for b in rows[1]] == ["🔁 Повторить №5", "✖️ Отменить №5"]

    await feed(tap(CUSTOMER, rows[1][1].callback_data))
    assert " · Отменён\n" in session.texts(CUSTOMER)[-1]  # the list is redrawn in place
    await feed(tap(200, "orders"))
    assert session.texts(200)[-1].startswith("Заказов пока нет")


async def test_card_transfer(env, store, demo):
    feed, place, session = env
    store.update_cafe(demo.id, card_number="8600 1234 5678 9012", card_holder="Ali V.")
    o = await place(payment_method="transfer")
    sent = session.messages(CUSTOMER)[-1]
    assert "Переведите 18 000 сум на карту <code>8600 1234 5678 9012</code> (Ali V.)" in sent.text
    copy, paid, cancel = buttons(sent)
    assert copy.copy_text.text == "8600 1234 5678 9012" and paid.callback_data == f"paid:{o.id}"
    assert cancel.callback_data == f"oc:{o.id}"
    assert "Перевод на карту · Не оплачен" in session.texts(OWNER)[-1]

    await feed(tap(CUSTOMER, f"paid:{o.id}"))
    assert store.order(demo.id, o.id).payment_status == "claimed"
    assert session.texts(OWNER)[-1] == "💳 Клиент сообщил о переводе по заказу №1 — проверьте"
    card = session.messages(OWNER)[-2]
    assert "Клиент сообщил о переводе" in card.text and "💳 Оплата получена" in [b.text for b in buttons(card)]
    await feed(tap(OWNER, f"pay:{o.id}"))
    assert store.order(demo.id, o.id).payment_status == "paid"
    assert session.texts(CUSTOMER)[-1] == "Оплата по заказу №1 получена ✅ Спасибо!"
    await feed(tap(OWNER, f"pay:{o.id}"))
    assert alerts(session)[-1] == "Оплата уже отмечена"


async def test_forged_staff_buttons_do_nothing(env, store, demo):
    """Telegram does not check that callback data came from a real button."""
    feed, place, _ = env
    o = await place(payment_method="cash")
    await feed(tap(CUSTOMER, f"st:{o.id}:accepted"))
    await feed(tap(CUSTOMER, f"pay:{o.id}"))
    assert store.order(demo.id, o.id).status == "new"
    store.set_staff_chat(demo.id, GROUP)
    await feed(tap(555, f"st:{o.id}:accepted", chat=Chat(id=GROUP, type="supergroup")))  # any barista in the group
    assert store.order(demo.id, o.id).status == "accepted"


async def test_owner_commands(env, store, demo):
    feed, place, session = env
    await feed(message(CUSTOMER, "/admin"))
    assert "login" not in session.texts(CUSTOMER)[-1]  # not an admin: just the greeting
    await feed(message(OWNER, "/admin"))
    link = session.texts(OWNER)[-1].split()[-1]
    assert link.startswith("https://admin.example.uz/login/")
    assert store.consume_login(link.rsplit("/", 1)[1])

    await feed(message(OWNER, "/staff", chat=Chat(id=GROUP, type="supergroup")))
    assert store.cafe(demo.id).staff_chat_id == GROUP
    await place()
    assert "Заказ №1 · Новый" in session.texts(GROUP)[-1]  # orders now go to the staff group
    await feed(message(OWNER, "/stats"))
    assert session.texts(OWNER)[-1] == "Сегодня: 1 заказов на 18 000 сум"
    await feed(message(CUSTOMER, "/privacy"))
    assert session.texts(CUSTOMER)[-1].startswith("Что мы храним")


async def test_two_cafes_one_process(store, demo, settings):
    other_id = import_cafe(
        store,
        "tea",
        "2:tea",
        Template(
            name="Tea House", greeting="Чай!", menu=[{"category": "Чай", "items": [{"name": "Зелёный", "price": 9000}]}]
        ),
    )
    store.join(other_id, store.new_invite(other_id), 9, "Tea owner")
    store.join(demo.id, store.new_invite(demo.id), OWNER, "Owner")
    a, b = FakeSession(), FakeSession()
    dp_a, dp_b = build(store, demo.id, settings), build(store, other_id, settings)
    bot_a, bot_b = Bot("1:demo", session=a), Bot("2:tea", session=b)

    await dp_b.feed_update(bot_b, message(CUSTOMER, "/start"))
    assert b.texts(CUSTOMER)[-1] == "Чай!"
    assert buttons(b.messages(CUSTOMER)[-1])[0].web_app.url.endswith("/app/tea")
    tea = store.cafe(other_id)
    o = order(store, tea, CUSTOMER, pick(store, other_id, "Зелёный"))
    await send_to_staff(bot_b, store, tea, o)
    assert "Зелёный × 1" in b.texts(9)[-1]  # the tea owner gets it through the tea bot
    assert a.sent == []

    # ids from another cafe do nothing in this bot, even for its admin
    await dp_a.feed_update(bot_a, tap(OWNER, f"st:{o.id}:accepted"))
    await dp_a.feed_update(bot_a, tap(CUSTOMER, f"oc:{o.id}"))
    assert store.order(other_id, o.id).status == "new"
    await dp_a.feed_update(bot_a, message(CUSTOMER, "/orders"))
    assert a.texts(CUSTOMER)[-1].startswith("Заказов пока нет")
    assert not [m for m in a.sent if isinstance(m, SendMessage) and m.chat_id == 9]


async def test_couriers_take_and_deliver(env, store, demo):
    feed, place, session = env
    couriers = Chat(id=-700, type="supergroup")
    await feed(message(OWNER, "/couriers", chat=couriers))
    assert store.cafe(demo.id).courier_chat_id == -700
    o = await place(picks=[pick(store, demo.id, "Чизкейк Нью-Йорк", qty=3)], method="delivery", address="Чиланзар 9")
    for status in ("accepted", "cooking", "courier"):
        await feed(tap(OWNER, f"st:{o.id}:{status}"))
    staff = [m for m in session.messages(OWNER) if isinstance(m, EditMessageText)][-1]
    assert "Ждём отметку курьера" in staff.text and not buttons(staff)  # the kitchen cannot mark it delivered
    await feed(tap(OWNER, f"st:{o.id}:delivered"))  # a stale or forged button
    assert store.order(demo.id, o.id).status == "courier"
    card = session.messages(-700)[-1]
    assert "📍 Чиланзар 9" in card.text and "Получить с клиента: 129 000 сум" in card.text
    assert [b.callback_data for b in buttons(card)] == [f"take:{o.id}"]

    await feed(tap(CUSTOMER, f"take:{o.id}"))  # forged from a private chat: nothing
    await feed(tap(301, f"dlv:{o.id}", chat=couriers))  # not taken yet
    assert store.order(demo.id, o.id).courier_id is None
    await feed(tap(301, f"take:{o.id}", chat=couriers))
    await feed(tap(302, f"take:{o.id}", chat=couriers))
    assert alerts(session)[-1] == "Заказ уже взял другой курьер"
    card = session.messages(-700)[-1]
    assert "Везёт: User301" in card.text and [b.callback_data for b in buttons(card)] == [f"dlv:{o.id}"]
    assert "🛵 Везёт: User301" in [m for m in session.messages(OWNER) if isinstance(m, EditMessageText)][-1].text
    await feed(tap(302, f"dlv:{o.id}", chat=couriers))  # only the one who took it
    assert store.order(demo.id, o.id).status == "courier"
    await feed(tap(301, f"dlv:{o.id}", chat=couriers))
    o = store.order(demo.id, o.id)
    assert (o.status, o.payment_status) == ("delivered", "paid")
    assert not buttons(session.messages(-700)[-1])

    await feed(message(OWNER, "/couriers"))  # in private: back to staff marking delivery
    assert store.cafe(demo.id).courier_chat_id is None


async def test_forgotten_orders_close_themselves(env, store, demo, fake):
    from datetime import UTC, datetime, timedelta

    from cafe.bot import close_stale

    _, place, _ = env
    bot, _ = fake
    old = await place()
    for status in ("accepted", "cooking", "ready"):
        store.set_status(demo.id, old.id, status)
    fresh = await place()
    store.db.execute(
        "update orders set created_at = ? where id = ?", ((datetime.now(UTC) - timedelta(hours=7)).isoformat(), old.id)
    )
    await close_stale(bot, store, demo, hours=6)
    assert store.order(demo.id, old.id).status == "done" and store.order(demo.id, fresh.id).status == "new"


async def test_no_https_no_menu_button_and_no_tech_text(store, demo, settings):
    settings.public_url = "http://localhost:8000"
    session = FakeSession()
    await build(store, demo.id, settings).feed_update(Bot("1:demo", session=session), message(CUSTOMER, "/start"))
    greeting = session.messages(CUSTOMER)[-1]
    assert greeting.text == demo.greeting and not any(b.web_app for b in buttons(greeting))
