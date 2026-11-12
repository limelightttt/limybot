"""Telegram side of one cafe. Customers order in the Mini App (web/miniapp.py); the chat greets them with a button to it,
sends order updates and keeps "my orders" (repeat, cancel, "I've paid" for card transfers). Staff get order cards with
status buttons, owners get /admin login links. Every cafe runs its own Dispatcher built here; the cafe is re-read from
the database on every update, so edits made in the web admin apply immediately."""

import logging
from contextlib import suppress
from html import escape
from urllib.parse import urlencode

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import (
    JOIN_TRANSITION,
    LEAVE_TRANSITION,
    ChatMemberUpdatedFilter,
    Command,
    CommandObject,
    CommandStart,
)
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    CopyTextButton,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    ReplyParameters,
    User,
    WebAppInfo,
)

from . import delivery, i18n
from .config import Settings
from .i18n import t
from .models import PAYMENT_METHODS, PAYMENT_STATUSES, ROLES, STATUSES, Cafe, Order, title
from .store import Store

log = logging.getLogger(__name__)

BUTTON = {  # staff actions
    "accepted": "✅ Принять",
    "cooking": "👨‍🍳 Готовится",
    "ready": "🔔 Готов",
    "done": "📦 Выдан",
    "courier": "🛵 Передан курьеру",
    "delivered": "🏁 Доставлен",
    "cancelled": "✖️ Отменить",
}
STYLE = {"accepted": "success", "cancelled": "danger"}  # Telegram colours these buttons since Bot API 9.4


def button(text: str, data: str | None = None, style: str | None = None, **kw) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, style=style, **kw)


def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup | None:
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def app_url(settings: Settings, cafe: Cafe, **query) -> str | None:
    """The cafe's Mini App. Telegram opens only https pages, so a local http setup has none."""
    if not settings.public_url.startswith("https://"):
        return None
    url = f"{settings.public_url.rstrip('/')}/app/{cafe.slug}"
    return f"{url}?{urlencode(query)}" if query else url


def customer_lang(store: Store, cafe_id: int, user: User | int) -> str:
    """The language the customer picked, else their Telegram language."""
    c = store.customer(cafe_id, user if isinstance(user, int) else user.id)
    if c:
        return c.lang
    return i18n.lang_of(None if isinstance(user, int) else user.language_code)


# --- orders: shared with the web admin and the Mini App ---
COURIER_STEPS = {"courier", "delivered"}  # what a courier group may press


def role_of(cafe: Cafe, chat_id: int) -> str:
    """Which card a chat gets: the cafe's groups by role; an admin's private chat sees everything, like the kitchen."""
    return next((role for role, (column, _) in ROLES.items() if getattr(cafe, column) == chat_id), "staff")


