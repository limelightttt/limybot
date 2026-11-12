"""News from a cafe to its guests: a text the owner writes in the admin goes to everyone who opened the cafe's bot and
did not opt out. Sent in the background at a pace Telegram accepts (about 20 messages a second, waiting when it asks
to), with "🔕 don't send news" under every message. Guests who blocked the bot are muted, so they are not retried.
Progress is kept in the broadcasts table and shown in the admin."""

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter

from . import i18n
from .bot import button, customer_lang, kb
from .store import Store

log = logging.getLogger(__name__)
PACE = 0.05  # seconds between messages: ~20 a second, under Telegram's 30
PER_DAY = 2  # broadcasts a cafe may send in 24 hours: news, not spam — guests who get spam block the bot
MAX_LEN = 1000
running: set[asyncio.Task] = set()  # kept so a task is not collected mid-way


async def send(bot: Bot, store: Store, cafe_id: int, broadcast_id: int, text: str, users: list[int]) -> None:
    sent = failed = 0
    try:
        sent, failed = await _send_all(bot, store, cafe_id, broadcast_id, text, users)
    finally:  # whatever happens, the admin must not show "sending…" forever
        store.broadcast_progress(cafe_id, broadcast_id, sent, failed, "done")
    log.info("broadcast %s: %s sent, %s failed", broadcast_id, sent, failed)


async def _send_all(bot: Bot, store: Store, cafe_id: int, broadcast_id: int, text: str, users: list[int]):
    sent = failed = 0
    for n, user_id in enumerate(users, 1):
        lang = customer_lang(store, cafe_id, user_id)
        markup = kb([[button(i18n.t(lang, "news_mute"), "news:off")]])
        for _ in range(3):
            try:
                await bot.send_message(user_id, text, reply_markup=markup)
                sent += 1
                break
            except TelegramRetryAfter as e:  # Telegram says: slow down
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:  # blocked the bot: never again
                store.set_muted(cafe_id, user_id, True)
                failed += 1
                break
            except (TelegramBadRequest, TelegramNetworkError):
                failed += 1
                break
        else:
            failed += 1
        if n % 25 == 0:
            store.broadcast_progress(cafe_id, broadcast_id, sent, failed, "sending")
        await asyncio.sleep(PACE)
    return sent, failed


def start(bot: Bot, store: Store, cafe_id: int, text: str) -> int:
    """Records the broadcast and sends it in the background; returns its id."""
    users = store.audience(cafe_id)
    broadcast_id = store.add_broadcast(cafe_id, text, len(users))
    task = asyncio.create_task(send(bot, store, cafe_id, broadcast_id, text, users))
    running.add(task)
    task.add_done_callback(running.discard)
    return broadcast_id
