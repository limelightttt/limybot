"""Whole customer → staff flows through aiogram with a fake Telegram (no network)."""

from io import BytesIO

import pytest
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageCaption, EditMessageText, SendPhoto
from aiogram.types import Chat, Contact, FSInputFile
from PIL import Image

from cafe.bot import build
from cafe.template import Template, import_cafe
from cafe.web import save_photo
from tests.conftest import FakeSession, item_id, message, tap

CUSTOMER, OWNER = 100, 7


@pytest.fixture
def env(store, demo, settings, fake):
    bot, session = fake
    dp = build(store, demo.id, settings)
    store.join(demo.id, store.new_invite(demo.id), OWNER, "Owner")

    async def feed(update):
        await dp.feed_update(bot, update)

    return feed, session


def button(session: FakeSession, label: str) -> str:
    """callback_data of the button whose text starts with label, in the latest keyboard."""
    markup = next(m.reply_markup for m in reversed(session.sent) if getattr(m, "reply_markup", None))
    return next(b.callback_data for row in markup.inline_keyboard for b in row if b.text.startswith(label))


def alerts(session: FakeSession) -> list[str]:
    return [m.text for m in session.sent if isinstance(m, AnswerCallbackQuery) and m.text]


async def test_order_with_modifiers_from_menu_to_ready(env, store, demo):
    feed, session = env
    await feed(message(CUSTOMER, "/start"))
    assert "Demo Coffee" in session.texts(CUSTOMER)[-1]
    await feed(message(CUSTOMER, "☕️ Меню"))
    await feed(tap(CUSTOMER, button(session, "Кофе")))
    await feed(tap(CUSTOMER, button(session, "Латте")))
    card = session.texts()[-1]
    assert "Размер: S" in card and "Молоко: обычное" in card and "Цена: 30 000 сум" in card
    await feed(tap(CUSTOMER, button(session, "M +4 000")))
    await feed(tap(CUSTOMER, button(session, "овсяное")))
    await feed(tap(CUSTOMER, button(session, "ваниль")))
    card = session.texts()[-1]
    assert "Размер: M" in card and "Сироп (можно несколько): ваниль" in card and "Цена: 44 000 сум" in card
    await feed(tap(CUSTOMER, button(session, "➕ В корзину")))
    await feed(tap(CUSTOMER, button(session, "➕ В корзину")))
    assert alerts(session)[-1] == "Латте (M, овсяное, ваниль) в корзине: 2 шт."

    await feed(message(CUSTOMER, "🛒 Корзина"))
    assert "Итого: 88 000 сум" in session.texts(CUSTOMER)[-1]
    await feed(tap(CUSTOMER, "checkout"))
    await feed(tap(CUSTOMER, "m:pickup"))
    await feed(message(CUSTOMER, contact=Contact(phone_number="+998901234567", first_name="A", user_id=CUSTOMER)))
    await feed(tap(CUSTOMER, "skip"))
    summary = session.texts(CUSTOMER)[-1]
    assert "Самовывоз" in summary and "+998901234567" in summary and "при получении" in summary
    await feed(tap(CUSTOMER, "confirm"))
    assert "Заказ №1 отправлен" in "".join(session.texts(CUSTOMER))
    staff = session.texts(OWNER)[-1]  # no staff group yet: admins get the order
    assert "Заказ №1 · Новый" in staff and "Латте (M, овсяное, ваниль) × 2 — 88 000 сум" in staff
    assert "При получении · Не оплачен" in staff

    order = store.orders(demo.id, active_only=True)[0]
    await feed(tap(OWNER, f"st:{order.id}:accepted"))
    await feed(tap(OWNER, f"st:{order.id}:ready"))
    assert session.texts(CUSTOMER)[-1].startswith("Заказ №1 готов!")
    card_edits = [m for m in session.sent if isinstance(m, EditMessageText) and m.chat_id == OWNER]
    assert "Заказ №1 · Готов" in card_edits[-1].text  # the staff card follows the status
    await feed(tap(OWNER, f"st:{order.id}:accepted"))  # stale button: ignored
    assert alerts(session)[-1] == "Статус уже изменён" and store.order(demo.id, order.id).status == "ready"