def staff_card(cafe: Cafe, o: Order, role: str = "staff") -> tuple[str, InlineKeyboardMarkup | None]:
    """The order card for a staff chat. The kitchen sees everything; couriers — where to go, whom to call, how much to
    take and their two buttons; finance — the money and "payment received"."""
    where = f"🛵 Доставка: {o.address}" if o.method == "delivery" else "🏃 Самовывоз"
    if o.point and cafe.point:
        where += f"\n📍 {delivery.km(cafe.point, o.point):.1f} км от вас".replace(".", ",")
    who = f"👤 {o.customer}, {o.phone}" + (f"\n💬 {o.comment}" if o.comment else "")
    payment = f"{PAYMENT_METHODS[o.payment_method]} · {PAYMENT_STATUSES[o.payment_status]}"
    map_row = [button("🗺 Открыть на карте", url=delivery.map_link(o.point))] if o.point else []
    pay = [button("💳 Оплата получена", f"pay:{o.id}", "success")] if o.awaits_transfer else []
    if role == "courier":
        collect = (
            f"💵 Получить с гостя: {cafe.money(o.total)}"
            if o.payment_method == "cash" and o.payment_status != "paid"
            else f"✅ Оплачено, с гостя ничего не брать ({cafe.money(o.total)})"
            if o.payment_status == "paid"
            else f"💳 Перевод {cafe.money(o.total)} ещё не подтверждён — уточните у кафе"
        )
        text = f"🛵 Доставка №{o.number} · {STATUSES[o.status]}\n\n{where}\n{who}\n\n{collect}\n\n{o.lines_text(cafe)}"
        steps = [button(BUTTON[s], f"st:{o.id}:{s}", STYLE.get(s)) for s in o.next_statuses if s in COURIER_STEPS]
        return text, kb([steps, map_row])
    if role == "finance":
        text = (
            f"💳 Заказ №{o.number} · {STATUSES[o.status]}\n\nИтого: {cafe.money(o.total)}"
            + (f" (доставка {cafe.money(o.delivery_fee)})" if o.delivery_fee else "")
            + f"\n{payment}\n\n{who}"
        )
        return text, kb([pay])
    fee = f"\nДоставка: {cafe.money(o.delivery_fee)}" if o.delivery_fee else ""
    text = (
        f"Заказ №{o.number} · {STATUSES[o.status]}\n\n{o.lines_text(cafe)}{fee}\nИтого: {cafe.money(o.total)}\n"
        f"💳 {payment}\n\n{where}\n{who}"
    )
    actions = [button(BUTTON[s], f"st:{o.id}:{s}", STYLE.get(s)) for s in o.next_statuses]
    if cafe.finance_chat_id:  # transfers are checked by the money people
        pay = []
    return text, kb([actions, pay, map_row])


async def _post(bot: Bot, store: Store, cafe: Cafe, o: Order, chat: int, role: str) -> None:
    text, markup = staff_card(cafe, o, role)
    try:
        msg = await bot.send_message(chat, text, reply_markup=markup)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.warning("%s: cannot deliver order %s to chat %s: %s", cafe.slug, o.number, chat, e)
        return
    store.add_staff_message(o.id, chat, msg.message_id)
    if o.point and role != "finance":  # a real pin under the card: a tap opens it in any map app on the phone
        with suppress(TelegramBadRequest, TelegramForbiddenError):
            reply = ReplyParameters(message_id=msg.message_id, allow_sending_without_reply=True)
            await bot.send_location(chat, *o.point, reply_parameters=reply)


async def send_to_staff(bot: Bot, store: Store, cafe: Cafe, o: Order) -> None:
    """A new order: to the kitchen group (until there is one, to every admin privately) and, for a card transfer, to
    the finance group. Couriers get it once it is accepted (change_status)."""
    chats = [cafe.staff_chat_id] if cafe.staff_chat_id else [uid for uid, _ in store.admins(cafe.id)]
    for chat in chats:
        await _post(bot, store, cafe, o, chat, "staff")
    if cafe.finance_chat_id and o.payment_method == "transfer":
        await _post(bot, store, cafe, o, cafe.finance_chat_id, "finance")


async def refresh_staff(
    bot: Bot, store: Store, cafe: Cafe, o: Order, note: str | None = None, note_roles: set[str] | None = None
) -> None:
    """Redraw the order's cards in staff chats, each for its role. A note goes out as a reply to the card (to the
    chats of note_roles, default all), so people get a notification."""
    for chat_id, message_id in store.staff_messages(o.id):
        role = role_of(cafe, chat_id)
        text, markup = staff_card(cafe, o, role)
        with suppress(TelegramBadRequest):  # card deleted in the chat
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
        if note and (note_roles is None or role in note_roles):
            with suppress(TelegramBadRequest, TelegramForbiddenError):
                reply = ReplyParameters(message_id=message_id, allow_sending_without_reply=True)
                await bot.send_message(chat_id, note, reply_parameters=reply)


