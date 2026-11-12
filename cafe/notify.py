"""Messages nobody has to remember to send.

Owners get them in their own cafe bot: the subscription ends in 3 days, tomorrow, it ended (the grace days), the bot
stopped taking orders, and "payment received". The operator gets them in the service bot — a bot of their own
connected in /ops: a morning digest (who pays soon, who is in the grace days or switched off, broken bots, yesterday's
orders) and an alert when a cafe is switched off for not paying or Telegram rejects its token. /panel there gives a
login link to /ops, so the operator never needs the terminal.

Every message goes out once: store.reminded / mark_reminded, keyed by the paid_until date it is about."""

import hashlib
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import billing, reports
from .billing import days, last_order_day
from .config import Settings
from .models import BOT_STATES, Cafe, money, orders_word
from .store import OPERATOR, OPS, Store

log = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Tashkent")
DIGEST_HOUR = 10  # the operator's morning digest, Tashkent time
SUMMARY_HOUR = 9  # the owner's summary of the day before, the cafe's time
REMIND_HOURS = range(10, 21)  # subscription reminders reach owners in the daytime, not at 00:00 when the date flips


def price(store: Store) -> int:
    return int(store.meta("price") or billing.PRICE)


def contact(store: Store, settings: Settings) -> str:
    """Where owners pay or ask about the subscription: set in /ops, else SUPPORT from .env."""
    return store.meta("support") or settings.support


def how_to_pay(store: Store, settings: Settings) -> str:
    who = contact(store, settings)
    where = f"Оплата и вопросы: {who}" if who else "Чтобы оплатить, напишите тому, кто подключал вам бота."
    return f"Стоимость — {money(price(store))} в месяц. {where}"


# --- owners ---
def owner_reminder(store: Store, settings: Settings, cafe: Cafe) -> tuple[str, str] | None:
    """(kind, text) the owner should get now about the subscription, if any."""
    state, left = cafe.subscription()
    if state not in ("soon", "grace", "off"):
        return None
    what = "Пробный период" if not store.payments(cafe.id) else "Срок подписки"
    until = f"{cafe.paid_until:%d.%m.%Y}"
    pay = how_to_pay(store, settings)
    name = f"«{cafe.name}»"
    if state == "soon" and left >= 2:
        return "soon3", (
            f"{what} бота {name} заканчивается {until} — через {days(left)}. Продлите заранее, тогда бот будет "
            f"принимать заказы без перерыва.\n\n{pay}"
        )
    if state == "soon":
        when = "сегодня" if left == 0 else "завтра"
        return "soon1", f"{what} бота {name} заканчивается {when}, {until}.\n\n{pay}"
    if state == "grace":
        return "grace", (
            f"{what} бота {name} закончился {until}. Бот ещё принимает заказы до {last_order_day(cafe.paid_until):%d.%m} "
            f"включительно, потом перестанет до оплаты. Меню и заказы сохранятся.\n\n{pay}"
        )
    return "off", (
        f"Бот {name} перестал принимать заказы: {what.lower()} закончился {until}. Гости видят «сейчас не принимаем "
        f"заказы». Оплатите — бот включится сразу, меню и история заказов на месте.\n\n{pay}"
    )


async def tell_owners(store: Store, bot: Bot, cafe_id: int, text: str) -> bool:
    """To every admin of the cafe; False if nobody got it (all of them blocked the bot)."""
    admins = store.admins(cafe_id)
    sent = False
    for user_id, _ in admins:
        with suppress(TelegramAPIError):
            await bot.send_message(user_id, text)
            sent = True
    return sent or not admins


async def payment_received(store: Store, bot: Bot | None, cafe: Cafe, until) -> None:
    if bot:
        await tell_owners(
            store, bot, cafe.id, f"Оплата получена, спасибо! Бот «{cafe.name}» работает до {until:%d.%m.%Y}."
        )


# --- the operator ---
def digest(store: Store, bot_state: Callable[[int], str], now: datetime) -> str:
    """What the operator should know this morning."""
    today = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    orders = revenue = 0
    soon, grace, off, broken = [], [], [], []
    for cafe in store.cafes():
        n, total = store.stats(cafe.id, today - timedelta(days=1), today)
        orders, revenue = orders + n, revenue + total
        state, _ = cafe.subscription()
        if state == "soon":
            soon.append(f"{cafe.name} — до {cafe.paid_until:%d.%m}")
        elif state == "grace":
            grace.append(f"{cafe.name} — отключится после {last_order_day(cafe.paid_until):%d.%m}")
        elif state == "off":
            off.append(f"{cafe.name} — с {last_order_day(cafe.paid_until) + timedelta(days=1):%d.%m}")
        if (bs := bot_state(cafe.id)) in ("rejected", "offline"):
            broken.append(f"{cafe.name} — {BOT_STATES[bs]}")
    month = store.income(today.replace(day=1))
    lines = [
        f"limybot, {today:%d.%m}",
        f"Вчера: {orders_word(orders)} на {money(revenue)} во всех заведениях.",
        f"Оплат за этот месяц: {money(month)}.",
    ]
    for title, items in (
        ("⏳ Скоро платить", soon),
        ("⚠️ Не оплатили, идёт отсрочка", grace),
        ("⛔ Отключены за неуплату", off),
        ("🔧 Боты не работают", broken),
    ):
        if items:
            lines += ["", title + ":", *(f"• {i}" for i in items)]
    if not (soon or grace or off or broken):
        lines += ["", "✅ Все оплатили, все боты работают."]
    lines += ["", "Панель: /panel"]
    return "\n".join(lines)


