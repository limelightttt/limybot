"""Telegram side of one cafe: menu → item card → cart → checkout for customers, order cards for staff,
/admin login links for owners. Every cafe runs its own Dispatcher built here; the cafe is re-read from the
database on every update, so edits made in the web admin apply immediately."""

import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .config import Settings
from .models import NEXT, PAYMENT_METHODS, PAYMENT_STATUSES, STATUSES, Cafe, Item, Order
from .store import Store

log = logging.getLogger(__name__)

MENU, CART = "☕️ Меню", "🛒 Корзина"
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=MENU), KeyboardButton(text=CART)]],
    resize_keyboard=True,
)
BUTTON = {
    "accepted": "✅ Принять",
    "ready": "🔔 Готов",
    "done": "📦 Выдан",
    "cancelled": "✖️ Отменить",
}
TO_CUSTOMER = {
    "accepted": "Заказ №{number} принят и готовится ☕️",
    "ready": "Заказ №{number} готов! {where}",
    "cancelled": "Заказ №{number} отменён. Если это ошибка — напишите нам.",
}


class Checkout(StatesGroup):
    method = State()
    address = State()
    phone = State()
    comment = State()
    confirm = State()


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


# --- orders: shared with the web admin ---
def staff_card(cafe: Cafe, o: Order) -> tuple[str, InlineKeyboardMarkup | None]:
    where = f"🚗 Доставка: {o.address}" if o.method == "delivery" else "🏃 Самовывоз"
    fee = f"\nДоставка: {cafe.money(o.delivery_fee)}" if o.delivery_fee else ""
    payment = f"{PAYMENT_METHODS[o.payment_method]} · {PAYMENT_STATUSES[o.payment_status]}"
    text = (
        f"Заказ №{o.number} · {STATUSES[o.status]}\n\n{o.lines_text(cafe)}{fee}\nИтого: {cafe.money(o.total)}\n"
        f"💳 {payment}\n\n{where}\n👤 {o.customer}, {o.phone}" + (f"\n💬 {o.comment}" if o.comment else "")
    )
    buttons = [(BUTTON[s], f"st:{o.id}:{s}") for s in NEXT.get(o.status, [])]
    return text, kb([buttons]) if buttons else None


async def send_to_staff(bot: Bot, store: Store, cafe: Cafe, o: Order) -> None:
    """To the staff group; until it is set up (/staff), to every admin privately."""
    text, markup = staff_card(cafe, o)
    chats = [cafe.staff_chat_id] if cafe.staff_chat_id else [uid for uid, _ in store.admins(cafe.id)]
    for chat in chats:
        try:
            msg = await bot.send_message(chat, text, reply_markup=markup)
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            log.warning("%s: cannot deliver order %s to chat %s: %s", cafe.slug, o.number, chat, e)
            continue
        store.add_staff_message(o.id, chat, msg.message_id)


async def change_status(bot: Bot, store: Store, cafe: Cafe, o: Order, new: str) -> bool:
    """Move an order forward, refresh its staff cards and tell the customer. False for stale requests."""
    if new not in NEXT.get(o.status, []):
        return False
    store.set_status(cafe.id, o.id, new)
    o = store.order(cafe.id, o.id)
    text, markup = staff_card(cafe, o)
    for chat_id, message_id in store.staff_messages(o.id):
        with suppress(TelegramBadRequest):  # card deleted in the chat
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
    if new in TO_CUSTOMER:
        where = "Ждём вас 🙌" if o.method == "pickup" else "Курьер уже в пути 🚗"
        with suppress(TelegramForbiddenError):  # the customer blocked the bot
            await bot.send_message(o.user_id, TO_CUSTOMER[new].format(number=o.number, where=where))
    return True


def parse_ids(raw: str) -> list[int]:
    return [int(x) for x in raw.split(".") if x.isdigit()]


