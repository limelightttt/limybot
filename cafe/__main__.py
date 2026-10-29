"""python -m cafe [run]                                  — all bots + web admin
python -m cafe add <name> --token <TOKEN> [--from clients/x.json] — connect a cafe
python -m cafe token <name> <TOKEN>                    — replace a cafe's bot token
python -m cafe invite <name>                           — new link that makes its opener an admin
python -m cafe paid <name> <YYYY-MM-DD>                — subscription paid until that day (inclusive)
python -m cafe disable|enable <name>                   — stop / resume taking orders, e.g. for non-payment
python -m cafe backup                                  — back up the database and photos now
python -m cafe list"""

import argparse
import asyncio
import logging
import re
import sqlite3
from datetime import date
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from pydantic import ValidationError

from .backup import make_backup
from .config import Settings
from .runner import serve
from .store import Store
from .template import Template, import_cafe, load_template


async def bot_username(token: str) -> str:
    bot = Bot(token)
    try:
        return (await bot.get_me()).username
    finally:
        await bot.session.close()


def check_token(token: str) -> str:
    try:
        return asyncio.run(bot_username(token))
    except TelegramUnauthorizedError:
        raise SystemExit("Telegram не принял токен — скопируйте его у @BotFather ещё раз")
    except TelegramNetworkError as e:
        raise SystemExit(f"Нет связи с Telegram, проверьте интернет: {e}")


def print_invite(store: Store, cafe_id: int) -> None:
    store.new_invite(cafe_id)
    cafe = store.cafe(cafe_id)
    print(f"Владелец «{cafe.name}» открывает эту ссылку в Telegram и становится администратором (один раз):")
    print(cafe.invite_link())


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m cafe", description="limybot: Telegram-боты заказов для кафе")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run", help="запустить всех ботов и веб-админку (по умолчанию)")
    add = sub.add_parser("add", help="подключить новое кафе")
    add.add_argument("name", help="короткое латинское имя, например coffee_house")
    add.add_argument("--token", required=True, help="токен бота от @BotFather")
    add.add_argument("--from", dest="template", type=Path, help="JSON с настройками и меню, см. clients/")
    tok = sub.add_parser("token", help="сменить токен бота кафе")
    tok.add_argument("name")
    tok.add_argument("token")
    inv = sub.add_parser("invite", help="новая ссылка, которая делает открывшего администратором")
    inv.add_argument("name")
    paid = sub.add_parser("paid", help="подписка оплачена до даты (включительно)")
    paid.add_argument("name")
    paid.add_argument("until", type=date.fromisoformat, help="ГГГГ-ММ-ДД")
    for cmd, text in (("disable", "приостановить приём заказов"), ("enable", "возобновить приём заказов")):
        sub.add_parser(cmd, help=text).add_argument("name")
    sub.add_parser("backup", help="сделать бэкап базы и фото сейчас")
    sub.add_parser("list", help="список кафе")
    args = p.parse_args()

    settings = Settings()
    if args.cmd in (None, "run"):
        logging.basicConfig(level=logging.INFO)
        asyncio.run(serve(settings))
        return

    store = Store(settings.db_path)
    if args.cmd == "list":
        for c in store.cafes():
            state = "работает" if c.active() else "НЕ принимает заказы"
            until = f"до {c.paid_until:%d.%m.%Y}" if c.paid_until else "без срока"
            print(f"{c.slug:20} {c.name:28} @{c.bot_username or '?':24} {until:14} {state}")
        return
    if args.cmd == "backup":
        print(asyncio.run(make_backup(store, settings, date.today())))
        return

    if args.cmd == "add":
        if not re.fullmatch(r"[a-z0-9_-]+", args.name):
            raise SystemExit("Имя кафе: латиница в нижнем регистре, цифры, _ и -")
        try:
            template = load_template(args.template) if args.template else Template(name=args.name)
        except ValidationError as e:
            raise SystemExit(f"{args.template}: {e}")
        username = check_token(args.token)
        try:
            cafe_id = import_cafe(store, args.name, args.token, template)
        except sqlite3.IntegrityError:
            raise SystemExit("Кафе с таким именем или токеном уже есть: python -m cafe list")
        store.set_bot_username(cafe_id, username)
        print(f"Кафе «{template.name}» подключено к @{username}.")
        print_invite(store, cafe_id)
        return

    cafe = store.cafe_by_slug(args.name)
    if not cafe:
        raise SystemExit(f"Нет кафе «{args.name}»: python -m cafe list")
    if args.cmd == "token":
        username = check_token(args.token)
        store.set_token(cafe.id, args.token)
        store.set_bot_username(cafe.id, username)
        print(f"Токен обновлён: @{username}. Запущенный процесс переключится сам в течение 15 секунд.")
    elif args.cmd == "invite":
        print_invite(store, cafe.id)
    elif args.cmd == "paid":
        store.set_paid_until(cafe.id, args.until)
        print(f"«{cafe.name}»: подписка до {args.until:%d.%m.%Y}.")
    elif args.cmd in ("disable", "enable"):
        store.set_enabled(cafe.id, args.cmd == "enable")
        print(f"«{cafe.name}»: {'приём заказов возобновлён' if args.cmd == 'enable' else 'приём заказов остановлен'}.")


if __name__ == "__main__":
    main()
