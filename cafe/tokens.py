"""Checking a bot token the operator pastes: the CLI, the operator panel and the runner share it."""

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramServerError, TelegramUnauthorizedError
from aiogram.utils.token import TokenValidationError


class TokenError(Exception):
    """A bot token that cannot be used; the message says what to do, in Russian."""


async def bot_profile(token: str) -> tuple[str, str]:
    """(username, display name) of the bot, or TokenError."""
    try:
        bot = Bot(token.strip())
    except TokenValidationError:
        raise TokenError("Это не токен бота. Он выглядит так: 123456789:AAH… — скопируйте его у @BotFather целиком")
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        raise TokenError(
            "Telegram не принял токен: его могли перевыпустить. Свежий — в @BotFather: /mybots → бот → API Token"
        )
    except (TelegramNetworkError, TelegramServerError):
        raise TokenError("Нет связи с Telegram — проверьте интернет и попробуйте ещё раз")
    finally:
        await bot.session.close()
    return me.username, me.first_name
