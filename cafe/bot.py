"""Telegram side of one cafe. Customers order in the Mini App (web/miniapp.py); the chat greets them with a button to it,
sends order updates and keeps "my orders" (repeat, cancel, "I've paid" for card transfers). Staff get order cards with
status buttons, owners get /admin login links. Every cafe runs its own Dispatcher built here; the cafe is re-read from
the database on every update, so edits made in the web admin apply immediately."""

import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from html import escape
from urllib.parse import urlencode

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
    User,
    WebAppInfo,
)

from . import i18n
from .config import Settings
from .i18n import t
from .models import ACTIVE, HANDED_OVER, PAYMENT_STATUSES, STATUSES, Cafe, Order, title
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
ICON = {
    "new": "🆕",
    "accepted": "✅",
    "cooking": "👨‍🍳",
    "ready": "🔔",
    "done": "📦",
    "courier": "🛵",
    "delivered": "🏁",
    "cancelled": "❌",
}
PAY_ICON = {"pending": "⏳", "claimed": "🧾", "paid": "✅"}


def lines_block(cafe: Cafe, o: Order) -> str:
    """The order's lines as a quote, so they stand apart from the rest of the card (HTML)."""
    rows = [
        f"{escape(title(ln['name'], ln['options']))} × {ln['qty']} — {cafe.money(ln['unit'] * ln['qty'])}"
        for ln in o.lines
    ]
    return "<blockquote>" + "\n".join(rows) + "</blockquote>"


def money_lines(cafe: Cafe, o: Order) -> str:
    fee = f"Доставка: {cafe.money(o.delivery_fee)}\n" if o.delivery_fee else ""
    if o.payment_method == "cash":
        pay = "💵 Наличными" + (f" · {change_text(cafe, o)}" if o.change_from else "")
    else:
        pay = f"💳 Перевод · {PAY_ICON[o.payment_status]} {PAYMENT_STATUSES[o.payment_status].lower()}"
    return f"{fee}<b>Итого: {cafe.money(o.total)}</b>\n{pay}"


def change_text(cafe: Cafe, o: Order) -> str:
    return f"сдача с {cafe.money(o.change_from)} → дать {cafe.money(o.change_from - o.total)}"


def who(o: Order) -> str:
    return f"👤 {escape(o.customer)} · <code>{escape(o.phone)}</code>" + (
        f"\n💬 <i>{escape(o.comment)}</i>" if o.comment else ""
    )


# --- orders: shared with the web admin and the Mini App; all cards are HTML ---
def staff_card(cafe: Cafe, o: Order) -> tuple[str, InlineKeyboardMarkup | None]:
    where = f"🛵 <b>Доставка</b>\n📍 {escape(o.address)}" if o.method == "delivery" else "🏃 <b>Самовывоз</b>"
    text = (
        f"{ICON[o.status]} <b>Заказ №{o.number}</b> · {STATUSES[o.status]}\n{where}\n\n"
        f"{lines_block(cafe, o)}\n{money_lines(cafe, o)}\n\n{who(o)}"
        + (f"\n🛵 Везёт: <b>{escape(o.courier_name)}</b>" if o.courier_name else "")
    )
    steps = o.next_statuses
    if o.status == "courier" and cafe.courier_chat_id:  # the courier marks it at the door, not the kitchen
        steps = []
        text += "\n\n⏳ <i>Ждём отметку курьера</i>"
    actions = [button(BUTTON[s], f"st:{o.id}:{s}", STYLE.get(s)) for s in steps]
    # with a finance group the money is checked there, by whoever sees the bank
    checks = o.awaits_transfer and not cafe.finance_chat_id
    paid = [button("💳 Оплата получена", f"pay:{o.id}", "success")] if checks else []
    return text, kb([actions, paid])


def finance_card(cafe: Cafe, o: Order) -> tuple[str, InlineKeyboardMarkup | None]:
    """A card transfer to check against the bank, in the finance group."""
    text = (
        f"💳 <b>Перевод · заказ №{o.number}</b>\n<b>{cafe.money(o.total)}</b> на карту {escape(cafe.card_number)}\n"
        f"{who(o)}\n{PAY_ICON[o.payment_status]} {PAYMENT_STATUSES[o.payment_status]}"
        + (" · <i>заказ отменён</i>" if o.status == "cancelled" else "")
    )
    return text, kb([[button("💳 Оплата получена", f"pay:{o.id}", "success")]] if o.awaits_transfer else [])


