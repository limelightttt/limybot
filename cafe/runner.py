"""One process for all cafes: a polling Dispatcher per cafe bot plus the web admin and the Mini App.
The cafe list is re-read periodically, so a cafe added with `python -m cafe add` starts without a restart.
Once a day the database and photos are backed up (backup.py)."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

from .backup import make_backup
from .bot import app_url, build, close_stale, daily_report
from .config import Settings
from .i18n import LANGS
from .store import Store
from .web import create_app

log = logging.getLogger(__name__)
SYNC_EVERY = 15  # seconds
AUTO_CLOSE_HOURS = 6  # ready / on the way longer than this: handed over, nobody pressed the button


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
        self.backed_up: date | None = None

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
        try:
            await self.setup_chat(bot, cafe_id)
        except (TelegramBadRequest, TelegramNetworkError, TelegramServerError) as e:  # cosmetic: the bot works anyway
            log.warning("%s: cannot set the menu button and commands: %s", slug, e)
        dp = build(self.store, cafe_id, self.settings)
        task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        self.running[cafe_id] = Running(token, bot, dp, task)
        log.info("%s: @%s is running", slug, me.username)

    async def setup_chat(self, bot: Bot, cafe_id: int) -> None:
        """The "Menu" button next to the input field opens the Mini App; the command list in both languages."""
        commands = {
            "ru": [
                BotCommand(command="start", description="Меню"),
                BotCommand(command="orders", description="Мои заказы"),
            ],
            "uz": [
                BotCommand(command="start", description="Menyu"),
                BotCommand(command="orders", description="Buyurtmalarim"),
            ],
        }
        for lang in LANGS:
            await bot.set_my_commands(commands[lang], language_code=None if lang == "ru" else lang)
        if url := app_url(self.settings, self.store.cafe(cafe_id)):
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Menyu · Меню", web_app=WebAppInfo(url=url))
            )

    async def backup(self) -> None:
        today = date.today()
        if self.backed_up != today:
            try:
                path = await make_backup(self.store, self.settings, today)
            except OSError:
                log.exception("backup failed, will retry")  # e.g. a full disk: the bots must keep running
                return
            self.backed_up = today
            log.info("backup: %s", path)

    async def close_stale(self) -> None:
        for cafe_id, r in list(self.running.items()):
            if cafe := self.store.cafe(cafe_id):
                await close_stale(r.bot, self.store, cafe, AUTO_CLOSE_HOURS)
                await daily_report(r.bot, self.store, cafe)

    async def stop(self, cafe_id: int) -> None:
        r = self.running.pop(cafe_id)
        await r.dp.stop_polling()
        await r.task

    async def run(self) -> None:
        if not self.settings.public_url.startswith("https://"):
            log.warning("PUBLIC_URL is not https: Telegram will not open the Mini App (see README, «Локально»)")
        await self.sync()
        if not self.store.cafes():
            log.warning("No cafes yet. Add one: python -m cafe add <name> --token <TOKEN> --from <json>")
        while True:
            await self.backup()
            await self.close_stale()
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
