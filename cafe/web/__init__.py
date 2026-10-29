"""The web side, one module per feature: the cafe owner's admin (orders, menu, settings, promo) and the customers'
Mini App. Everything is rendered on the server with Jinja; the Mini App adds one plain JS file. No build step."""

import pathlib
from collections.abc import Callable

from aiogram import Bot
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..bot import BUTTON
from ..config import Settings
from ..media import ensure_thumbs, thumb
from ..models import PAYMENT_METHODS, PAYMENT_STATUSES, STATUSES
from ..store import SESSION_TTL, Store
from . import cafe_settings, menu, miniapp, orders, promo
from .common import COOKIE, LoginRequired, Web

HERE = pathlib.Path(__file__).parent


def create_app(store: Store, settings: Settings, bot_for: Callable[[int], Bot | None]) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    media = settings.media_dir
    media.mkdir(parents=True, exist_ok=True)
    ensure_thumbs(media)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.mount("/media", StaticFiles(directory=media), name="media")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        ACTIONS=BUTTON,
        STATUSES=STATUSES,
        PAYMENT_METHODS=PAYMENT_METHODS,
        PAYMENT_STATUSES=PAYMENT_STATUSES,
        SUPPORT=settings.support,
    )
    templates.env.filters["thumb"] = thumb
    web = Web(store, settings, bot_for, templates)

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, _: LoginRequired):
        return templates.TemplateResponse(request, "admin/login.html", {"cafe": None}, status_code=401)

    @app.get("/login/{token}")
    async def login(request: Request, token: str):
        session = store.consume_login(token)
        if not session:
            raise LoginRequired
        resp = RedirectResponse("/orders", status_code=303)
        resp.set_cookie(
            COOKIE,
            session,
            max_age=SESSION_TTL,
            httponly=True,
            samesite="lax",  # also our CSRF protection: cross-site form posts arrive without the cookie
            secure=settings.public_url.startswith("https"),
        )
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        store.drop_session(request.cookies.get(COOKIE, ""))
        resp = RedirectResponse("/orders", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/")
    async def home():
        return RedirectResponse("/orders", status_code=303)

    for feature in (orders, menu, cafe_settings, promo, miniapp):
        app.include_router(feature.router(web))
    return app
