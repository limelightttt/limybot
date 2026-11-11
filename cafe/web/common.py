"""What every part of the web app shares: the store, the running bots, templates and the admin login check.
There are no passwords: /admin in the bot sends a one-time link that opens a session for that cafe."""

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

from aiogram import Bot
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import Settings
from ..models import Cafe
from ..store import OPERATOR, Store

COOKIE = "sid"  # a cafe admin session
OPS_COOKIE = "ops"  # the operator's session, kept apart: the operator also opens cafe admins


class Bots(Protocol):
    """The running bots (runner.Runner); tests pass a fake."""

    def bot(self, cafe_id: int) -> Bot | None: ...  # None while the bot is down

    def state(self, cafe_id: int) -> str: ...  # a key of models.BOT_STATES

    async def check(self, token: str) -> tuple[str, str]: ...  # (username, name) or runner.TokenError

    async def sync(self) -> None: ...  # start and stop bots after the cafe list changed


class LoginRequired(Exception):
    """No cafe admin session: the page explains /admin in the bot."""


class OpsLoginRequired(Exception):
    """No operator session: the page explains where the operator's link comes from."""


@dataclass
class Web:
    store: Store
    settings: Settings
    bots: Bots
    templates: Jinja2Templates

    async def admin(self, request: Request) -> tuple[Cafe, int]:  # async: sync dependencies run in a thread
        """FastAPI dependency: the cafe and the user of the admin session, or the login page."""
        s = self.store.session(request.cookies.get(COOKIE, ""))
        cafe = self.store.cafe(s[0]) if s else None
        if not cafe or not (s[1] == OPERATOR or self.store.is_admin(*s)):
            raise LoginRequired
        return cafe, s[1]

    def page(self, request: Request, name: str, cafe: Cafe, **ctx) -> HTMLResponse:
        return self.templates.TemplateResponse(
            request, f"admin/{name}", {"cafe": cafe, "msg": request.query_params.get("msg"), **ctx}
        )


def secure_cookie(request: Request, settings: Settings) -> bool:
    """Https-only cookies, except when the page was opened on this computer by http://localhost (`cafe ops` prints such
    a link): Safari does not keep https-only cookies there, and the login would silently fail."""
    return settings.public_url.startswith("https") and request.url.hostname not in ("localhost", "127.0.0.1")


def back(url: str, msg: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(url + (f"{sep}msg={quote(msg)}" if msg else ""), status_code=303)
