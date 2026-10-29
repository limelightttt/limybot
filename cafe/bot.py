"""Telegram handlers: menu → cart → checkout for customers, order buttons for staff, menu edits for the owner."""

from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .config import Settings
from .store import STATUSES, Order, Store

MENU, CART = "☕️ Меню", "🛒 Корзина"
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=MENU), KeyboardButton(text=CART)]],
    resize_keyboard=True,
)
# what staff can do with an order in each status
NEXT = {
    "new": ["accepted", "cancelled"],
    "accepted": ["ready", "cancelled"],
    "ready": ["done"],
}
BUTTON = {
    "accepted": "✅ Принять",
    "ready": "🔔 Готов",
    "done": "📦 Выдан",
    "cancelled": "✖️ Отменить",
}
TO_CUSTOMER = {
    "accepted": "Заказ №{id} принят и готовится ☕️",
    "ready": "Заказ №{id} готов! {where}",
    "cancelled": "Заказ №{id} отменён. Если это ошибка — напишите нам.",
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


def build(settings: Settings, store: Store) -> Dispatcher:
    c = store.client
    r = Router()

    def cart_view(user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
        lines = store.cart(user_id)
        if not lines:
            return "Корзина пуста — загляните в меню.", kb([[("☕️ Меню", "menu")]])
        body = "\n".join(f"{ln.item.name} × {ln.qty} — {c.money(ln.sum)}" for ln in lines)
        total = sum(ln.sum for ln in lines)
        rows = [[(f"➖ {ln.item.name}", f"dec:{ln.item.id}"), ("➕", f"inc:{ln.item.id}")] for ln in lines]
        rows += [[("🗑 Очистить", "clear"), ("✅ Оформить", "checkout")]]
        return f"🛒 Ваш заказ:\n\n{body}\n\nИтого: {c.money(total)}", kb(rows)

    def staff_view(o: Order) -> tuple[str, InlineKeyboardMarkup | None]:
        where = f"🚗 Доставка: {o.address}" if o.method == "delivery" else "🏃 Самовывоз"
        text = (
            f"Заказ №{o.id} · {STATUSES[o.status]}\n\n{o.lines}\nИтого: {c.money(o.total)}\n\n"
            f"{where}\n👤 {o.customer}, {o.phone}" + (f"\n💬 {o.comment}" if o.comment else "")
        )
        buttons = [(BUTTON[s], f"st:{o.id}:{s}") for s in NEXT.get(o.status, [])]
        return text, kb([buttons]) if buttons else None

    # --- customer ---
    @r.message(CommandStart())
    async def start(m: Message, state: FSMContext):
        await state.clear()
        await m.answer(c.greeting, reply_markup=MAIN_KB)

    @r.message(F.text == MENU)
    @r.message(Command("menu"))
    async def menu(m: Message):
        cats = [[(name, f"cat:{i}")] for i, (name, _) in enumerate(store.menu())]
        await m.answer("Выберите раздел:", reply_markup=kb(cats))

    @r.callback_query(F.data == "menu")
    async def menu_cb(q: CallbackQuery):
        cats = [[(name, f"cat:{i}")] for i, (name, _) in enumerate(store.menu())]
        await q.message.edit_text("Выберите раздел:", reply_markup=kb(cats))

    @r.callback_query(F.data.startswith("cat:"))
    async def category(q: CallbackQuery):
        sections = store.menu()
        i = int(q.data.split(":")[1])
        if i >= len(sections):
            return await q.answer("Раздел обновился, откройте меню заново")
        name, items = sections[i]
        rows = [[(f"{it.name} · {c.money(it.price)}", f"add:{it.id}")] for it in items]
        rows.append([("← Разделы", "menu"), ("🛒 Корзина", "cart")])
        await q.message.edit_text(
            f"{name}\nНажмите на позицию, чтобы добавить в корзину:",
            reply_markup=kb(rows),
        )

    @r.callback_query(F.data.startswith("add:"))
    async def add(q: CallbackQuery):
        it = store.item(q.data[4:])
        if not it:
            return await q.answer("Этой позиции сейчас нет", show_alert=True)
        store.add(q.from_user.id, it.id)
        qty = next(ln.qty for ln in store.cart(q.from_user.id) if ln.item.id == it.id)
        await q.answer(f"{it.name} в корзине: {qty} шт.")

    @r.message(F.text == CART)
    @r.message(Command("cart"))
    async def cart(m: Message):
        text, markup = cart_view(m.from_user.id)
        await m.answer(text, reply_markup=markup)

    @r.callback_query(F.data == "cart")
    @r.callback_query(F.data.startswith("inc:") | F.data.startswith("dec:") | (F.data == "clear"))
    async def cart_edit(q: CallbackQuery):
        if q.data == "clear":
            store.clear(q.from_user.id)
        elif q.data != "cart":
            store.add(q.from_user.id, q.data[4:], 1 if q.data.startswith("inc:") else -1)
        text, markup = cart_view(q.from_user.id)
        await q.message.edit_text(text, reply_markup=markup)
        await q.answer()

    @r.callback_query(F.data == "checkout")
    async def checkout(q: CallbackQuery, state: FSMContext):
        if not c.is_open():
            return await q.answer(
                f"Сейчас закрыто. Работаем {c.hours.open:%H:%M}–{c.hours.close:%H:%M}",
                show_alert=True,
            )
        total = sum(ln.sum for ln in store.cart(q.from_user.id))
        if not total:
            return await q.answer("Корзина пуста")
        options = [("🏃 Самовывоз", "m:pickup")]
        if c.delivery.enabled:
            if total >= c.delivery.min_order:
                options.append((f"🚗 Доставка (+{c.money(c.delivery.fee)})", "m:delivery"))
            else:
                options.append((f"Доставка от {c.money(c.delivery.min_order)}", "m:min"))
        await state.set_state(Checkout.method)
        await q.message.edit_text("Как получите заказ?", reply_markup=kb([options]))

    @r.callback_query(Checkout.method, F.data == "m:min")
    async def below_min(q: CallbackQuery):
        await q.answer(
            f"Доставка от {c.money(c.delivery.min_order)} — добавьте что-нибудь в корзину",
            show_alert=True,
        )

    @r.callback_query(Checkout.method, F.data.in_({"m:pickup", "m:delivery"}))
    async def method(q: CallbackQuery, state: FSMContext):
        await state.update_data(method=q.data[2:])
        if q.data == "m:delivery":
            await state.set_state(Checkout.address)
            return await q.message.edit_text("Напишите адрес доставки (улица, дом, подъезд, квартира):")
        await q.message.edit_text(f"Самовывоз: {c.pickup_address}")
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
        await m.answer(
            "Оставьте номер телефона — нажмите кнопку или напишите его:",
            reply_markup=share,
        )

    @r.message(Checkout.phone, F.contact | F.text)
    async def phone(m: Message, state: FSMContext):
        number = m.contact.phone_number if m.contact else m.text.strip()
        if sum(ch.isdigit() for ch in number) < 7:
            return await m.answer("Не похоже на номер телефона, попробуйте ещё раз.")
        await state.update_data(phone=number)
        await state.set_state(Checkout.comment)
        await m.answer(
            "Комментарий к заказу? Например, «без сахара».",
            reply_markup=ReplyKeyboardRemove(),
        )
        await m.answer("Или пропустите:", reply_markup=kb([[("Без комментария", "skip")]]))

    @r.message(Checkout.comment, F.text)
    @r.callback_query(Checkout.comment, F.data == "skip")
    async def comment(event: Message | CallbackQuery, state: FSMContext):
        if isinstance(event, Message):
            await state.update_data(comment=event.text.strip())
        else:
            await event.answer()
        msg = event if isinstance(event, Message) else event.message
        data = await state.get_data()
        fee = c.delivery.fee if data["method"] == "delivery" else 0
        text, _ = cart_view(event.from_user.id)
        where = (
            f"🚗 Доставка: {data['address']}" if data["method"] == "delivery" else f"🏃 Самовывоз: {c.pickup_address}"
        )
        extra = f"\nДоставка: {c.money(fee)}" if fee else ""
        await state.set_state(Checkout.confirm)
        await msg.answer(
            f"{text}{extra}\n\n{where}\n📱 {data['phone']}\n💳 {c.payment_note}",
            reply_markup=kb([[("✏️ Изменить", "cart"), ("✅ Подтвердить заказ", "confirm")]]),
        )

    @r.callback_query(Checkout.confirm, F.data == "confirm")
    async def confirm(q: CallbackQuery, state: FSMContext, bot: Bot):
        data = await state.get_data()
        await state.clear()
        fee = c.delivery.fee if data["method"] == "delivery" else 0
        o = store.place(
            q.from_user.id,
            q.from_user.full_name,
            data["phone"],
            data["method"],
            data.get("address"),
            data.get("comment"),
            fee,
        )
        if not o:
            return await q.answer("Корзина пуста", show_alert=True)
        await q.message.edit_text(f"Спасибо! Заказ №{o.id} отправлен, мы сообщим, когда он будет готов.")
        await q.message.answer("Хотите что-то ещё?", reply_markup=MAIN_KB)
        # orders go to the staff group; until it is set up, straight to the owner
        text, markup = staff_view(o)
        for chat in [settings.staff_chat_id] if settings.staff_chat_id else sorted(settings.admins):
            await bot.send_message(chat, text, reply_markup=markup)

    # --- staff ---
    @r.callback_query(F.data.startswith("st:"))
    async def status(q: CallbackQuery, bot: Bot):
        _, order_id, new = q.data.split(":")
        o = store.order(int(order_id))
        if not o or new not in NEXT.get(o.status, []):
            return await q.answer("Статус уже изменён")
        store.set_status(o.id, new)
        o = store.order(o.id)
        text, markup = staff_view(o)
        await q.message.edit_text(text, reply_markup=markup)
        if new in TO_CUSTOMER:
            where = "Ждём вас 🙌" if o.method == "pickup" else "Курьер уже в пути 🚗"
            await bot.send_message(o.user_id, TO_CUSTOMER[new].format(id=o.id, where=where))
        await q.answer(STATUSES[new])

    # --- owner ---
    def is_admin(m: Message) -> bool:
        return m.from_user.id in settings.admins

    @r.message(Command("myid"))
    async def myid(m: Message):
        await m.answer(f"Ваш id: {m.from_user.id}")

    @r.message(Command("chatid"))
    async def chatid(m: Message):
        await m.answer(f"id этого чата: {m.chat.id}")

    @r.message(Command("admin"), is_admin)
    async def admin(m: Message):
        lines = []
        available = {i.id for _, items in store.menu() for i in items}
        for name, items in store.menu(only_available=False):
            lines.append(f"\n{name}")
            for it in items:
                mark = "" if it.id in available else " ⛔️ стоп"
                lines.append(f"  {it.id} — {it.name}, {c.money(it.price)}{mark}")
        await m.answer(
            "Меню:" + "\n".join(lines) + "\n\nКоманды:\n/price latte 32000 — цена\n/off latte — в стоп-лист\n"
            "/on latte — вернуть\n/stats — заказы за сегодня"
        )

    @r.message(Command("price"), is_admin)
    async def price(m: Message, command: CommandObject):
        parts = (command.args or "").split()
        if len(parts) != 2 or not store.is_known(parts[0]) or not parts[1].isdigit():
            return await m.answer("Формат: /price latte 32000 (id позиции — в /admin)")
        store.set_price(parts[0], int(parts[1]))
        await m.answer(f"Цена {parts[0]}: {c.money(int(parts[1]))}")

    @r.message(Command("off", "on"), is_admin)
    async def toggle(m: Message, command: CommandObject):
        item_id = (command.args or "").strip()
        if not store.is_known(item_id):
            return await m.answer(f"Формат: /{command.command} latte (id позиции — в /admin)")
        store.set_available(item_id, command.command == "on")
        await m.answer(f"{item_id}: " + ("снова в меню" if command.command == "on" else "в стоп-листе"))

    @r.message(Command("stats"), is_admin)
    async def stats(m: Message):
        now = datetime.now(ZoneInfo(c.timezone))
        n, total = store.stats(now.replace(hour=0, minute=0, second=0, microsecond=0))
        await m.answer(f"Сегодня: {n} заказов на {c.money(total)}")

    dp = Dispatcher()
    dp.include_router(r)
    return dp
