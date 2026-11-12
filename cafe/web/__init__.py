"""The web side, one module per feature: the cafe owner's admin (orders, menu, settings, promo), the operator panel
(ops) and the customers' Mini App. Everything is rendered on the server with Jinja; the Mini App adds one plain JS file. No build step."""

import pathlib

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import notify
from ..bot import BUTTON
from ..config import Settings
from ..media import ensure_thumbs, thumb
from ..models import APP_THEMES, PAYMENT_METHODS, PAYMENT_STATUSES, STATUSES, Cafe
from ..store import OPS, SESSION_TTL, Store
from . import cafe_settings, menu, miniapp, ops, orders, promo, reports
from .common import COOKIE, Bots, LoginRequired, OpsLoginRequired, Web, secure_cookie

HERE = pathlib.Path(__file__).parent


def notice(store: Store, settings: Settings, cafe: Cafe) -> tuple[str, str] | None:
    """The banner over the owner's admin about the subscription: (flash class, text)."""
    if cafe.subscription()[0] == "paused":
        who = notify.contact(store, settings)
        return "danger", "Приём заказов приостановлен." + (f" Вопросы — {who}." if who else "")
    due = notify.owner_reminder(store, settings, cafe)
    if not due:
        return None
    kind, text = due
    return ("danger" if kind in ("grace", "off") else ""), text


def create_app(store: Store, settings: Settings, bots: Bots) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    media = settings.media_dir
    media.mkdir(parents=True, exist_ok=True)
    ensure_thumbs(media)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.mount("/media", StaticFiles(directory=media), name="media")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        ACTIONS=BUTTON,
        APP_THEMES=APP_THEMES,
        STATUSES=STATUSES,
        PAYMENT_METHODS=PAYMENT_METHODS,
        PAYMENT_STATUSES=PAYMENT_STATUSES,
        SUBSCRIPTION_NOTICE=lambda cafe: notice(store, settings, cafe),
    )
    templates.env.filters["thumb"] = thumb
    web = Web(store, settings, bots, templates)

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, _: LoginRequired):
        return templates.TemplateResponse(request, "admin/login.html", {"cafe": None}, status_code=401)

    @app.exception_handler(OpsLoginRequired)
    async def ops_login_required(request: Request, _: OpsLoginRequired):
        return templates.TemplateResponse(request, "ops/login.html", {}, status_code=401)

    @app.get("/login/{token}")
    async def login_page(request: Request, token: str):
        """Opening the link only shows "Войти" (sent by itself in a browser): a messenger's preview must not use it up."""
        who = store.peek_login(token)
        if not who or who[0] == OPS:
            raise LoginRequired
        return templates.TemplateResponse(request, "enter.html", {"title": "Вход в админку"})

    @app.post("/login/{token}")
    async def login(request: Request, token: str):
        who = store.consume_login(token)
        if not who or who[0] == OPS or not store.is_admin(*who):
            raise LoginRequired
        resp = RedirectResponse("/orders", status_code=303)
        resp.set_cookie(
            COOKIE,
            store.create_session(*who),
            max_age=SESSION_TTL,
            httponly=True,
            samesite="lax",  # also our CSRF protection: cross-site form posts arrive without the cookie
            secure=secure_cookie(request, settings),
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

    for feature in (orders, menu, cafe_settings, promo, reports, ops, miniapp):
        app.include_router(feature.router(web))
    return app