async def test_item_photo_is_uploaded_once(env, store, demo, settings):
    feed, session = env
    buf = BytesIO()
    Image.new("RGB", (2000, 1000), "brown").save(buf, "PNG")
    settings.media_dir.mkdir(parents=True)
    latte = item_id(store, demo.id, "Латте")
    store.set_photo(demo.id, latte, save_photo(buf.getvalue(), settings.media_dir))

    await feed(tap(CUSTOMER, f"it:{latte}"))  # from the text list: the list is replaced by a photo card
    sent = session.sent[-2]
    assert isinstance(sent, SendPhoto) and isinstance(sent.photo, FSInputFile) and "Латте" in sent.caption
    file_id = store.item(demo.id, latte).photo_file_id
    assert file_id
    await feed(tap(CUSTOMER, button(session, "M"), photo=True))  # option toggles only edit the caption
    assert isinstance(session.sent[-2], EditMessageCaption) and "Размер: M" in session.sent[-2].caption
    await feed(tap(CUSTOMER, f"it:{latte}"))
    assert session.sent[-2].photo == file_id  # Telegram already has the file
    await feed(tap(CUSTOMER, button(session, "← Кофе"), photo=True))
    assert session.texts()[-1].startswith("Кофе")  # back to a text list


async def test_delivery_needs_minimum_and_address(env, store, demo):
    feed, session = env
    store.add_to_cart(demo.id, CUSTOMER, item_id(store, demo.id, "Эспрессо"), [])
    await feed(tap(CUSTOMER, "checkout"))
    assert "Доставка от 50 000 сум" in str(session.sent[-2].reply_markup)
    for _ in range(2):
        store.add_to_cart(demo.id, CUSTOMER, item_id(store, demo.id, "Чизкейк Нью-Йорк"), [])
    await feed(tap(CUSTOMER, "checkout"))
    await feed(tap(CUSTOMER, "m:delivery"))
    await feed(message(CUSTOMER, "Чиланзар 9, кв 12"))
    await feed(message(CUSTOMER, "90 123 45 67"))
    await feed(message(CUSTOMER, "без сахара"))
    await feed(tap(CUSTOMER, "confirm"))
    o = store.orders(demo.id, active_only=True)[0]
    assert (o.method, o.address, o.comment, o.total) == (
        "delivery",
        "Чиланзар 9, кв 12",
        "без сахара",
        18000 + 76000 + 15000,
    )


async def test_closed_cafe_does_not_take_orders(env, store, demo):
    feed, session = env
    now = demo.now()
    store.update_cafe(
        demo.id,
        open_time=(now.replace(hour=(now.hour + 2) % 24)).time(),
        close_time=(now.replace(hour=(now.hour + 3) % 24)).time(),
    )
    store.add_to_cart(demo.id, CUSTOMER, item_id(store, demo.id, "Эспрессо"), [])
    await feed(tap(CUSTOMER, "checkout"))
    assert alerts(session)[-1].startswith("Сейчас закрыто")


async def test_owner_commands(env, store, demo):
    feed, session = env
    await feed(message(CUSTOMER, "/admin"))
    assert session.texts(CUSTOMER) == []  # not an admin: silence
    await feed(message(OWNER, "/admin"))
    link = session.texts(OWNER)[-1].split()[-1]
    assert link.startswith("https://admin.example.uz/login/")
    assert store.consume_login(link.rsplit("/", 1)[1])

    group = Chat(id=-500, type="supergroup")
    await feed(message(OWNER, "/staff", chat=group))
    assert store.cafe(demo.id).staff_chat_id == -500
    store.add_to_cart(demo.id, CUSTOMER, item_id(store, demo.id, "Эспрессо"), [])
    await feed(tap(CUSTOMER, "checkout"))
    await feed(tap(CUSTOMER, "m:pickup"))
    await feed(message(CUSTOMER, "+998901234567"))
    await feed(tap(CUSTOMER, "skip"))
    await feed(tap(CUSTOMER, "confirm"))
    assert "Заказ №1 · Новый" in session.texts(-500)[-1]  # orders now go to the staff group

    await feed(message(OWNER, "/stats"))
    assert session.texts(OWNER)[-1] == "Сегодня: 1 заказов на 18 000 сум"


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
    await dp_b.feed_update(bot_b, message(CUSTOMER, "☕️ Меню"))
    assert "Чай" in str(b.sent[-1].reply_markup) and "Кофе" not in str(b.sent[-1].reply_markup)
    tea = item_id(store, other_id, "Зелёный")
    await dp_a.feed_update(bot_a, tap(CUSTOMER, f"add:{tea}:"))  # another cafe's item id in this bot
    assert store.cart(demo.id, CUSTOMER) == [] and "Позиция изменилась" in alerts(a)[-1]

    await dp_b.feed_update(bot_b, tap(CUSTOMER, f"add:{tea}:"))
    for update in (
        tap(CUSTOMER, "checkout"),
        tap(CUSTOMER, "m:pickup"),
        message(CUSTOMER, "+998901234567"),
        tap(CUSTOMER, "skip"),
        tap(CUSTOMER, "confirm"),
    ):
        await dp_b.feed_update(bot_b, update)
    assert "Зелёный × 1" in b.texts(9)[-1]  # the tea owner gets it through the tea bot
    assert a.texts(OWNER) == [] and store.orders(demo.id, active_only=False) == []