def receipt(cafe: Cafe, o: Order, cancelled_by: str = "кафе") -> str:
    """One line per closed order in the finance group: its history, searchable in Telegram (HTML)."""
    when = o.created_at.astimezone(cafe.now().tzinfo).strftime("%d.%m %H:%M")
    if o.status == "cancelled":
        return f"❌ <b>№{o.number}</b> · {when} · <s>{cafe.money(o.total)}</s> · отменён {cancelled_by}"
    how = "доставка" if o.method == "delivery" else "самовывоз"
    if o.payment_method == "transfer":
        paid = "💳 перевод, подтверждён" if o.payment_status == "paid" else "⚠️ перевод, НЕ подтверждён"
    elif o.courier_name:
        paid = f"💵 наличные, получил курьер {escape(o.courier_name)}"
    else:
        paid = "💵 наличные, " + ("получил курьер" if o.method == "delivery" else "на кассе")
    return f"🧾 <b>№{o.number}</b> · {when} · <b>{cafe.money(o.total)}</b> · {how} · {paid}"


async def to_finance(bot: Bot, cafe: Cafe, text: str) -> None:
    if cafe.finance_chat_id:
        with suppress(TelegramBadRequest, TelegramForbiddenError):
            await bot.send_message(cafe.finance_chat_id, text, parse_mode="HTML")


def courier_card(cafe: Cafe, o: Order) -> tuple[str, InlineKeyboardMarkup | None]:
    """What the couriers' group sees: where to go, whom to call, what to collect. "Беру" first, then "Доставлен"."""
    if o.payment_status == "paid":
        collect = "✅ Оплачено, деньги не брать"
    elif o.payment_method == "transfer":
        collect = "💳 Перевод ещё не подтверждён — уточните у кафе перед выдачей"
    else:
        collect = f"💵 Получить <b>{cafe.money(o.total)}</b>" + (f"\n{change_text(cafe, o)}" if o.change_from else "")
    text = (
        f"{ICON[o.status]} <b>Доставка №{o.number}</b> · {STATUSES[o.status]}\n📍 <b>{escape(o.address)}</b>\n"
        f"{who(o)}\n\n{lines_block(cafe, o)}\n{collect}"
        + (f"\n\n🛵 Везёт: <b>{escape(o.courier_name)}</b>" if o.courier_name else "")
    )
    if o.status != "courier":
        return text, None
    if not o.courier_id:
        return text, kb([[button("🙋 Беру", f"take:{o.id}", "primary")]])
    return text, kb([[button("🏁 Доставлен", f"dlv:{o.id}", "success")]])


async def post(bot: Bot, store: Store, cafe: Cafe, o: Order, chats: list[int], card) -> None:
    text, markup = card(cafe, o)
    for chat in chats:
        try:
            msg = await bot.send_message(chat, text, parse_mode="HTML", reply_markup=markup)
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            log.warning("%s: cannot deliver order %s to chat %s: %s", cafe.slug, o.number, chat, e)
            continue
        store.add_staff_message(o.id, chat, msg.message_id)


async def send_to_staff(bot: Bot, store: Store, cafe: Cafe, o: Order) -> None:
    """To the staff group; until it is set up (/staff), to every admin privately."""
    chats = [cafe.staff_chat_id] if cafe.staff_chat_id else [uid for uid, _ in store.admins(cafe.id)]
    await post(bot, store, cafe, o, chats, staff_card)


async def refresh_staff(bot: Bot, store: Store, cafe: Cafe, o: Order, note: str | None = None) -> None:
    """Redraw the order's cards in staff and courier chats. A note goes out as a reply to the staff card, so staff
    get a notification."""
    for chat_id, message_id in store.staff_messages(o.id):
        is_courier = chat_id in (cafe.courier_chat_id, cafe.finance_chat_id)
        card = {cafe.courier_chat_id: courier_card, cafe.finance_chat_id: finance_card}.get(chat_id, staff_card)
        text, markup = card(cafe, o)
        with suppress(TelegramBadRequest):  # card deleted in the chat
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, parse_mode="HTML", reply_markup=markup
            )
        if note and not is_courier:
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
    if o.status == "new":
        rows.append([button(t(lang, "cancel_order"), f"oc:{o.id}", "danger")])
    return text, kb(rows)


async def send_to_customer(bot: Bot, store: Store, cafe: Cafe, o: Order) -> None:
    text, markup = customer_view(cafe, o, customer_lang(store, cafe.id, o.user_id))
    with suppress(TelegramForbiddenError, TelegramBadRequest):  # never started the bot, or blocked it
        await bot.send_message(o.user_id, text, parse_mode="HTML", reply_markup=markup)


