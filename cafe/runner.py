"""One process for all cafes: a polling Dispatcher per cafe bot plus the web side (owner admin, operator panel,
Mini App). The cafe list is re-read every few seconds and right after the operator changes it, so cafes start and
stop without a restart. Once a day the database and photos are backed up (backup.py)."""

import asyncio
import logging
import socket
import webbrowser
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
from aiogram.utils.token import TokenValidationError

from .backup import make_backup
from .bot import app_url, build
from .config import Settings
from .i18n import LANGS
from .models import BOT_STATES
from .notify import Notifier, build_ops
from .store import OPERATOR, OPS, Store
from .tokens import bot_profile
from .tunnel import close_tunnel, needs_tunnel, open_tunnel
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
        self.problems: dict[int, tuple[str, str]] = {}  # cafe id → (state, token it happened with)
        self.lock = asyncio.Lock()  # the periodic sync and the operator panel must not start one bot twice
        self.backed_up: date | None = None
        self.notifier = Notifier(store, settings, self.bot, self.state)

    # --- what the web side uses (web.common.Bots) ---
    def bot(self, cafe_id: int) -> Bot | None:
        r = self.running.get(cafe_id)
        return r.bot if r else None

    def token(self, cafe_id: int) -> str | None:
        """What the bot should run with now; OPS is the operator's service bot (set in /ops)."""
        if cafe_id == OPS:
            return self.store.meta("ops_bot_token")
        cafe = self.store.cafe(cafe_id)
        return cafe.bot_token if cafe else None

    def state(self, cafe_id: int) -> str:
        if cafe_id in self.running:
            return "running"
        token = self.token(cafe_id)
        problem = self.problems.get(cafe_id)
        return problem[0] if token and problem and problem[1] == token else "starting"

    async def check(self, token: str) -> tuple[str, str]:
        return await bot_profile(token)

    async def sync(self) -> None:
        async with self.lock:
            wanted = {c.id: (c.slug, c.bot_token) for c in self.store.cafes()}
            if ops := self.store.meta("ops_bot_token"):
                wanted[OPS] = ("служебный бот", ops)
            for cafe_id in list(self.running):
                if cafe_id not in wanted or wanted[cafe_id][1] != self.running[cafe_id].token:
                    await self.stop(cafe_id)
            for cafe_id, (slug, token) in wanted.items():
                if cafe_id not in self.running and self.problems.get(cafe_id) != ("rejected", token):
                    await self.start(cafe_id, slug, token)

    # --- bots ---
    async def start(self, cafe_id: int, slug: str, token: str) -> None:
        try:
            bot = Bot(token)
        except TokenValidationError:
            log.error("%s: в базе не токен бота — смените его в панели оператора", slug)
            self.problems[cafe_id] = ("rejected", token)
            return
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            log.error("%s: Telegram не принял токен — смените его в панели оператора", slug)
            self.problems[cafe_id] = ("rejected", token)
            await bot.session.close()
            return
        except (TelegramNetworkError, TelegramServerError) as e:  # try again on the next sync
            if self.problems.get(cafe_id) != ("offline", token):
                log.warning("%s: нет связи с Telegram (%s), пробую снова", slug, e)
            self.problems[cafe_id] = ("offline", token)
            await bot.session.close()
            return
        self.problems.pop(cafe_id, None)
        if cafe_id == OPS:
            self.store.set_meta("ops_bot_username", me.username)
        else:
            self.store.set_bot_username(cafe_id, me.username)
        try:
            await self.setup_chat(bot, cafe_id)
        except (TelegramBadRequest, TelegramNetworkError, TelegramServerError) as e:  # cosmetic: the bot works anyway
            log.warning("%s: cannot set the menu button and commands: %s", slug, e)
        dp = (
            build_ops(self.store, self.settings, self.state)
            if cafe_id == OPS
            else build(self.store, cafe_id, self.settings)
        )
        task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        self.running[cafe_id] = Running(token, bot, dp, task)
        log.info("%s: @%s работает", slug, me.username)

    async def setup_chat(self, bot: Bot, cafe_id: int) -> None:
        """The "Menu" button next to the input field opens the Mini App; the command list in both languages."""
        if cafe_id == OPS:
            await bot.set_my_commands(
                [
                    BotCommand(command="panel", description="Вход в панель"),
                    BotCommand(command="status", description="Сводка"),
                ]
            )
            return
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
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Меню", web_app=WebAppInfo(url=url)))

    async def stop(self, cafe_id: int) -> None:
        r = self.running.pop(cafe_id)
        await r.dp.stop_polling()
        await r.task

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

    def summary(self) -> str:
        """What the operator sees in the terminal after the start: what works and where to go."""
        url = self.settings.public_url.rstrip("/")
        lines = [
            "─" * 64,
            "limybot работает на сервере."
            if self.settings.server
            else "limybot работает. Не закрывайте это окно (остановить — Ctrl+C).",
            "",
            "Панель оператора — подключать заведения, смотреть, что работает",
            "(ссылка одноразовая, на 15 минут; потом вход запомнится в браузере):",
            f"  {url}/ops/login/{self.store.create_login(OPS, OPERATOR)}",
            "",
            "Заведения:",
        ]
        for cafe in self.store.cafes():
            state = self.state(cafe.id)
            mark = "✅" if state == "running" and cafe.active() else "⚠️ "
            why = BOT_STATES[state] if state != "running" else ("работает" if cafe.active() else "не принимает заказы")
            lines.append(f"  {mark} {cafe.name} — @{cafe.bot_username or '?'}: {why}")
        if not self.store.cafes():
            lines.append("  пока нет — откройте панель и нажмите «Подключить заведение»")
        lines.append("")
        if url.endswith(".trycloudflare.com"):
            lines += [
                f"Витрина открывается через временный адрес {url}",
                "При каждом запуске он новый: в старых сообщениях бота кнопка «Открыть меню» не сработает —",
                "отправьте боту /start, и придёт свежая.",
            ]
        elif not url.startswith("https://"):
            lines += [
                "⚠️  Нет https-адреса: бот отвечает, но Telegram не откроет витрину (меню).",
                "   На своём компьютере установите cloudflared (Mac: brew install cloudflared) и перезапустите.",
            ]
        lines.append("─" * 64)
        return "\n".join(lines)

    def panel_link(self) -> str:
        """For the browser on this very machine: localhost works at once, a fresh tunnel may need a few seconds."""
        return f"http://localhost:{self.settings.web_port}/ops/login/{self.store.create_login(OPS, OPERATOR)}"

    async def run(self, tunnel: asyncio.subprocess.Process | None = None, open_panel: bool = False) -> None:
        await self.sync()
        print(self.summary(), flush=True)
        if open_panel:
            webbrowser.open(self.panel_link())
        while True:
            await self.backup()
            try:
                await self.notifier.tick()
            except Exception:  # a reminder that failed must never stop the bots; it is retried on the next tick
                log.exception("notifications failed")
            await asyncio.sleep(SYNC_EVERY)
            if tunnel and tunnel.returncode is not None:
                log.error("Туннель cloudflared остановился — витрина не открывается. Перезапустите ./run.sh")
                tunnel = None
            await self.sync()


def port_is_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


async def serve(settings: Settings, open_panel: bool = False) -> None:
    if not port_is_free(settings.web_host, settings.web_port):
        raise SystemExit(
            f"Порт {settings.web_port} занят — похоже, limybot уже запущен в другом окне Терминала.\n"
            "Закройте его (Ctrl+C в том окне) и запустите снова."
        )
    tunnel = None
    if needs_tunnel(settings.public_url):
        found = await open_tunnel(settings.web_port)
        if found:
            tunnel, settings.public_url = found
        else:
            settings.public_url = f"http://localhost:{settings.web_port}"
    store = Store(settings.db_path)
    runner = Runner(store, settings)
    app = create_app(store, settings, runner)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.web_host, port=settings.web_port, log_level="warning"))
    bots = asyncio.create_task(runner.run(tunnel, open_panel))
    try:
        await server.serve()  # until Ctrl+C
    finally:
        bots.cancel()
        for cafe_id in list(runner.running):
            await runner.stop(cafe_id)
        if tunnel:
            await close_tunnel(tunnel)
