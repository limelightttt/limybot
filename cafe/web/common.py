"""What every part of the web app shares: the store, the running bots, templates and the admin login check.
There are no passwords: /admin in the bot sends a one-time link that opens a session for that cafe."""

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

from aiogram import Bot
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import Settings
from ..models import Cafe
from ..store import Store

COOKIE = "sid"


class LoginRequired(Exception):
    pass


@dataclass
class Web:
    store: Store
    settings: Settings
    bot_for: Callable[[int], Bot | None]  # the running bot of a cafe, None while it is down
    templates: Jinja2Templates

    async def admin(self, request: Request) -> tuple[Cafe, int]:  # async: sync dependencies run in a thread
        """FastAPI dependency: the cafe and the user of the admin session, or the login page."""
        s = self.store.session(request.cookies.get(COOKIE, ""))
        if not s or not self.store.is_admin(*s):
            raise LoginRequired
        return self.store.cafe(s[0]), s[1]

    def page(self, request: Request, name: str, cafe: Cafe, **ctx) -> HTMLResponse:
        return self.templates.TemplateResponse(
            request, f"admin/{name}", {"cafe": cafe, "msg": request.query_params.get("msg"), **ctx}
        )


def back(url: str, msg: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(url + (f"{sep}msg={quote(msg)}" if msg else ""), status_code=303)
