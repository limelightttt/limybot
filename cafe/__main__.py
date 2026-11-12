"""python -m cafe [run] [--open]                         — all bots, the admins, the operator panel and the Mini Apps;
                                                         --open also opens the panel in this machine's browser
python -m cafe ops                                     — a login link to the operator panel (/ops)
python -m cafe add --token <TOKEN> [--from clients/x.json] — connect a cafe (the panel does the same)
python -m cafe remove <name>                           — delete a cafe and everything of it
python -m cafe token <name> <TOKEN>                    — replace a cafe's bot token
python -m cafe invite <name>                           — new link that makes its opener an admin
python -m cafe paid <name> <YYYY-MM-DD>                — subscription paid until that day (inclusive)
python -m cafe disable|enable <name>                   — stop / resume taking orders, e.g. for non-payment
python -m cafe backup                                  — back up the database and photos now
python -m cafe list"""

import argparse
import asyncio
import logging
from datetime import date
from pathlib import Path

from .backup import make_backup
from .config import Settings
from .media import drop_photo
from .runner import serve
from .store import OPERATOR, OPS, Store
from .template import connect_cafe, load_template
from .tokens import TokenError, bot_profile


def check_token(token: str) -> tuple[str, str]:
    try:
        return asyncio.run(bot_profile(token))
    except TokenError as e:
        raise SystemExit(str(e))


def print_invite(store: Store, cafe_id: int) -> None:
    cafe = store.cafe(cafe_id)
    print(f"Владелец «{cafe.name}» открывает эту ссылку в Telegram и становится администратором (один раз):")
    print(f"  {cafe.invite_link()}")


def public_url(settings: Settings) -> str:
    """Where the running process serves the panel. With an automatic tunnel the address is known only to it."""
    if settings.public_url.startswith("https://") and "trycloudflare.com" not in settings.public_url:
        return settings.public_url.rstrip("/")
    return f"http://localhost:{settings.web_port}"


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m cafe", description="limybot: Telegram-боты заказов для кафе")
    sub = p.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="запустить всех ботов, админки и панель оператора (по умолчанию)")
    run.add_argument("--open", action="store_true", help="сразу открыть панель оператора в браузере")
    sub.add_parser("ops", help="ссылка для входа в панель оператора")
    add = sub.add_parser("add", help="подключить новое кафе (то же есть в панели оператора)")
    add.add_argument("--token", required=True, help="токен бота от @BotFather")
    add.add_argument("--from", dest="template", type=Path, help="JSON с настройками и меню, см. clients/")
    rm = sub.add_parser("remove", help="удалить кафе со всеми данными")
    rm.add_argument("name")
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
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
        logging.getLogger("aiogram").setLevel(logging.WARNING)  # "update handled" per message drowns what matters
        try:
            asyncio.run(serve(settings, open_panel=getattr(args, "open", False)))
        except KeyboardInterrupt:
            print("\nlimybot остановлен.")
        return

    store = Store(settings.db_path)
    if args.cmd == "ops":
        print("Панель оператора (ссылка одноразовая, на 15 минут; limybot должен быть запущен):")
        print(f"  {public_url(settings)}/ops/login/{store.create_login(OPS, OPERATOR)}")
        return
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
        template = None
        if args.template:
            if not args.template.is_file():
                raise SystemExit(f"Нет файла {args.template}. Проверьте путь — он считается от папки limybot.")
            try:
                template = load_template(args.template)
            except ValueError as e:  # also pydantic's ValidationError
                raise SystemExit(f"{args.template} — не получилось прочитать меню: {e}")
        if taken := store.cafe_by_token(args.token.strip()):
            raise SystemExit(
                f"Этот бот уже подключён — «{taken.name}» ({taken.slug}). Удалить: python -m cafe remove {taken.slug}"
            )
        username, name = check_token(args.token)
        cafe_id = connect_cafe(store, settings.media_dir, args.token.strip(), username, name, template)
        print(f"Готово: «{store.cafe(cafe_id).name}» подключено к @{username}. Запущенный limybot подхватит его сам.")
        print_invite(store, cafe_id)
        return

    cafe = store.cafe_by_slug(args.name)
    if not cafe:
        raise SystemExit(f"Нет кафе «{args.name}»: python -m cafe list")
    if args.cmd == "remove":
        answer = input(f"Удалить «{cafe.name}» со всем меню, фото и заказами? Впишите {cafe.slug}: ")
        if answer.strip() != cafe.slug:
            raise SystemExit("Не удалено.")
        for photo in store.delete_cafe(cafe.id):
            drop_photo(settings.media_dir, photo)
        print(f"«{cafe.name}» удалено, бот @{cafe.bot_username} свободен.")
    elif args.cmd == "token":
        username, _ = check_token(args.token)
        store.set_token(cafe.id, args.token)
        store.set_bot_username(cafe.id, username)
        print(f"Токен обновлён: @{username}. Запущенный процесс переключится сам в течение 15 секунд.")
    elif args.cmd == "invite":
        store.new_invite(cafe.id)
        print_invite(store, cafe.id)
    elif args.cmd == "paid":
        store.set_paid_until(cafe.id, args.until)
        print(f"«{cafe.name}»: подписка до {args.until:%d.%m.%Y}.")
    elif args.cmd in ("disable", "enable"):
        store.set_enabled(cafe.id, args.cmd == "enable")
        print(f"«{cafe.name}»: {'приём заказов возобновлён' if args.cmd == 'enable' else 'приём заказов остановлен'}.")


if __name__ == "__main__":
    main()
