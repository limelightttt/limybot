"""The chat side through aiogram with a fake Telegram (no network): greeting, order updates by how the order is
received, cancelling, "my orders", card transfers, staff buttons. Orders themselves come from the Mini App
(tests/test_miniapp.py); here they are placed in the store and announced the same way."""

from datetime import datetime

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Chat, ChatMemberLeft, ChatMemberMember, ChatMemberUpdated, Message, Update, User

from cafe.bot import build, send_to_customer, send_to_staff
from cafe.template import Template, import_cafe
from tests.conftest import FakeSession, ids, message, order, pick, tap, user

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


def new_messages(session: FakeSession, chat: int) -> list[str]:
    """Messages that arrived (with a notification), not edits of earlier ones."""
    return [m.text for m in session.sent if isinstance(m, SendMessage) and m.chat_id == chat]


def alerts(session: FakeSession) -> list[str]:
    return [m.text for m in session.sent if isinstance(m, AnswerCallbackQuery) and m.text]


async def test_greeting_opens_the_mini_app(env, store, demo):
    feed, _, session = env
    await feed(message(CUSTOMER, "/start"))
    greeting = session.messages(CUSTOMER)[-1]
    assert greeting.text.startswith("Привет! Это Demo Coffee")
    menu = buttons(greeting)[0]
    assert (menu.text, menu.web_app.url, menu.style) == (
        "🍽 Открыть меню",
        "https://admin.example.uz/app/demo",
        "primary",
    )

    await feed(tap(CUSTOMER, "lang:uz"))
    assert [b.text for b in buttons(session.messages(CUSTOMER)[-1])][:2] == ["🍽 Menyuni ochish", "📋 Buyurtmalarim"]
    assert store.customer(demo.id, CUSTOMER).lang == "uz"
    await feed(message(CUSTOMER, "🛒 Корзина"))  # a leftover keyboard from the old bot, or just "hi"
    assert buttons(session.messages(CUSTOMER)[-1])[0].web_app

    await feed(message(200, "/start", lang="uz"))  # Telegram language decides until the customer picks one
    assert buttons(session.messages(200)[-1])[0].text == "🍽 Menyuni ochish"

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
    before = len(new_messages(session, CUSTOMER))
    for status in ("accepted", "cooking", "courier", "delivered"):
        await feed(tap(OWNER, f"st:{o.id}:{status}"))
    told = new_messages(session, CUSTOMER)[before:]  # the order message itself is edited in place, see below
    assert told == [
        "Заказ №1 принят ✅ Скоро начнём готовить.",
        "Заказ №1 готовится 👨‍🍳",
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
    assert staff[-1].text == "❗️ Гость отменил заказ №1 — не готовьте его" and staff[-1].reply_parameters  # a ping
    assert "Статус: Отменён" in session.texts(CUSTOMER)[-1] and not buttons(session.messages(CUSTOMER)[-1])

    accepted = await place()  # accepted, not cooking yet: the guest may still change their mind
    await feed(tap(OWNER, f"st:{accepted.id}:accepted"))
    edited = session.messages(CUSTOMER)[-2]  # the order message follows the status, "Cancel" is still there
    assert isinstance(edited, EditMessageText) and "Статус: Принят" in edited.text
    assert [b.callback_data for b in buttons(edited)] == [f"oc:{accepted.id}"]
    await feed(tap(CUSTOMER, f"oc:{accepted.id}"))
    assert store.order(demo.id, accepted.id).status == "cancelled"

    cooking = await place()
    await feed(tap(OWNER, f"st:{cooking.id}:accepted"))
    await feed(tap(OWNER, f"st:{cooking.id}:cooking"))
    edited = [m for m in session.messages(CUSTOMER) if isinstance(m, EditMessageText)][-1]
    assert "Статус: Готовится" in edited.text and not buttons(edited)  # the button is gone once cooking starts
    await feed(tap(CUSTOMER, f"oc:{cooking.id}"))  # an old screen, or a forged tap
    assert alerts(session)[-1].startswith("Заказ уже готовят") and store.order(demo.id, cooking.id).status == "cooking"


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
    assert [b.text for b in rows[0]] == ["🔁 Повторить №6", "✖️ Отменить №6"]  # accepted: still cancellable
    assert rows[0][0].web_app.url == f"https://admin.example.uz/app/demo?repeat={o.id}"
    assert [b.text for b in rows[1]] == ["🔁 Повторить №5", "✖️ Отменить №5"]

    await feed(tap(CUSTOMER, rows[1][1].callback_data))
    assert " · Отменён\n" in session.texts(CUSTOMER)[-2]  # the list is redrawn in place…
    assert "Статус: Отменён" in session.texts(CUSTOMER)[-1]  # …and so is that order's own message
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
    assert session.texts(OWNER)[-1] == "💳 Гость сообщил о переводе 18 000 сум по заказу №1 — проверьте поступление"
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
    assert "для администраторов" in session.texts(CUSTOMER)[-1]  # not an admin: told why, no link
    await feed(message(OWNER, "/admin"))
    sent = session.messages(OWNER)[-1]
    link = sent.reply_markup.inline_keyboard[0][0].url  # a button: Telegram does not preview it
    assert link.startswith("https://admin.example.uz/login/") and link not in sent.text
    assert sent.link_preview_options.is_disabled
    await feed(message(OWNER, "/admin", chat=Chat(id=GROUP, type="supergroup")))
    assert "в личные сообщения" in session.texts(GROUP)[-1]  # no login links in front of the whole group
    assert store.consume_login(link.rsplit("/", 1)[1]) == (demo.id, OWNER)

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


BOT = User(id=1, is_bot=True, first_name="Demo bot", username="demo_bot")


def membership(by: int, joined: bool, chat_id: int = GROUP) -> Update:
    """The bot added to (or removed from) a group by a user."""
    was, now = (ChatMemberLeft(user=BOT), ChatMemberMember(user=BOT))
    return Update(
        update_id=next(ids),
        my_chat_member=ChatMemberUpdated(
            chat=Chat(id=chat_id, type="supergroup", title="Кухня"),
            from_user=user(by),
            date=datetime.now(),
            old_chat_member=was if joined else now,
            new_chat_member=now if joined else was,
        ),
    )


async def test_adding_the_bot_to_a_group_makes_it_the_staff_group(env, store, demo):
    feed, place, session = env
    await feed(membership(CUSTOMER, joined=True, chat_id=-600))  # someone else's group
    assert store.cafe(demo.id).staff_chat_id is None and "/staff" in session.texts(-600)[-1]
    await feed(membership(OWNER, joined=True))
    assert store.cafe(demo.id).staff_chat_id == GROUP and "сюда приходят все заказы" in session.texts(GROUP)[-1]
    await place()
    assert "Заказ №1 · Новый" in session.texts(GROUP)[-1]

    upgraded = message(OWNER, chat=Chat(id=GROUP, type="group"))
    upgraded.message.__dict__["migrate_to_chat_id"] = -100500  # the group became a supergroup with a new id
    await feed(upgraded)
    assert store.cafe(demo.id).staff_chat_id == -100500

    await feed(membership(OWNER, joined=False, chat_id=-100500))
    assert store.cafe(demo.id).staff_chat_id is None
    assert "личные сообщения" in session.texts(OWNER)[-1]  # the owner knows where orders go now
    await place()
    assert "Заказ №2 · Новый" in session.texts(OWNER)[-1]


async def test_commands_never_fall_silent(env, store, demo):
    feed, _, session = env
    group = Chat(id=GROUP, type="supergroup")
    await feed(message(CUSTOMER, "/staff", chat=group))
    assert "для администраторов «Demo Coffee»" in session.texts(GROUP)[-1]
    assert store.cafe(demo.id).staff_chat_id is None
    anonymous = Update(
        update_id=next(ids),
        message=Message(
            message_id=next(ids),
            date=datetime.now(),
            chat=group,
            from_user=User(id=1087968824, is_bot=True, first_name="Group", username="GroupAnonymousBot"),
            sender_chat=group,
            text="/staff",
        ),
    )
    await feed(anonymous)
    assert "анонимно" in session.texts(GROUP)[-1]
    await feed(message(OWNER, "/staff", chat=group))
    assert store.cafe(demo.id).staff_chat_id == GROUP
    await feed(message(OWNER, "/staff"))  # in private: orders back to the admins
    assert store.cafe(demo.id).staff_chat_id is None


async def test_a_tap_from_before_a_restart_is_ignored(env):
    feed, _, session = env
    session.fail[AnswerCallbackQuery] = TelegramBadRequest(
        method=AnswerCallbackQuery(callback_query_id="x"),
        message="Bad Request: query is too old and response timeout expired or query ID is invalid",
    )
    await feed(tap(CUSTOMER, "orders"))  # must not raise: there is nothing to answer any more


async def test_groups_for_couriers_and_finance(env, store, demo):
    """Kitchen, couriers and money in separate groups: each sees its part and presses only its buttons."""
    feed, place, session = env
    store.update_cafe(demo.id, card_number="8600 1234 5678 9012", card_holder="Ali")
    kitchen, couriers, money = GROUP, -700, -800
    await feed(membership(OWNER, joined=True))  # the first group: the kitchen, no questions
    assert store.cafe(demo.id).staff_chat_id == kitchen
    await feed(membership(OWNER, joined=True, chat_id=couriers))  # the next one: which is it?
    ask = session.messages(couriers)[-1]
    assert "Для чего эта группа" in ask.text and [b.callback_data for b in buttons(ask)] == [
        "role:staff",
        "role:courier",
        "role:finance",
    ]
    await feed(tap(CUSTOMER, "role:courier", chat=Chat(id=couriers, type="supergroup")))
    assert alerts(session)[-1].startswith("Выбрать может только администратор")
    await feed(tap(OWNER, "role:courier", chat=Chat(id=couriers, type="supergroup")))
    assert store.cafe(demo.id).courier_chat_id == couriers and "группа курьеров" in session.texts(couriers)[-1]
    await feed(message(OWNER, "/finance", chat=Chat(id=money, type="supergroup")))
    assert store.cafe(demo.id).finance_chat_id == money

    o = await place(method="delivery", address="Чиланзар 9", payment_method="transfer")
    assert "Заказ №1 · Новый" in session.texts(kitchen)[-1]
    finance_card = session.messages(money)[-1]
    assert finance_card.text.startswith("💳 Заказ №1") and [b.text for b in buttons(finance_card)] == [
        "💳 Оплата получена"
    ]
    assert "Оплата получена" not in str(buttons(session.messages(kitchen)[-1]))  # money is checked by finance
    assert len(new_messages(session, couriers)) == 1  # only the question: nothing for couriers until it is accepted

    await feed(tap(CUSTOMER, f"st:{o.id}:accepted", chat=Chat(id=couriers, type="supergroup")))
    assert store.order(demo.id, o.id).status == "new"  # a courier cannot accept for the kitchen
    await feed(tap(100500, f"st:{o.id}:accepted", chat=Chat(id=kitchen, type="supergroup")))
    courier_card = session.messages(couriers)[-1]
    assert courier_card.text.startswith("🛵 Доставка №1") and "Чиланзар 9" in courier_card.text
    assert "ещё не подтверждён" in courier_card.text and buttons(courier_card) == []  # nothing to press yet

    await feed(tap(CUSTOMER, f"paid:{o.id}"))  # the guest says they paid: finance is pinged, not the kitchen
    assert "проверьте поступление" in session.texts(money)[-1]
    assert not any("проверьте поступление" in t for t in session.texts(kitchen))
    await feed(tap(100501, f"pay:{o.id}", chat=Chat(id=kitchen, type="supergroup")))
    assert store.order(demo.id, o.id).payment_status == "claimed"  # the kitchen cannot confirm money
    await feed(tap(100501, f"pay:{o.id}", chat=Chat(id=money, type="supergroup")))
    assert store.order(demo.id, o.id).payment_status == "paid"

    await feed(tap(100500, f"st:{o.id}:cooking", chat=Chat(id=kitchen, type="supergroup")))
    courier_card = [m for m in session.messages(couriers) if isinstance(m, EditMessageText)][-1]
    assert "с гостя ничего не брать" in courier_card.text
    assert [b.text for b in buttons(courier_card)] == ["🛵 Передан курьеру"]
    await feed(tap(100502, f"st:{o.id}:courier", chat=Chat(id=couriers, type="supergroup")))
    await feed(tap(100502, f"st:{o.id}:delivered", chat=Chat(id=couriers, type="supergroup")))
    assert store.order(demo.id, o.id).status == "delivered"

    await feed(membership(OWNER, joined=False, chat_id=couriers))
    assert store.cafe(demo.id).courier_chat_id is None and "группы курьеров больше нет" in session.texts(OWNER)[-1]
    await feed(message(OWNER, "/finance"))  # in private: that group is switched off
    assert store.cafe(demo.id).finance_chat_id is None and "переводы подтверждают на кухне" in session.texts(OWNER)[-1]
