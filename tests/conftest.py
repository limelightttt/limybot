"""Shared fixtures: a database with the demo cafe, a fake Telegram that records every request, signed Mini App data."""

import hashlib
import hmac
import itertools
import json
import time
from datetime import datetime
from urllib.parse import urlencode

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageText, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from cafe.config import ROOT, Settings
from cafe.models import Cafe, Order
from cafe.store import Store
from cafe.template import import_cafe, load_template

DEMO = ROOT / "clients" / "demo_coffee.json"
ids = itertools.count(1)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent: list[TelegramMethod] = []

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        chat = Chat(id=getattr(method, "chat_id", None) or 0, type="private")
        if isinstance(method, SendMessage | EditMessageText):
            return Message(message_id=next(ids), date=datetime.now(), chat=chat, text=method.text)
        return True

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    async def close(self):
        pass

    def messages(self, chat: int | None = None) -> list[SendMessage | EditMessageText]:
        """Everything shown in a chat: new messages and edits."""
        return [
            m for m in self.sent if isinstance(m, SendMessage | EditMessageText) and (chat is None or m.chat_id == chat)
        ]

    def texts(self, chat: int | None = None) -> list[str]:
        return [m.text for m in self.messages(chat)]


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=tmp_path / "limybot.db", public_url="https://admin.example.uz")


@pytest.fixture
def store(settings):
    return Store(settings.db_path)


@pytest.fixture
def demo(store):
    """The demo cafe imported from clients/demo_coffee.json, always open."""
    cafe_id = import_cafe(store, "demo", "1:demo", load_template(DEMO))
    store.update_cafe(cafe_id, open_time=None, close_time=None)
    return store.cafe(cafe_id)


@pytest.fixture
def fake():
    session = FakeSession()
    return Bot("1:demo", session=session), session


def item_id(store: Store, cafe_id: int, name: str) -> int:
    return next(it.id for c in store.menu(cafe_id, only_available=False) for it in c.items if it.name == name)


def option_id(store: Store, cafe_id: int, name: str) -> int:
    return next(o.id for g in store.groups(cafe_id) for o in g.options if o.name == name)


def pick(store: Store, cafe_id: int, name: str, options: list[str] = (), qty: int = 1) -> dict:
    """A cart line as the Mini App sends it, by names."""
    return {
        "item_id": item_id(store, cafe_id, name),
        "option_ids": [option_id(store, cafe_id, o) for o in options],
        "qty": qty,
    }


def order(store: Store, cafe: Cafe, user_id: int, *picks: dict, **kw) -> Order:
    """An order placed straight in the store; picks default to one espresso."""
    picks = picks or (pick(store, cafe.id, "Эспрессо"),)
    lines = store.lines(cafe.id, [(p["item_id"], p["option_ids"], p["qty"]) for p in picks])
    assert None not in lines
    fields = {
        "phone": "+998901234567",
        "method": "pickup",
        "address": None,
        "comment": None,
        "payment_method": "cash",
        "lang": "ru",
    }
    return store.place(cafe, user_id, f"User{user_id}", lines, **(fields | kw))


def init_data(token: str, uid: int, lang: str = "ru", age: int = 0) -> str:
    """What Telegram passes to a Mini App, signed with the bot token like Telegram does."""
    fields = {
        "auth_date": str(int(time.time()) - age),
        "query_id": "q",
        "user": json.dumps({"id": uid, "first_name": f"User{uid}", "language_code": lang}),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def user(uid: int, lang: str = "ru") -> User:
    return User(id=uid, is_bot=False, first_name=f"User{uid}", language_code=lang)


def message(uid: int, text: str | None = None, chat: Chat | None = None, lang: str = "ru") -> Update:
    return Update(
        update_id=next(ids),
        message=Message(
            message_id=next(ids),
            date=datetime.now(),
            chat=chat or Chat(id=uid, type="private"),
            from_user=user(uid, lang),
            text=text,
        ),
    )


def tap(uid: int, data: str, chat: Chat | None = None) -> Update:
    """A button press under a message in the user's private chat, or in another chat (a staff group)."""
    msg = Message(message_id=next(ids), date=datetime.now(), chat=chat or Chat(id=uid, type="private"), text="…")
    return Update(
        update_id=next(ids),
        callback_query=CallbackQuery(id=str(next(ids)), from_user=user(uid), chat_instance="c", message=msg, data=data),
    )