def operator_alerts(cafe: Cafe, bot_state: str) -> list[tuple[str, str, str]]:
    """(kind, key, text) for what the operator must hear about right away."""
    alerts = []
    state, _ = cafe.subscription()
    if state == "off":
        alerts.append(
            (
                "op-off",
                cafe.paid_until.isoformat(),
                f"⛔ «{cafe.name}» отключено за неуплату — бот не принимает заказы.",
            )
        )
    if bot_state == "rejected":
        key = hashlib.sha256(cafe.bot_token.encode()).hexdigest()[:16]  # once per token, not the token itself
        alerts.append(
            (
                "op-rejected",
                key,
                f"🔧 «{cafe.name}»: Telegram не принял токен бота — бот не работает. Новый токен — в панели, «Сменить бота».",
            )
        )
    return alerts


class Notifier:
    """Called by the runner every few seconds; sends whatever is due."""

    def __init__(self, store: Store, settings: Settings, bot_for: Callable[[int], Bot | None], bot_state):
        self.store, self.settings, self.bot_for, self.bot_state = store, settings, bot_for, bot_state

    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        ops = self.bot_for(OPS)
        for cafe in self.store.cafes():
            try:  # one cafe's broken bot must not silence everyone after it
                if now.astimezone(cafe.now().tzinfo).hour in REMIND_HOURS:
                    await self._owner(cafe)
                await self._summary(cafe, now)
            except Exception:
                log.exception("%s: notifications failed", cafe.slug)
            if ops:
                for kind, key, text in operator_alerts(cafe, self.bot_state(cafe.id)):
                    if not self.store.reminded(cafe.id, kind, key) and await self.to_operators(ops, text):
                        self.store.mark_reminded(cafe.id, kind, key)
        local = now.astimezone(TZ)
        key = local.date().isoformat()
        due = ops and local.hour >= DIGEST_HOUR and not self.store.reminded(OPS, "digest", key)
        if due and await self.to_operators(ops, digest(self.store, self.bot_state, now)):
            self.store.mark_reminded(OPS, "digest", key)

    async def _owner(self, cafe: Cafe) -> None:
        due = owner_reminder(self.store, self.settings, cafe)
        if not due:
            return
        kind, text = due
        key = cafe.paid_until.isoformat()
        if self.store.reminded(cafe.id, kind, key):
            return
        bot = self.bot_for(cafe.id)
        if bot and await tell_owners(self.store, bot, cafe.id, text):  # a stopped bot: try again later
            self.store.mark_reminded(cafe.id, kind, key)

    async def _summary(self, cafe: Cafe, now: datetime) -> None:
        """Every morning: yesterday in numbers, to the owner — only if there were orders."""
        local = now.astimezone(cafe.now().tzinfo)
        day = (local - timedelta(days=1)).date()
        if local.hour < SUMMARY_HOUR or self.store.reminded(cafe.id, "daily", day.isoformat()):
            return
        bot = self.bot_for(cafe.id)
        if not bot:
            return
        text = reports.daily_summary(self.store, cafe, day)
        if text is None or await tell_owners(self.store, bot, cafe.id, text):
            self.store.mark_reminded(cafe.id, "daily", day.isoformat())

    async def to_operators(self, bot: Bot, text: str) -> bool:
        sent = False
        for chat_id, _ in self.store.operators():
            with suppress(TelegramAPIError):
                await bot.send_message(chat_id, text)
                sent = True
        return sent


def build_ops(store: Store, settings: Settings, bot_state: Callable[[int], str]) -> Dispatcher:
    """The operator's service bot: joining by the link from /ops, /panel, /status."""
    r = Router()

    async def is_operator(m: Message) -> bool:  # async: sync filters run in a thread (sqlite)
        return any(chat_id == m.chat.id for chat_id, _ in store.operators())

    @r.message(CommandStart(deep_link=True, magic=F.args.startswith("op-")))
    async def join(m: Message, command: CommandObject):
        if store.add_operator(command.args[3:], m.chat.id, m.from_user.full_name if m.from_user else ""):
            await m.answer(
                "Готово: сюда будут приходить утренняя сводка по заведениям, напоминания об оплатах и тревоги, "
                "если бот заведения перестал работать.\n\n/panel — вход в панель оператора\n/status — сводка сейчас"
            )
        else:
            await m.answer("Ссылка устарела или уже использована. Сделайте новую в панели оператора.")

    @r.message(Command("panel"), is_operator)
    async def panel(m: Message):
        link = f"{settings.public_url.rstrip('/')}/ops/login/{store.create_login(OPS, OPERATOR)}"
        await m.answer(
            "Вход в панель оператора — кнопка одноразовая, 15 минут.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Открыть панель", url=link, style="primary")]]
            ),
        )

    @r.message(Command("status"), is_operator)
    async def status(m: Message):
        await m.answer(digest(store, bot_state, datetime.now(UTC)))

    @r.message(is_operator)
    async def help_(m: Message):
        await m.answer("/panel — вход в панель оператора\n/status — сводка по заведениям")

    @r.message()
    async def stranger(m: Message):
        await m.answer("Это служебный бот limybot. Подключиться можно только по ссылке из панели оператора.")

    dp = Dispatcher()
    dp.include_router(r)
    return dp
