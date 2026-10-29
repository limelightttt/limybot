import asyncio
import logging

from aiogram import Bot

from .bot import build
from .config import Settings, load_client
from .store import Store


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is empty: put the token from @BotFather into .env")
    client = load_client(settings.client)
    dp = build(settings, Store(settings.db_path, client))
    await dp.start_polling(Bot(settings.bot_token))


if __name__ == "__main__":
    asyncio.run(main())
