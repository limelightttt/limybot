"""Shared fixtures: a database with the demo cafe and a fake Telegram that records every request."""

import itertools
from datetime import datetime

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageCaption, EditMessageText, SendMessage, SendPhoto, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User

from cafe.config import ROOT, Settings
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
        if isinstance(method, SendPhoto):
            photo = [PhotoSize(file_id=f"file-{next(ids)}", file_unique_id="u", width=1, height=1)]
            return Message(message_id=next(ids), date=datetime.now(), chat=chat, photo=photo, caption=method.caption)
        if isinstance(method, SendMessage | EditMessageText):
            return Message(message_id=next(ids), date=datetime.now(), chat=chat, text=method.text)
        return True

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    async def close(self):
        pass

    def texts(self, chat: int | None = None) -> list[str]:
        """Everything shown to a chat: messages, edits and photo captions."""
        return [
            m.text if isinstance(m, SendMessage | EditMessageText) else m.caption
            for m in self.sent
            if isinstance(m, SendMessage | EditMessageText | SendPhoto | EditMessageCaption)
            and (chat is None or m.chat_id == chat)
        ]


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


def user(uid: int) -> User:
    return User(id=uid, is_bot=False, first_name=f"User{uid}")


def message(uid: int, text: str | None = None, contact=None, chat: Chat | None = None) -> Update:
    return Update(
        update_id=next(ids),
        message=Message(
            message_id=next(ids),
            date=datetime.now(),
            chat=chat or Chat(id=uid, type="private"),
            from_user=user(uid),
            text=text,
            contact=contact,
        ),
    )


def tap(uid: int, data: str, photo: bool = False) -> Update:
    """A button press under a text message, or under a photo card."""
    chat = Chat(id=uid, type="private")
    pic = [PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)] if photo else None
    msg = Message(message_id=next(ids), date=datetime.now(), chat=chat, text=None if photo else "…", photo=pic)
    return Update(
        update_id=next(ids),
        callback_query=CallbackQuery(id=str(next(ids)), from_user=user(uid), chat_instance="c", message=msg, data=data),
    )