def build(store: Store, cafe_id: int, settings: Settings) -> Dispatcher:
    r = Router()

    async def show(q: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None, photo=None) -> Message | None:
        """Replace the tapped message: edit in place when possible, re-send when switching text ↔ photo."""
        m = q.message
        if photo is not None and m.photo:
            with suppress(TelegramBadRequest):  # "message is not modified"
                await m.edit_caption(caption=text, reply_markup=markup)
            return None
        if photo is None and not m.photo:
            with suppress(TelegramBadRequest):
                await m.edit_text(text, reply_markup=markup)
            return None
        with suppress(TelegramBadRequest):
            await m.delete()
        if photo is None:
            return await m.answer(text, reply_markup=markup)
        return await m.answer_photo(photo, caption=text, reply_markup=markup)

    def categories_view(cafe: Cafe) -> tuple[str, InlineKeyboardMarkup | None]:
        cats = store.menu(cafe.id)
        if not cats:
            return "Меню пока пустое — загляните чуть позже.", None
        return "Выберите раздел:", kb([[(c.name, f"cat:{c.id}")] for c in cats])

    def cart_view(cafe: Cafe, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
        lines = store.cart(cafe.id, user_id)
        if not lines:
            return "Корзина пуста — загляните в меню.", kb([[("☕️ Меню", "menu")]])
        body = "\n".join(f"{ln.title} × {ln.qty} — {cafe.money(ln.sum)}" for ln in lines)
        total = sum(ln.sum for ln in lines)
        rows = [[(f"➖ {ln.title}", f"dec:{ln.id}"), ("➕", f"inc:{ln.id}")] for ln in lines]
        rows += [[("🗑 Очистить", "clear"), ("✅ Оформить", "checkout")]]
        return f"🛒 Ваш заказ:\n\n{body}\n\nИтого: {cafe.money(total)}", kb(rows)

    def item_view(cafe: Cafe, it: Item, chosen: list[int]) -> tuple[str, InlineKeyboardMarkup]:
        options = store.pick(it, chosen) or []
        lines = [it.name] + ([it.description] if it.description else []) + [""]
        rows = []
        for g in it.groups:
            names = [o.name for o in g.options if o in options]
            lines.append(f"{g.name}{' (можно несколько)' if g.multiple else ''}: {', '.join(names) or '—'}")
            buttons = []
            for o in g.options:
                if g.multiple:
                    sel = set(chosen) ^ {o.id}
                else:
                    sel = (set(chosen) - {x.id for x in g.options}) | {o.id}
                label = (
                    ("✅ " if o in options else "") + o.name + (f" +{o.price:,}".replace(",", " ") if o.price else "")
                )
                buttons.append((label, f"it:{it.id}:{'.'.join(map(str, sorted(sel)))}"))
            rows += [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
        price = it.price + sum(o.price for o in options)
        lines.append(f"Цена: {cafe.money(price)}")
        cat = next(c for c in store.menu(cafe.id) if c.id == it.category_id)
        rows.append([(f"➕ В корзину · {cafe.money(price)}", f"add:{it.id}:{'.'.join(map(str, sorted(chosen)))}")])
        rows.append([(f"← {cat.name}", f"cat:{cat.id}"), ("🛒 Корзина", "cart")])
        return "\n".join(lines), kb(rows)

    # --- customer ---
    @r.message(CommandStart())
    async def start(m: Message, state: FSMContext, command: CommandObject, cafe: Cafe):
        await state.clear()
        code = (command.args or "").removeprefix("join-")
        if command.args and store.join(cafe.id, code, m.from_user.id, m.from_user.full_name):
            return await m.answer(
                f"Готово, вы администратор «{cafe.name}».\n\n"
                "/admin — ссылка для входа в веб-админку (меню, цены, фото, заказы)\n"
                "/staff — отправьте в группе персонала, чтобы заказы приходили туда\n"
                "/stats — заказы за сегодня",
                reply_markup=MAIN_KB,
            )
        await m.answer(cafe.greeting, reply_markup=MAIN_KB)

    @r.message(F.text == MENU)
    @r.message(Command("menu"))
    async def menu(m: Message, cafe: Cafe):
        text, markup = categories_view(cafe)
        await m.answer(text, reply_markup=markup)

    @r.callback_query(F.data == "menu")
    async def menu_cb(q: CallbackQuery, cafe: Cafe):
        await show(q, *categories_view(cafe))
        await q.answer()

    @r.callback_query(F.data.startswith("cat:"))
    async def category(q: CallbackQuery, cafe: Cafe):
        cat = store.category(cafe.id, int(q.data[4:]))
        if not cat:
            return await q.answer("Раздел обновился, откройте меню заново")
        rows = [[(f"{it.name} · {cafe.money(it.price)}", f"it:{it.id}")] for it in cat.items]
        rows.append([("← Разделы", "menu"), ("🛒 Корзина", "cart")])
        await show(q, f"{cat.name}\nВыберите позицию:", kb(rows))
        await q.answer()

    @r.callback_query(F.data.startswith("it:"))
    async def item(q: CallbackQuery, cafe: Cafe):
        _, item_id, *sel = q.data.split(":")
        it = store.item(cafe.id, int(item_id))
        if not it:
            return await q.answer("Этой позиции сейчас нет", show_alert=True)
        chosen = parse_ids(sel[0]) if sel else []
        if store.pick(it, chosen) is None:  # first open, or options changed since the card was drawn
            chosen = it.default_options()
        text, markup = item_view(cafe, it, chosen)
        photo = (it.photo_file_id or FSInputFile(settings.media_dir / it.photo)) if it.photo else None
        sent = await show(q, text, markup, photo)
        if sent and sent.photo and not it.photo_file_id:
            store.set_photo_file_id(it.id, sent.photo[-1].file_id)
        await q.answer()

    @r.callback_query(F.data.startswith("add:"))
    async def add(q: CallbackQuery, cafe: Cafe):
        _, item_id, sel = q.data.split(":")
        line = store.add_to_cart(cafe.id, q.from_user.id, int(item_id), parse_ids(sel))
        if not line:
            return await q.answer("Позиция изменилась — откройте её в меню заново", show_alert=True)
        await q.answer(f"{line.title} в корзине: {line.qty} шт.")

    @r.message(F.text == CART)
    @r.message(Command("cart"))
    async def cart(m: Message, cafe: Cafe):
        text, markup = cart_view(cafe, m.from_user.id)
        await m.answer(text, reply_markup=markup)

    @r.callback_query(F.data == "cart")
    @r.callback_query(F.data.startswith("inc:") | F.data.startswith("dec:") | (F.data == "clear"))
    async def cart_edit(q: CallbackQuery, cafe: Cafe):
        if q.data == "clear":
            store.clear(cafe.id, q.from_user.id)
        elif q.data != "cart":
            store.change(cafe.id, q.from_user.id, int(q.data[4:]), 1 if q.data.startswith("inc:") else -1)
        await show(q, *cart_view(cafe, q.from_user.id))
        await q.answer()

    @r.callback_query(F.data == "checkout")
    async def checkout(q: CallbackQuery, state: FSMContext, cafe: Cafe):
        if not cafe.is_open():
            return await q.answer(
                f"Сейчас закрыто. Работаем {cafe.open_time:%H:%M}–{cafe.close_time:%H:%M}",
                show_alert=True,
            )
        total = sum(ln.sum for ln in store.cart(cafe.id, q.from_user.id))
        if not total:
            return await q.answer("Корзина пуста")
        options = [("🏃 Самовывоз", "m:pickup")]
        if cafe.delivery_enabled:
            if total >= cafe.delivery_min:
                options.append((f"🚗 Доставка (+{cafe.money(cafe.delivery_fee)})", "m:delivery"))
            else:
                options.append((f"Доставка от {cafe.money(cafe.delivery_min)}", "m:min"))
        await state.set_state(Checkout.method)
        await show(q, "Как получите заказ?", kb([options]))
        await q.answer()

    @r.callback_query(Checkout.method, F.data == "m:min")
    async def below_min(q: CallbackQuery, cafe: Cafe):
        await q.answer(
            f"Доставка от {cafe.money(cafe.delivery_min)} — добавьте что-нибудь в корзину",
            show_alert=True,
        )

    @r.callback_query(Checkout.method, F.data.in_({"m:pickup", "m:delivery"}))
    async def method(q: CallbackQuery, state: FSMContext, cafe: Cafe):
        await state.update_data(method=q.data[2:])
        if q.data == "m:delivery":
            await state.set_state(Checkout.address)
            return await q.message.edit_text("Напишите адрес доставки (улица, дом, подъезд, квартира):")
        await q.message.edit_text(f"Самовывоз: {cafe.pickup_address}")
        await ask_phone(q.message, state)

    @r.message(Checkout.address, F.text)
    async def address(m: Message, state: FSMContext):
        await state.update_data(address=m.text.strip())
        await ask_phone(m, state)

    async def ask_phone(m: Message, state: FSMContext):
        await state.set_state(Checkout.phone)
        share = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await m.answer("Оставьте номер телефона — нажмите кнопку или напишите его:", reply_markup=share)

    @r.message(Checkout.phone, F.contact | F.text)
    async def phone(m: Message, state: FSMContext):
        number = m.contact.phone_number if m.contact else m.text.strip()
        if sum(ch.isdigit() for ch in number) < 7:
            return await m.answer("Не похоже на номер телефона, попробуйте ещё раз.")
        await state.update_data(phone=number)
        await state.set_state(Checkout.comment)
        await m.answer("Комментарий к заказу? Например, «без сахара».", reply_markup=ReplyKeyboardRemove())
        await m.answer("Или пропустите:", reply_markup=kb([[("Без комментария", "skip")]]))

    @r.message(Checkout.comment, F.text)
    @r.callback_query(Checkout.comment, F.data == "skip")
    async def comment(event: Message | CallbackQuery, state: FSMContext, cafe: Cafe):
        if isinstance(event, Message):
            await state.update_data(comment=event.text.strip())
        else:
            await event.answer()
        msg = event if isinstance(event, Message) else event.message
        data = await state.get_data()
        delivery = data["method"] == "delivery"
        text, _ = cart_view(cafe, event.from_user.id)
        where = f"🚗 Доставка: {data['address']}" if delivery else f"🏃 Самовывоз: {cafe.pickup_address}"
        extra = f"\nДоставка: {cafe.money(cafe.delivery_fee)}" if delivery and cafe.delivery_fee else ""
        await state.set_state(Checkout.confirm)
        await msg.answer(
            f"{text}{extra}\n\n{where}\n📱 {data['phone']}\n💳 {cafe.payment_note}",
            reply_markup=kb([[("✏️ Изменить", "cart"), ("✅ Подтвердить заказ", "confirm")]]),
        )

    @r.callback_query(Checkout.confirm, F.data == "confirm")
    async def confirm(q: CallbackQuery, state: FSMContext, bot: Bot, cafe: Cafe):
        data = await state.get_data()
        await state.clear()
        o = store.place(
            cafe,
            q.from_user.id,
            q.from_user.full_name,
            data["phone"],
            data["method"],
            data.get("address"),
            data.get("comment"),
        )
        if not o:
            return await q.answer("Корзина пуста", show_alert=True)
        await q.message.edit_text(f"Спасибо! Заказ №{o.number} отправлен, мы сообщим, когда он будет готов.")
        await q.message.answer("Хотите что-то ещё?", reply_markup=MAIN_KB)
        await send_to_staff(bot, store, cafe, o)

    # --- staff ---
    @r.callback_query(F.data.startswith("st:"))
    async def status(q: CallbackQuery, bot: Bot, cafe: Cafe):
        _, order_id, new = q.data.split(":")
        o = store.order(cafe.id, int(order_id))
        if not o or not await change_status(bot, store, cafe, o, new):
            return await q.answer("Статус уже изменён")
        await q.answer(STATUSES[new])

    # --- owner ---
    async def is_admin(m: Message, cafe: Cafe) -> bool:  # async: aiogram runs sync filters in a thread
        return store.is_admin(cafe.id, m.from_user.id)

    @r.message(Command("admin"), F.chat.type == "private", is_admin)
    async def admin(m: Message, cafe: Cafe):
        token = store.create_login(cafe.id, m.from_user.id)
        await m.answer(
            f"Вход в админку «{cafe.name}». Ссылка одноразовая и действует 15 минут:\n"
            f"{settings.public_url.rstrip('/')}/login/{token}"
        )

    @r.message(Command("staff"), is_admin)
    async def staff(m: Message, cafe: Cafe):
        if m.chat.type == "private":
            store.set_staff_chat(cafe.id, None)
            return await m.answer("Заказы снова приходят администраторам в личные сообщения.")
        store.set_staff_chat(cafe.id, m.chat.id)
        await m.answer(f"Заказы «{cafe.name}» теперь приходят в этот чат.")

    @r.message(Command("stats"), is_admin)
    async def stats(m: Message, cafe: Cafe):
        n, total = store.stats(cafe.id, cafe.today())
        await m.answer(f"Сегодня: {n} заказов на {cafe.money(total)}")

    dp = Dispatcher()

    @dp.update.outer_middleware()
    async def with_cafe(handler, event, data):
        data["cafe"] = store.cafe(cafe_id)
        return await handler(event, data)

    dp.include_router(r)
    return dp