async def change_status(bot: Bot, store: Store, cafe: Cafe, o: Order, new: str) -> bool:
    """Move an order forward, refresh its staff cards and tell the customer. False for stale requests."""
    if new not in o.next_statuses:
        return False
    store.set_status(cafe.id, o.id, new)
    o = store.order(cafe.id, o.id)
    await refresh_staff(bot, store, cafe, o)
    if new == "courier" and cafe.courier_chat_id:
        await post(bot, store, cafe, o, [cafe.courier_chat_id], courier_card)
    if new in HANDED_OVER or new == "cancelled":
        await to_finance(bot, cafe, receipt(cafe, o))
    key = f"status_{new}"  # nothing is sent when the order is handed over: the customer already has it
    if key in i18n.BOT_TEXTS["ru"]:
        lang = customer_lang(store, cafe.id, o.user_id)
        with suppress(TelegramForbiddenError, TelegramBadRequest):
            text = t(lang, key, number=o.number, address=escape(cafe.pickup_address))
            await bot.send_message(o.user_id, text, parse_mode="HTML")
    return True


async def mark_paid(bot: Bot, store: Store, cafe: Cafe, order_id: int) -> bool:
    """Staff saw a card transfer arrive. False if there is nothing to confirm."""
    if not store.confirm_payment(cafe.id, order_id):
        return False
    o = store.order(cafe.id, order_id)
    await refresh_staff(bot, store, cafe, o)
    lang = customer_lang(store, cafe.id, o.user_id)
    with suppress(TelegramForbiddenError, TelegramBadRequest):
        await bot.send_message(o.user_id, t(lang, "payment_received", number=o.number), parse_mode="HTML")
    return True


async def close_stale(bot: Bot, store: Store, cafe: Cafe, hours: int) -> None:
    """Orders left ready or on the way for hours were handed over long ago: close them, so they leave the lists."""
    for o in store.stale(cafe.id, datetime.now(UTC) - timedelta(hours=hours)):
        await change_status(bot, store, cafe, o, o.next_statuses[0])


def report(cafe: Cafe, orders: list[Order], title: str, since: datetime, until: datetime) -> str:
    """Money for a period (HTML): what came in and how, what is still to check, what cash each courier carries."""
    tz = cafe.now().tzinfo
    done = [o for o in orders if o.status != "cancelled"]
    cancelled = [o for o in orders if o.status == "cancelled"]
    cash = [o for o in done if o.payment_method == "cash"]
    transfers = [o for o in done if o.payment_method == "transfer"]
    unconfirmed = [o for o in transfers if o.payment_status != "paid"]
    total = lambda os: cafe.money(sum(o.total for o in os))
    lines = [
        f"📊 <b>Отчёт · {escape(cafe.name)}</b> · {title}",
        f"<i>{since.astimezone(tz):%d.%m %H:%M} — {until.astimezone(tz):%d.%m %H:%M}</i>",
        "",
        f"Заказов: <b>{len(done)}</b> на <b>{total(done)}</b>",
        f"💵 Наличные: {total(cash)}",
        f"💳 Переводы: {total([o for o in transfers if o.payment_status == 'paid'])} подтверждено",
    ]
    if unconfirmed:
        numbers = ", ".join(f"№{o.number}" for o in unconfirmed)
        lines.append(f"⚠️ Не подтверждены: {numbers} — <b>{total(unconfirmed)}</b>")
    if cancelled:
        lines.append(f"❌ Отменено: {len(cancelled)} на {total(cancelled)}")
    by_courier: dict[str, int] = {}
    for o in cash:
        if o.courier_name and o.status == "delivered":
            by_courier[o.courier_name] = by_courier.get(o.courier_name, 0) + o.total
    if by_courier:
        lines += ["", "<b>Наличные у курьеров</b>"]
        lines += [f"🛵 {escape(name)}: {cafe.money(s)}" for name, s in by_courier.items()]
    if active := [o for o in done if o.status in ACTIVE]:
        lines.append(f"\n⏳ Ещё не закрыты: {', '.join(f'№{o.number}' for o in active)}")
    return "\n".join(lines)


async def daily_report(bot: Bot, store: Store, cafe: Cafe) -> None:
    """At the end of each business day the finance group (or the admins) gets its report, once."""
    now = cafe.now()
    began = cafe.day_began(now)
    ends = cafe.day_ends(began)
    if ends > now:  # this day is not over: the last finished one is yesterday's
        began -= timedelta(days=1)
        ends = cafe.day_ends(began)
    if cafe.reported_until is None or cafe.reported_until >= ends:
        if cafe.reported_until is None:  # a new cafe: start from the next day, do not report the past
            store.set_reported_until(cafe.id, ends)
        return
    text = report(cafe, store.orders_between(cafe.id, began, ends), "рабочий день", began, ends)
    chats = [cafe.finance_chat_id] if cafe.finance_chat_id else [uid for uid, _ in store.admins(cafe.id)]
    for chat in chats:
        with suppress(TelegramBadRequest, TelegramForbiddenError):
            await bot.send_message(chat, text, parse_mode="HTML")
    store.set_reported_until(cafe.id, ends)


