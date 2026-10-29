"""The whole customer → staff flow through aiogram with a fake Telegram session (no network)."""

import itertools
from datetime import datetime

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Contact, Message, Update, User

from cafe.bot import build
from cafe.config import Settings, load_client
from cafe.store import Store

CUSTOMER, OWNER = 100, 7
ids = itertools.count(1)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent: list[TelegramMethod] = []

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        if isinstance(method, SendMessage | EditMessageText):
            chat = Chat(id=method.chat_id or CUSTOMER, type="private")
            return Message(message_id=next(ids), date=datetime.now(), chat=chat, text=method.text)
        return True

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    async def close(self):
        pass


@pytest.fixture
def env(tmp_path):
    session = FakeSession()
    bot = Bot("1:test", session=session)
    store = Store(tmp_path / "cafe.db", load_client("demo_coffee"))
    dp = build(Settings(bot_token="1:test", admin_ids=str(OWNER)), store)
    return dp, bot, session, store


def _user(uid):
    return User(id=uid, is_bot=False, first_name=f"User{uid}")


def message(uid, text=None, contact=None):
    chat = Chat(id=uid, type="private")
    return Update(
        update_id=next(ids),
        message=Message(
            message_id=next(ids), date=datetime.now(), chat=chat, from_user=_user(uid), text=text, contact=contact
        ),
    )


def tap(uid, data):
    chat = Chat(id=uid, type="private")
    msg = Message(message_id=next(ids), date=datetime.now(), chat=chat, text="…")
    return Update(
        update_id=next(ids),
        callback_query=CallbackQuery(
            id=str(next(ids)), from_user=_user(uid), chat_instance="c", message=msg, data=data
        ),
    )


def texts(session, chat=None):
    return [
        m.text
        for m in session.sent
        if isinstance(m, SendMessage | EditMessageText) and (chat is None or m.chat_id == chat)
    ]


async def test_order_from_menu_to_ready(env):
    dp, bot, session, store = env
    feed = lambda u: dp.feed_update(bot, u)
    await feed(message(CUSTOMER, "/start"))
    assert "Demo Coffee" in texts(session)[-1]
    await feed(message(CUSTOMER, "☕️ Меню"))
    await feed(tap(CUSTOMER, "cat:0"))
    await feed(tap(CUSTOMER, "add:latte"))
    await feed(tap(CUSTOMER, "add:latte"))
    alerts = [m.text for m in session.sent if isinstance(m, AnswerCallbackQuery)]
    assert alerts[-1] == "Латте в корзине: 2 шт."
    await feed(message(CUSTOMER, "🛒 Корзина"))
    assert "Итого: 60 000 сум" in texts(session)[-1]
    await feed(tap(CUSTOMER, "checkout"))
    await feed(tap(CUSTOMER, "m:pickup"))
    await feed(message(CUSTOMER, contact=Contact(phone_number="+998901234567", first_name="A", user_id=CUSTOMER)))
    await feed(tap(CUSTOMER, "skip"))
    assert "Самовывоз" in texts(session)[-1] and "+998901234567" in texts(session)[-1]
    await feed(tap(CUSTOMER, "confirm"))
    assert "Заказ №1 отправлен" in "".join(texts(session, CUSTOMER))
    staff = texts(session, OWNER)[-1]  # no staff group yet: the owner gets the order
    assert "Заказ №1 · Новый" in staff and "Латте × 2" in staff and "Самовывоз" in staff

    await feed(tap(OWNER, "st:1:accepted"))
    await feed(tap(OWNER, "st:1:ready"))
    assert texts(session, CUSTOMER)[-1].startswith("Заказ №1 готов!")
    await feed(tap(OWNER, "st:1:accepted"))  # stale button: ignored
    assert store.order(1).status == "ready"


async def test_delivery_needs_minimum_and_address(env):
    dp, bot, session, store = env
    store.add(CUSTOMER, "espresso")
    await dp.feed_update(bot, tap(CUSTOMER, "checkout"))
    assert "Доставка от 50 000 сум" in str(session.sent[-1].reply_markup)
    store.add(CUSTOMER, "cheesecake", 2)
    await dp.feed_update(bot, tap(CUSTOMER, "checkout"))
    await dp.feed_update(bot, tap(CUSTOMER, "m:delivery"))
    await dp.feed_update(bot, message(CUSTOMER, "Чиланзар 9, кв 12"))
    await dp.feed_update(bot, message(CUSTOMER, "90 123 45 67"))
    await dp.feed_update(bot, message(CUSTOMER, "без сахара"))
    await dp.feed_update(bot, tap(CUSTOMER, "confirm"))
    o = store.order(1)
    assert (o.method, o.address, o.comment, o.total) == (
        "delivery",
        "Чиланзар 9, кв 12",
        "без сахара",
        18000 + 76000 + 15000,
    )


async def test_only_admins_edit_the_menu(env):
    dp, bot, session, store = env
    await dp.feed_update(bot, message(CUSTOMER, "/price latte 1"))
    assert store.item("latte").price == 30000
    await dp.feed_update(bot, message(OWNER, "/price latte 32000"))
    await dp.feed_update(bot, message(OWNER, "/off croissant"))
    assert store.item("latte").price == 32000 and store.item("croissant") is None
    await dp.feed_update(bot, message(OWNER, "/admin"))
    assert "croissant — Круассан, 20 000 сум ⛔️ стоп" in texts(session, OWNER)[-1]
