"""One process for all cafes: a polling Dispatcher per cafe bot plus the web admin.
The cafe list is re-read periodically, so a cafe added with `python -m cafe add` starts without a restart."""

import asyncio
import logging
from dataclasses import dataclass

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramServerError, TelegramUnauthorizedError

from .bot import build
from .config import Settings
from .store import Store
from .web import create_app

log = logging.getLogger(__name__)
SYNC_EVERY = 15  # seconds


@dataclass
class Running:
    token: str
    bot: Bot
    dp: Dispatcher
    task: asyncio.Task


class Runner:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
        self.running: dict[int, Running] = {}
        self.rejected: dict[int, str] = {}  # cafe id → token Telegram refused; retried once the token changes

    def bot(self, cafe_id: int) -> Bot | None:
        r = self.running.get(cafe_id)
        return r.bot if r else None

    async def sync(self) -> None:
        cafes = {c.id: c for c in self.store.cafes()}
        for cafe_id in list(self.running):
            if cafe_id not in cafes or cafes[cafe_id].bot_token != self.running[cafe_id].token:
                await self.stop(cafe_id)
        for cafe in cafes.values():
            if cafe.id not in self.running and self.rejected.get(cafe.id) != cafe.bot_token:
                await self.start(cafe.id, cafe.slug, cafe.bot_token)

    async def start(self, cafe_id: int, slug: str, token: str) -> None:
        bot = Bot(token)
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            log.error(
                "%s: Telegram rejected the bot token — set a new one: python -m cafe token %s <TOKEN>", slug, slug
            )
            self.rejected[cafe_id] = token
            await bot.session.close()
            return
        except (TelegramNetworkError, TelegramServerError) as e:  # try again on the next sync
            log.warning("%s: cannot reach Telegram (%s), will retry", slug, e)
            await bot.session.close()
            return
        self.store.set_bot_username(cafe_id, me.username)
        dp = build(self.store, cafe_id, self.settings)
        task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        self.running[cafe_id] = Running(token, bot, dp, task)
        log.info("%s: @%s is running", slug, me.username)

    async def stop(self, cafe_id: int) -> None:
        r = self.running.pop(cafe_id)
        await r.dp.stop_polling()
        await r.task

    async def run(self) -> None:
        await self.sync()
        if not self.store.cafes():
            log.warning("No cafes yet. Add one: python -m cafe add <name> --token <TOKEN> --from <json>")
        while True:
            await asyncio.sleep(SYNC_EVERY)
            await self.sync()


async def serve(settings: Settings) -> None:
    store = Store(settings.db_path)
    runner = Runner(store, settings)
    app = create_app(store, settings, runner.bot)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.web_host, port=settings.web_port, log_level="info"))
    bots = asyncio.create_task(runner.run())
    try:
        await server.serve()  # until Ctrl+C
    finally:
        bots.cancel()
        for cafe_id in list(runner.running):
            await runner.stop(cafe_id)