def build(store: Store, cafe_id: int, settings: Settings) -> Dispatcher:
    r = Router()

    def start_view(cafe: Cafe, lang: str) -> tuple[str, InlineKeyboardMarkup | None]:
        other = "ru" if lang == "uz" else "uz"
        rows = [[button(t(lang, "my_orders"), "orders"), button(t(lang, "switch_lang"), f"lang:{other}")]]
        if not cafe.active():
            return t(lang, "inactive", name=cafe.name), kb(rows)
        url = app_url(settings, cafe)
        if not url:  # a local run without https: Telegram opens Mini Apps only over https
            return cafe.greeting, kb(rows)
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
            if o.status == "new":
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
                "/admin — ссылка для входа в веб-админку (меню, цены, фото, заказы)\n"
                "/staff — отправьте в группе персонала, чтобы заказы приходили туда\n"
                "/couriers — в группе курьеров, /finance — в группе для денег и отчётов\n"
                "/report — отчёт за сегодня"
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
        await refresh_staff(bot, store, cafe, o, note=f"❗️ Клиент отменил заказ №{o.number}")
        await to_finance(bot, cafe, receipt(cafe, o, cancelled_by="клиентом"))
        await redraw(q, *(orders_view(cafe, q.from_user.id, lang) if match[2] else customer_view(cafe, o, lang)))
        await q.answer(t(lang, "cancelled_ok", number=o.number))

    @r.callback_query(F.data.regexp(r"^paid:(\d+)$").as_("match"))
    async def claim_transfer(q: CallbackQuery, match, bot: Bot, cafe: Cafe):
        lang = customer_lang(store, cafe.id, q.from_user)
        if not store.claim_transfer(cafe.id, q.from_user.id, int(match[1])):
            return await q.answer(t(lang, "claim_no"), show_alert=True)
        o = store.order(cafe.id, int(match[1]))
        if cafe.finance_chat_id:
            await refresh_staff(bot, store, cafe, o)
            await post(bot, store, cafe, o, [cafe.finance_chat_id], finance_card)
        else:
            note = f"💳 Клиент сообщил о переводе по заказу №{o.number} — проверьте"
            await refresh_staff(bot, store, cafe, o, note=note)
        await redraw(q, *customer_view(cafe, o, lang))
        await q.answer(t(lang, "claim_ok"))

    @r.message(F.chat.type == "private", F.photo)
    async def transfer_screenshot(m: Message, bot: Bot, cafe: Cafe):
        """After "I've paid" the customer sends the bank screenshot; it goes to whoever checks the money."""
        o = store.claimed_transfer(cafe.id, m.from_user.id)
        if not o:
            return
        chats = [cafe.finance_chat_id or cafe.staff_chat_id] if cafe.finance_chat_id or cafe.staff_chat_id else []
        caption = f"🧾 Скриншот перевода по заказу №{o.number} · {cafe.money(o.total)} · {o.customer}"
        for chat in chats or [uid for uid, _ in store.admins(cafe.id)]:
            with suppress(TelegramBadRequest, TelegramForbiddenError):
                await bot.copy_message(chat, m.chat.id, m.message_id, caption=caption)
        await m.answer(t(customer_lang(store, cafe.id, m.from_user), "screenshot_ok"))

    @r.message(Command("privacy"))
    async def privacy(m: Message, cafe: Cafe):
        await m.answer(t(customer_lang(store, cafe.id, m.from_user), "privacy", name=cafe.name))

    # --- staff ---
    async def is_staff(q: CallbackQuery, cafe: Cafe) -> bool:  # async: aiogram runs sync filters in a thread
        """Staff buttons work in the staff group and for admins. Telegram does not check that callback data came from
        a real button, so a forged st:/pay: from a customer's chat must do nothing."""
        in_staff_chat = cafe.staff_chat_id is not None and q.message.chat.id == cafe.staff_chat_id
        return in_staff_chat or store.is_admin(cafe.id, q.from_user.id)

    @r.callback_query(F.data.regexp(r"^st:(\d+):([a-z]+)$").as_("match"), is_staff)
    async def status(q: CallbackQuery, match, bot: Bot, cafe: Cafe):
        o = store.order(cafe.id, int(match[1]))
        if match[2] == "delivered" and cafe.courier_chat_id:
            return await q.answer("Доставку отмечает курьер в своей группе", show_alert=True)
        if not o or not await change_status(bot, store, cafe, o, match[2]):
            return await q.answer("Статус уже изменён")
        await q.answer(STATUSES[match[2]])

    async def checks_money(q: CallbackQuery, cafe: Cafe) -> bool:
        """The finance group when there is one, otherwise staff; admins always."""
        if cafe.finance_chat_id is not None and q.message.chat.id == cafe.finance_chat_id:
            return True
        return store.is_admin(cafe.id, q.from_user.id) or (not cafe.finance_chat_id and await is_staff(q, cafe))

    @r.callback_query(F.data.regexp(r"^pay:(\d+)$").as_("match"), checks_money)
    async def payment_received(q: CallbackQuery, match, bot: Bot, cafe: Cafe):
        if not await mark_paid(bot, store, cafe, int(match[1])):
            return await q.answer("Оплата уже отмечена")
        await q.answer("Оплачен")

    # --- couriers ---
    async def is_courier(q: CallbackQuery, cafe: Cafe) -> bool:
        return cafe.courier_chat_id is not None and q.message.chat.id == cafe.courier_chat_id

    @r.callback_query(F.data.regexp(r"^take:(\d+)$").as_("match"), is_courier)
    async def take(q: CallbackQuery, match, bot: Bot, cafe: Cafe):
        if not store.take(cafe.id, int(match[1]), q.from_user.id, q.from_user.full_name):
            return await q.answer("Заказ уже взял другой курьер", show_alert=True)
        await refresh_staff(bot, store, cafe, store.order(cafe.id, int(match[1])))
        await q.answer("Заказ ваш")

    @r.callback_query(F.data.regexp(r"^dlv:(\d+)$").as_("match"), is_courier)
    async def delivered(q: CallbackQuery, match, bot: Bot, cafe: Cafe):
        o = store.order(cafe.id, int(match[1]))
        if not o or o.courier_id != q.from_user.id:
            return await q.answer("Отметить доставку может тот, кто взял заказ", show_alert=True)
        if not await change_status(bot, store, cafe, o, "delivered"):
            return await q.answer("Статус уже изменён")
        await q.answer("Доставлен")

    # --- owner ---
    async def is_admin(m: Message, cafe: Cafe) -> bool:
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

    @r.message(Command("couriers"), is_admin)
    async def couriers(m: Message, cafe: Cafe):
        if m.chat.type == "private":
            store.set_courier_chat(cafe.id, None)
            return await m.answer("Группа курьеров отключена: доставку отмечает персонал.")
        store.set_courier_chat(cafe.id, m.chat.id)
        await m.answer(f"Доставки «{cafe.name}» теперь приходят в этот чат: «Беру» — взять, «Доставлен» — у двери.")

    @r.message(Command("finance"), is_admin)
    async def finance(m: Message, cafe: Cafe):
        if m.chat.type == "private":
            store.set_finance_chat(cafe.id, None)
            return await m.answer("Группа финансов отключена: переводы проверяет персонал, отчёт — /report здесь.")
        store.set_finance_chat(cafe.id, m.chat.id)
        await m.answer(
            f"Финансы «{cafe.name}» теперь здесь: закрытые заказы, переводы со скриншотами и отчёт в конце дня.\n"
            "/report — за сегодня, /report yesterday — за вчера, /report 12h — за последние 12 часов."
        )

    async def sees_money(m: Message, cafe: Cafe) -> bool:
        in_finance = cafe.finance_chat_id is not None and m.chat.id == cafe.finance_chat_id
        return in_finance or (m.chat.type == "private" and store.is_admin(cafe.id, m.from_user.id))

    @r.message(Command("report"), sees_money)
    async def report_cmd(m: Message, command: CommandObject, cafe: Cafe):
        now = cafe.now()
        arg = (command.args or "").strip().lower()
        began = cafe.day_began(now)
        if arg == "yesterday":
            since, until, title = began - timedelta(days=1), began, "вчера"
        elif arg.endswith("h") and arg[:-1].isdigit() and 0 < int(arg[:-1]) <= 24 * 31:
            since, until, title = now - timedelta(hours=int(arg[:-1])), now, f"последние {arg[:-1]} ч"
        else:
            since, until, title = began, now, "сегодня"
        await m.answer(
            report(cafe, store.orders_between(cafe.id, since, until), title, since, until), parse_mode="HTML"
        )

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

    dp.include_router(r)
    return dp
