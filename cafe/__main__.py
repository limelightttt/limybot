"""python -m cafe [run]                                  — all bots + web admin
python -m cafe add <name> --token <TOKEN> [--from clients/x.json] — connect a cafe
python -m cafe token <name> <TOKEN>                    — replace a cafe's bot token
python -m cafe invite <name>                           — new link that makes its opener an admin
python -m cafe list"""

import argparse
import asyncio
import logging
import re
import sqlite3
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from pydantic import ValidationError

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
            print(f"{c.slug:20} {c.name:30} @{c.bot_username or '?':25} админов: {len(store.admins(c.id))}")
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


if __name__ == "__main__":
    main()