def customer_view(cafe: Cafe, o: Order, lang: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """The order message in the customer's chat (HTML), with cancel and card-transfer buttons while they apply."""
    text = t(lang, "order_sent", number=o.number, name=escape(cafe.name)) + "\n\n" + i18n.order_text(cafe, o, lang)
    if o.status != "new":
        text += "\n\n" + t(lang, "status_line", status=i18n.STATUS[lang][o.status])
    rows = []
    if o.awaits_transfer and o.payment_status == "pending":
        total = i18n.money(cafe, o.total, lang)
        howto = t(lang, "transfer_howto", total=total, card=escape(cafe.card_number), holder=escape(cafe.card_holder))
        text += "\n\n" + howto
        rows.append([button(t(lang, "copy_card"), copy_text=CopyTextButton(text=cafe.card_number))])
        rows.append([button(t(lang, "i_paid"), f"paid:{o.id}", "success")])
    elif o.awaits_transfer:
        text += "\n\n" + t(lang, "transfer_claimed")
    elif o.status == "new":
        text += "\n\n" + t(lang, "we_will_write")
    if o.customer_can_cancel:
        rows.append([button(t(lang, "cancel_order"), f"oc:{o.id}", "danger")])
    return text, kb(rows)


async def send_to_customer(bot: Bot, store: Store, cafe: Cafe, o: Order) -> None:
    text, markup = customer_view(cafe, o, customer_lang(store, cafe.id, o.user_id))
    with suppress(TelegramForbiddenError, TelegramBadRequest):  # never started the bot, or blocked it
        msg = await bot.send_message(o.user_id, text, parse_mode="HTML", reply_markup=markup)
        store.set_customer_message(cafe.id, o.id, msg.message_id)


async def refresh_customer(bot: Bot, store: Store, cafe: Cafe, o: Order) -> None:
    """Redraw the order message in the guest's chat: the status, and buttons only while they still apply —
    "Cancel" disappears once the kitchen starts cooking."""
    if not o.customer_message_id:
        return
    text, markup = customer_view(cafe, o, customer_lang(store, cafe.id, o.user_id))
    with suppress(TelegramForbiddenError, TelegramBadRequest):  # deleted by the guest, or "not modified"
        await bot.edit_message_text(
            text, chat_id=o.user_id, message_id=o.customer_message_id, parse_mode="HTML", reply_markup=markup
        )


async def change_status(bot: Bot, store: Store, cafe: Cafe, o: Order, new: str) -> bool:
    """Move an order forward, refresh its staff cards and tell the customer. False for stale requests."""
    if new not in o.next_statuses:
        return False
    store.set_status(cafe.id, o.id, new)
    o = store.order(cafe.id, o.id)
    note = f"✖️ Заказ №{o.number} отменён" if new == "cancelled" else None
    await refresh_staff(bot, store, cafe, o, note=note, note_roles={"courier", "finance"})
    await refresh_customer(bot, store, cafe, o)
    if new == "accepted" and o.method == "delivery" and cafe.courier_chat_id:
        await _post(bot, store, cafe, o, cafe.courier_chat_id, "courier")
    key = f"status_{new}"  # nothing is sent when the order is handed over: the customer already has it
    if key in i18n.BOT_TEXTS["ru"]:
        lang = customer_lang(store, cafe.id, o.user_id)
        with suppress(TelegramForbiddenError, TelegramBadRequest):
            await bot.send_message(o.user_id, t(lang, key, number=o.number, address=cafe.pickup_address))
    return True


async def mark_paid(bot: Bot, store: Store, cafe: Cafe, order_id: int) -> bool:
    """Staff saw a card transfer arrive. False if there is nothing to confirm."""
    if not store.confirm_payment(cafe.id, order_id):
        return False
    o = store.order(cafe.id, order_id)
    await refresh_staff(bot, store, cafe, o)
    await refresh_customer(bot, store, cafe, o)
    lang = customer_lang(store, cafe.id, o.user_id)
    with suppress(TelegramForbiddenError, TelegramBadRequest):
        await bot.send_message(o.user_id, t(lang, "payment_received", number=o.number))
    return True


def build(store: Store, cafe_id: int, settings: Settings) -> Dispatcher:
    r = Router()

    def start_view(cafe: Cafe, lang: str) -> tuple[str, InlineKeyboardMarkup | None]:
        other = "ru" if lang == "uz" else "uz"
        rows = [[button(t(lang, "my_orders"), "orders"), button(t(lang, "switch_lang"), f"lang:{other}")]]
        if not cafe.active():
            return t(lang, "inactive", name=cafe.name), kb(rows)
        url = app_url(settings, cafe)
        if not url:
            return f"{cafe.greeting}\n\n{t(lang, 'no_webapp')}", kb(rows)
        menu = button(t(lang, "open_menu"), style="primary", web_app=WebAppInfo(url=url))
        return cafe.greeting, kb([[menu], *rows])

    def orders_view(cafe: Cafe, user_id: int, lang: str) -> tuple[str, InlineKeyboardMarkup | None]:
        orders = store.customer_orders(cafe.id, user_id)
        url = app_url(settings, cafe)
        if not orders:
            menu = [button(t(lang, "open_menu"), style="primary", web_app=WebAppInfo(url=url))] if url else []
            return t(lang, "orders_empty"), kb([menu])
        blocks, rows = [], []
        for o in orders:
            when = o.created_at.astimezone(cafe.now().tzinfo).strftime("%d.%m %H:%M")
            what = ", ".join(f"{title(ln['name'], ln['options'])} × {ln['qty']}" for ln in o.lines)
            blocks.append(
                f"<b>№{o.number}</b> · {when} · {i18n.STATUS[lang][o.status]}\n"
                f"{escape(what)} — {i18n.money(cafe, o.total, lang)}"
            )
            row = []
            if repeat := app_url(settings, cafe, repeat=o.id):
                row.append(button(t(lang, "repeat", number=o.number), web_app=WebAppInfo(url=repeat)))
            if o.customer_can_cancel:
                row.append(button(t(lang, "cancel_n", number=o.number), f"oc:{o.id}:list", "danger"))
            rows.append(row)
        return t(lang, "orders_title") + "\n\n" + "\n\n".join(blocks), kb(rows)

    async def redraw(q: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None, html: bool = True) -> None:
        with suppress(TelegramBadRequest):  # "message is not modified"
            await q.message.edit_text(text, parse_mode="HTML" if html else None, reply_markup=markup)

    # --- customer ---
    @r.message(CommandStart())
    async def start(m: Message, command: CommandObject, cafe: Cafe):
        code = (command.args or "").removeprefix("join-")
        if command.args and store.join(cafe.id, code, m.from_user.id, m.from_user.full_name):
            return await m.answer(
                f"Готово, вы администратор «{cafe.name}».\n\n"
                "Заказы будут приходить вам сюда. Чтобы они шли сотрудникам, добавьте меня в их группу в Telegram — "
                "дальше я сам буду присылать заказы туда. Есть курьеры или бухгалтер — сделайте им отдельные группы "
                "и тоже добавьте меня: спрошу, для чего группа.\n\n"
                "/admin — вход в админку: меню, цены, фото, заказы\n"
                "/stats — заказы за сегодня"
            )
        text, markup = start_view(cafe, customer_lang(store, cafe.id, m.from_user))
        await m.answer(text, reply_markup=markup)

    @r.callback_query(F.data.regexp(r"^lang:(ru|uz)$").as_("match"))
    async def switch_lang(q: CallbackQuery, match, cafe: Cafe):
        lang = match[1]
        store.set_lang(cafe.id, q.from_user.id, lang)
        await redraw(q, *start_view(cafe, lang), html=False)
        await q.answer()

    @r.message(Command("orders"))
    async def my_orders(m: Message, cafe: Cafe):
        text, markup = orders_view(cafe, m.from_user.id, customer_lang(store, cafe.id, m.from_user))
        await m.answer(text, parse_mode="HTML", reply_markup=markup)

    @r.callback_query(F.data == "orders")
    async def my_orders_cb(q: CallbackQuery, cafe: Cafe):
        text, markup = orders_view(cafe, q.from_user.id, customer_lang(store, cafe.id, q.from_user))
        await q.message.answer(text, parse_mode="HTML", reply_markup=markup)
        await q.answer()

    @r.callback_query(F.data.regexp(r"^oc:(\d+)(:list)?$").as_("match"))
    async def cancel_order(q: CallbackQuery, match, bot: Bot, cafe: Cafe):
        lang = customer_lang(store, cafe.id, q.from_user)
        if not store.cancel_by_customer(cafe.id, q.from_user.id, int(match[1])):
            return await q.answer(t(lang, "cancel_no"), show_alert=True)
        o = store.order(cafe.id, int(match[1]))
        await refresh_staff(bot, store, cafe, o, note=f"❗️ Гость отменил заказ №{o.number} — не готовьте его")
        if match[2]:  # from "my orders": that list is redrawn, the order message too
            await redraw(q, *orders_view(cafe, q.from_user.id, lang))
            await refresh_customer(bot, store, cafe, o)
        else:
            await redraw(q, *customer_view(cafe, o, lang))
        await q.answer(t(lang, "cancelled_ok", number=o.number))

    @r.callback_query(F.data.regexp(r"^paid:(\d+)$").as_("match"))
    async def claim_transfer(q: CallbackQuery, match, bot: Bot, cafe: Cafe):
        lang = customer_lang(store, cafe.id, q.from_user)
        if not store.claim_transfer(cafe.id, q.from_user.id, int(match[1])):
            return await q.answer(t(lang, "claim_no"), show_alert=True)
        o = store.order(cafe.id, int(match[1]))
        await refresh_staff(
            bot,
            store,
            cafe,
            o,
            note=f"💳 Гость сообщил о переводе {cafe.money(o.total)} по заказу №{o.number} — проверьте поступление",
            note_roles={"finance"} if cafe.finance_chat_id else {"staff"},
        )
        await redraw(q, *customer_view(cafe, o, lang))
        await q.answer(t(lang, "claim_ok"))

    @r.message(Command("privacy"))
    async def privacy(m: Message, cafe: Cafe):
        await m.answer(t(customer_lang(store, cafe.id, m.from_user), "privacy", name=cafe.name))

    # --- staff ---
    def chat_role(cafe: Cafe, chat_id: int) -> str | None:
        return next((role for role, (column, _) in ROLES.items() if getattr(cafe, column) == chat_id), None)

    async def is_staff(q: CallbackQuery, cafe: Cafe) -> bool:  # async: aiogram runs sync filters in a thread
        """Staff buttons work in the cafe's groups and for admins. Telegram does not check that callback data came
        from a real button, so a forged st:/pay: from a customer's chat must do nothing; each group may press only
        its own buttons (couriers: picked up / delivered, finance: payment received)."""
        if store.is_admin(cafe.id, q.from_user.id):
            return True
        role = chat_role(cafe, q.message.chat.id)
        if q.data.startswith("pay:"):
            return role == "finance" or (role == "staff" and not cafe.finance_chat_id)
        return role == "staff" or (role == "courier" and q.data.rsplit(":", 1)[1] in COURIER_STEPS)

    @r.callback_query(F.data.regexp(r"^st:(\d+):([a-z]+)$").as_("match"), is_staff)
    async def status(q: CallbackQuery, match, bot: Bot, cafe: Cafe):
        o = store.order(cafe.id, int(match[1]))
        if not o or not await change_status(bot, store, cafe, o, match[2]):
            return await q.answer("Статус уже изменён")
        await q.answer(STATUSES[match[2]])

    @r.callback_query(F.data.regexp(r"^pay:(\d+)$").as_("match"), is_staff)
    async def payment_received(q: CallbackQuery, match, bot: Bot, cafe: Cafe):
        if not await mark_paid(bot, store, cafe, int(match[1])):
            return await q.answer("Оплата уже отмечена")
        await q.answer("Оплачен")

    # --- owner ---
    async def is_admin(m: Message, cafe: Cafe) -> bool:
        return store.is_admin(cafe.id, m.from_user.id)

    @r.message(Command("admin"), F.chat.type == "private", is_admin)
    async def admin(m: Message, cafe: Cafe):
        url = f"{settings.public_url.rstrip('/')}/login/{store.create_login(cafe.id, m.from_user.id)}"
        await m.answer(
            f"Вход в админку «{cafe.name}». Кнопка одноразовая и действует 15 минут.\n\n"
            "Удобнее с компьютера: отправьте /admin в Telegram на компьютере и нажмите кнопку там.",
            reply_markup=kb([[button("Открыть админку", url=url, style="primary")]]),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    @r.message(Command("admin"), is_admin)
    async def admin_in_group(m: Message):
        await m.reply("Ссылку на админку в группе не показываю — напишите /admin мне в личные сообщения.")

    GROUP_COMMANDS = {"staff": "staff", "couriers": "courier", "finance": "finance"}
    BOUND = {
        "staff": "Готово: сюда приходят все заказы «{name}» — с кнопками «Принять», «Готовится» и так далее.",
        "courier": "Готово: это группа курьеров «{name}». Сюда приходят принятые заказы на доставку — адрес, точка на "
        "карте, телефон, сколько взять с гостя — и кнопки «Передан курьеру» и «Доставлен».",
        "finance": "Готово: это группа финансов «{name}». Сюда приходят заказы с переводом на карту — проверьте "
        "поступление и нажмите «Оплата получена», — и отмены.",
    }
    UNBOUND = {
        "staff": "Заказы снова приходят администраторам в личные сообщения.\n"
        "Чтобы они шли сотрудникам, добавьте меня в их группу.",
        "courier": "Группа курьеров отключена: заказы на доставку видны только на кухне.",
        "finance": "Группа финансов отключена: переводы подтверждают на кухне.",
    }

    def role_buttons(current: str | None) -> InlineKeyboardMarkup | None:
        return kb([[button(label, f"role:{role}")] for role, (_, label) in ROLES.items() if role != current])

    @r.message(Command(*GROUP_COMMANDS), is_admin)
    async def bind_group(m: Message, command: CommandObject, cafe: Cafe):
        role = GROUP_COMMANDS[command.command]
        if m.chat.type == "private":  # the command in private switches that group off
            store.set_staff_chat(cafe.id, None, role)
            return await m.answer(UNBOUND[role])
        store.set_staff_chat(cafe.id, m.chat.id, role)
        await m.answer(BOUND[role].format(name=cafe.name))

    @r.callback_query(F.data.regexp(r"^role:(staff|courier|finance)$").as_("match"))
    async def pick_role(q: CallbackQuery, match, cafe: Cafe):
        if not store.is_admin(cafe.id, q.from_user.id):
            return await q.answer("Выбрать может только администратор заведения", show_alert=True)
        store.set_staff_chat(cafe.id, q.message.chat.id, match[1])
        with suppress(TelegramBadRequest):
            await q.message.edit_text(BOUND[match[1]].format(name=cafe.name), reply_markup=role_buttons(match[1]))
        await q.answer("Сохранено")

    @r.message(Command("stats"), is_admin)
    async def stats(m: Message, cafe: Cafe):
        n, total = store.stats(cafe.id, cafe.today())
        await m.answer(f"Сегодня: {n} заказов на {cafe.money(total)}")

    @r.message(Command("admin", "staff", "couriers", "finance", "stats"))
    async def not_admin(m: Message, cafe: Cafe):
        """Silence looks like a broken bot: say why the command does nothing."""
        if m.sender_chat:  # an anonymous group admin: Telegram hides who it is
            return await m.reply(
                "Вы пишете анонимно от имени группы, и я не вижу, кто вы. Отключите «Анонимность» в своих правах "
                "администратора группы или напишите команду мне в личные сообщения."
            )
        await m.reply(
            f"Эта команда — для администраторов «{cafe.name}». Если вы владелец, откройте ссылку-приглашение, "
            "которую вам прислали при подключении, — после этого я буду вас узнавать."
        )

    # --- staff groups: adding the bot is all it takes ---
    @r.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION), F.chat.type.in_({"group", "supergroup"}))
    async def joined_group(event: ChatMemberUpdated, bot: Bot, cafe: Cafe):
        markup = None
        if not store.is_admin(cafe.id, event.from_user.id):
            text = (
                f"Здравствуйте! Я бот заказов «{cafe.name}». Чтобы заказы приходили в эту группу, администратор "
                "заведения должен отправить здесь /staff (кухня), /couriers (курьеры) или /finance (финансы)."
            )
        elif not cafe.staff_chat_id:  # the first group is the kitchen: the common case needs no choice
            store.set_staff_chat(cafe.id, event.chat.id)
            text = (
                BOUND["staff"].format(name=cafe.name) + "\n\nЕсли это группа курьеров или финансов — нажмите кнопку "
                "ниже. Чтобы вернуть заказы в личные сообщения, удалите меня из группы."
            )
            markup = role_buttons("staff")
        else:
            text = f"Здравствуйте! Для чего эта группа «{cafe.name}»?"
            markup = role_buttons(None)
        with suppress(TelegramBadRequest, TelegramForbiddenError):  # a group where the bot may not write
            await bot.send_message(event.chat.id, text, reply_markup=markup)

    @r.my_chat_member(ChatMemberUpdatedFilter(LEAVE_TRANSITION))
    async def left_group(event: ChatMemberUpdated, bot: Bot, cafe: Cafe):
        role = next((role for role, (column, _) in ROLES.items() if getattr(cafe, column) == event.chat.id), None)
        if not role:
            return
        store.set_staff_chat(cafe.id, None, role)
        what = {
            "staff": "заказы теперь приходят вам в личные сообщения",
            "courier": "группы курьеров больше нет",
            "finance": "группы финансов больше нет — переводы подтверждают на кухне",
        }[role]
        for uid, _ in store.admins(cafe.id):
            with suppress(TelegramBadRequest, TelegramForbiddenError):
                await bot.send_message(
                    uid, f"Меня убрали из группы «{event.chat.title}» — {what}. Вернуть — добавьте меня в группу снова."
                )

    @r.message(F.migrate_to_chat_id)
    async def group_upgraded(m: Message, cafe: Cafe):
        """A group that becomes a supergroup gets a new id; the old one stops receiving messages."""
        store.replace_chat(cafe.id, m.chat.id, m.migrate_to_chat_id)

    # anything else typed in private (old keyboards, "hi") gets the greeting with the menu button
    @r.message(F.chat.type == "private", F.text)
    async def fallback(m: Message, cafe: Cafe):
        text, markup = start_view(cafe, customer_lang(store, cafe.id, m.from_user))
        await m.answer(text, reply_markup=markup)

    dp = Dispatcher()

    @dp.update.outer_middleware()
    async def with_cafe(handler, event, data):
        data["cafe"] = store.cafe(cafe_id)
        return await handler(event, data)

    @dp.errors(F.exception.func(lambda e: isinstance(e, TelegramBadRequest) and "query is too old" in e.message))
    async def stale_button(_: ErrorEvent):
        """A tap queued while the bot was off: Telegram no longer takes an answer to it, and nothing is lost."""
        return True

    dp.include_router(r)
    return dp
