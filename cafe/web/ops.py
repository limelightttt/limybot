"""The operator panel /ops — for whoever runs this installation and sells it to cafes. Everything that used to need
the command line: connect a cafe (bot token + a menu template or a zip pack), see whether each bot works, send the
owner their invite link, subscription, pause, a new token, deleting, and opening any cafe's admin to fill it in.
The login link is printed by ./run.sh at start (or `python -m cafe ops`)."""

import secrets
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse

from .. import notify
from ..billing import GRACE_DAYS, SUBSCRIPTION, TRIAL_DAYS, last_order_day
from ..config import ROOT
from ..media import BadImage, drop_photo
from ..models import BOT_STATES, MAX_MONEY, Cafe
from ..store import OPERATOR, OPS, SESSION_TTL
from ..template import available_templates, connect_cafe, load_template, unpack_pack
from ..tokens import TokenError
from .common import COOKIE, OPS_COOKIE, OpsLoginRequired, Web, back, secure_cookie

CLIENTS = ROOT / "clients"


def router(web: Web) -> APIRouter:
    r = APIRouter(prefix="/ops")
    store, settings, bots = web.store, web.settings, web.bots

    async def operator(request: Request) -> None:  # async: sync dependencies run in a thread
        if store.session(request.cookies.get(OPS_COOKIE, "")) != (OPS, OPERATOR):
            raise OpsLoginRequired

    Operator = Depends(operator)

    def page(request: Request, name: str, **ctx):
        return web.templates.TemplateResponse(request, f"ops/{name}", {"msg": request.query_params.get("msg"), **ctx})

    def status(cafe: Cafe) -> tuple[str, str]:
        """(ok | warn | bad, what the operator should know first)."""
        state = bots.state(cafe.id)
        if state != "running":
            return ("bad" if state == "rejected" else "warn"), BOT_STATES[state]
        sub, _ = cafe.subscription()
        if sub == "paused":
            return "warn", "Приостановлено вами — заказы не принимаются"
        if sub == "off":
            return "bad", f"Не оплачено с {cafe.paid_until:%d.%m} — заказы не принимаются"
        if sub == "grace":
            return "bad", f"Не оплачено — отключится после {last_order_day(cafe.paid_until):%d.%m}"
        if not store.admins(cafe.id):
            return "warn", "Владелец ещё не подключился — отправьте ему ссылку-приглашение"
        if sub == "soon":
            return "warn", f"Работает · оплачено до {cafe.paid_until:%d.%m}, скоро платить"
        return "ok", "Работает"

    def subscription(cafe: Cafe) -> str:
        """The subscription in one line for the cafe page."""
        sub, _ = cafe.subscription()
        trial = not store.payments(cafe.id)
        if sub == "unlimited":
            return "Без срока — демо или своё заведение, оплата не нужна."
        if sub == "paused":
            return "Приостановлено вами: бот не принимает заказы, пока вы не включите его снова."
        until = f"{cafe.paid_until:%d.%m.%Y}"
        what = "Пробный период" if trial else "Оплачено"
        if sub in ("ok", "soon"):
            return f"{what} до {until} включительно."
        if sub == "grace":
            return (
                f"Не оплачено: срок кончился {until}. Отсрочка {GRACE_DAYS} дня — заказы принимаются до "
                f"{last_order_day(cafe.paid_until):%d.%m}, владельцу уже напомнили."
            )
        return f"Не оплачено с {until}: бот не принимает заказы. Запишите оплату — он включится сразу."

    def cookie(request: Request, resp: RedirectResponse, name: str, value: str) -> RedirectResponse:
        resp.set_cookie(
            name,
            value,
            max_age=SESSION_TTL,
            httponly=True,
            samesite="lax",  # also the CSRF protection: cross-site form posts arrive without the cookie
            secure=secure_cookie(request, settings),
        )
        return resp

    # --- login ---
    @r.get("/login/{token}")
    async def login_page(request: Request, token: str):
        if store.peek_login(token) != (OPS, OPERATOR):  # see /login: previews must not use the link up
            raise OpsLoginRequired
        return web.templates.TemplateResponse(request, "enter.html", {"title": "Панель оператора"})

    @r.post("/login/{token}")
    async def login(request: Request, token: str):
        if store.consume_login(token) != (OPS, OPERATOR):
            raise OpsLoginRequired
        return cookie(
            request, RedirectResponse("/ops", status_code=303), OPS_COOKIE, store.create_session(OPS, OPERATOR)
        )

    @r.post("/logout")
    async def logout(request: Request):
        store.drop_session(request.cookies.get(OPS_COOKIE, ""))
        resp = RedirectResponse("/ops", status_code=303)
        resp.delete_cookie(OPS_COOKIE)
        return resp

    # --- cafes ---
    @r.get("", dependencies=[Operator])
    async def index(request: Request):
        cafes = [(c, status(c), store.stats(c.id, c.today())) for c in store.cafes()]
        month_start = datetime.now(notify.TZ).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        paying = [c for c in store.cafes() if c.subscription()[0] in ("ok", "soon") and store.payments(c.id)]
        return page(
            request,
            "index.html",
            cafes=cafes,
            public_url=settings.public_url,
            income=store.income(month_start),
            paying=len(paying),
            monthly=sum(p.amount / p.months for c in paying for p in store.payments(c.id)[:1]),
            service_bot=bool(store.operators()),
        )

    @r.get("/new", dependencies=[Operator])
    async def new(request: Request):
        return page(request, "new.html", templates=available_templates(CLIENTS), token="", chosen="")

    @r.post("/new", dependencies=[Operator])
    async def create(
        request: Request,
        token: Annotated[str, Form()],
        template: Annotated[str, Form()] = "",
        pack: Annotated[UploadFile | None, File()] = None,
        plan: Annotated[str, Form()] = "trial",
    ):
        token = token.strip()
        templates = available_templates(CLIENTS)

        def again(error: str):  # the form back with what was typed, so nothing is pasted twice
            return page(request, "new.html", templates=templates, token=token, chosen=template, plan=plan, error=error)

        if taken := store.cafe_by_token(token):
            return again(f"Этот бот уже подключён — «{taken.name}». Для другого заведения нужен другой бот.")
        if token == store.meta("ops_bot_token"):
            return again("Это ваш служебный бот — он для уведомлений вам. Для заведения нужен отдельный бот.")
        try:
            username, name = await bots.check(token)
        except TokenError as e:
            return again(str(e))
        data = await pack.read() if pack else b""
        with tempfile.TemporaryDirectory() as tmp:  # an uploaded pack is needed only while it is imported
            try:
                if data:
                    chosen = load_template(unpack_pack(data, Path(tmp)))
                elif template:
                    if template not in dict(templates):
                        return again("Такого шаблона нет — выберите из списка")
                    chosen = load_template(CLIENTS / template)
                else:
                    chosen = None
            except ValueError as e:  # also pydantic's ValidationError: the JSON is not a menu
                return again(f"Шаблон не подошёл: {e}")
            trial = None if plan == "demo" else date.today() + timedelta(days=TRIAL_DAYS)
            try:
                cafe_id = connect_cafe(store, settings.media_dir, token, username, name, chosen, trial)
            except BadImage as e:  # nothing was created: the operator fixes the picture and tries again
                return again(f"Картинка в архиве не подошла — {e}")
        await bots.sync()
        return back(f"/ops/cafes/{cafe_id}", "Заведение подключено. Отправьте владельцу ссылку-приглашение ниже.")

    @r.get("/cafes/{cafe_id}", dependencies=[Operator])
    async def cafe_page(request: Request, cafe_id: int):
        cafe = store.cafe(cafe_id)
        if not cafe:
            return back("/ops")
        return page(
            request,
            "cafe.html",
            cafe=cafe,
            status=status(cafe),
            admins=store.admins(cafe.id),
            today=store.stats(cafe.id, cafe.today()),
            public_url=settings.public_url,
            subscription=subscription(cafe),
            sub_state=SUBSCRIPTION[cafe.subscription()[0]],
            payments=store.payments(cafe.id),
            price=notify.price(store),
        )

    @r.post("/cafes/{cafe_id}/invite", dependencies=[Operator])
    async def invite(cafe_id: int):
        store.new_invite(cafe_id)
        return back(f"/ops/cafes/{cafe_id}#owner")

    @r.post("/cafes/{cafe_id}/paid", dependencies=[Operator])
    async def paid(cafe_id: int, until: Annotated[str, Form()] = ""):
        try:
            store.set_paid_until(cafe_id, date.fromisoformat(until) if until else None)
        except ValueError:
            return back(f"/ops/cafes/{cafe_id}", "Не понял дату")
        return back(f"/ops/cafes/{cafe_id}", "Срок подписки сохранён")

    @r.post("/cafes/{cafe_id}/payment", dependencies=[Operator])
    async def payment(
        cafe_id: int,
        amount: Annotated[int, Form(ge=0, le=MAX_MONEY)],
        months: Annotated[int, Form(ge=1, le=24)] = 1,
        note: Annotated[str, Form()] = "",
    ):
        cafe = store.cafe(cafe_id)
        if not cafe:
            return back("/ops")
        paid = store.add_payment(cafe_id, amount, months, cafe.now().date(), note.strip())
        if not cafe.enabled:  # paused for not paying: paying switches it back on
            store.set_enabled(cafe_id, True)
        await notify.payment_received(store, bots.bot(cafe_id), cafe, paid.until)
        return back(f"/ops/cafes/{cafe_id}#subscription", f"Оплата записана — работает до {paid.until:%d.%m.%Y}")

    @r.post("/cafes/{cafe_id}/payments/{payment_id}/cancel", dependencies=[Operator])
    async def cancel_payment(cafe_id: int, payment_id: int):
        done = store.cancel_payment(cafe_id, payment_id)
        return back(f"/ops/cafes/{cafe_id}#subscription", "Оплата отменена, срок вернулся" if done else None)

    @r.post("/cafes/{cafe_id}/enabled", dependencies=[Operator])
    async def enabled(cafe_id: int, on: Annotated[bool, Form()] = False):
        store.set_enabled(cafe_id, on)
        return back(f"/ops/cafes/{cafe_id}", "Заказы снова принимаются" if on else "Приём заказов остановлен")

    @r.post("/cafes/{cafe_id}/token", dependencies=[Operator])
    async def new_token(cafe_id: int, token: Annotated[str, Form()]):
        token = token.strip()
        taken = store.cafe_by_token(token)
        if taken and taken.id != cafe_id:
            return back(f"/ops/cafes/{cafe_id}", f"Этот бот уже подключён к «{taken.name}»")
        if token == store.meta("ops_bot_token"):
            return back(f"/ops/cafes/{cafe_id}", "Это ваш служебный бот — для заведения нужен отдельный бот")
        try:
            username, _ = await bots.check(token)
        except TokenError as e:
            return back(f"/ops/cafes/{cafe_id}", str(e))
        store.set_token(cafe_id, token)
        store.set_bot_username(cafe_id, username)
        await bots.sync()
        return back(f"/ops/cafes/{cafe_id}", f"Токен сменён — теперь это @{username}")

    @r.post("/cafes/{cafe_id}/delete", dependencies=[Operator])
    async def delete(cafe_id: int, confirm: Annotated[str, Form()] = ""):
        cafe = store.cafe(cafe_id)
        if not cafe:
            return back("/ops")
        if confirm.strip() != cafe.slug:
            return back(f"/ops/cafes/{cafe_id}#delete", f"Чтобы удалить, впишите «{cafe.slug}»")
        for name in store.delete_cafe(cafe_id):
            drop_photo(settings.media_dir, name)
        await bots.sync()
        return back("/ops", f"«{cafe.name}» удалено. Бот @{cafe.bot_username} свободен — его можно подключить заново.")

    # --- the operator's own settings: payment contact, price, the service bot ---
    @r.get("/settings", dependencies=[Operator])
    async def ops_settings(request: Request):
        username = store.meta("ops_bot_username")
        invite = None
        if store.meta("ops_bot_token") and username:
            code = store.meta("ops_invite")
            if not code:
                code = secrets.token_urlsafe(9)
                store.set_meta("ops_invite", code)
            invite = f"https://t.me/{username}?start=op-{code}"
        return page(
            request,
            "settings.html",
            support=notify.contact(store, settings),
            price=notify.price(store),
            bot_username=username if store.meta("ops_bot_token") else None,
            bot_state=BOT_STATES[bots.state(OPS)] if store.meta("ops_bot_token") else None,
            invite=invite,
            operators=store.operators(),
        )

    @r.post("/settings", dependencies=[Operator])
    async def save_settings(support: Annotated[str, Form()] = "", price: Annotated[int, Form(ge=0, le=MAX_MONEY)] = 0):
        store.set_meta("support", support.strip() or None)
        store.set_meta("price", str(price) if price else None)
        return back("/ops/settings", "Сохранено")

    @r.post("/settings/bot", dependencies=[Operator])
    async def service_bot(token: Annotated[str, Form()] = ""):
        token = token.strip()
        if not token:
            store.set_meta("ops_bot_token", None)
            store.set_meta("ops_bot_username", None)
            await bots.sync()
            return back("/ops/settings", "Служебный бот отключён")
        if store.cafe_by_token(token):
            return back("/ops/settings#bot", "Этот бот уже работает у заведения — для уведомлений нужен отдельный бот")
        try:
            username, _ = await bots.check(token)
        except TokenError as e:
            return back("/ops/settings#bot", str(e))
        store.set_meta("ops_bot_token", token)
        store.set_meta("ops_bot_username", username)
        store.set_meta("ops_invite", None)  # a new bot: a new link
        await bots.sync()
        return back("/ops/settings#bot", f"@{username} подключён. Теперь откройте ссылку ниже в Telegram.")

    @r.post("/settings/operators/{chat_id}/remove", dependencies=[Operator])
    async def remove_operator(chat_id: int):
        store.remove_operator(chat_id)
        return back("/ops/settings#bot")

    @r.post("/cafes/{cafe_id}/admin", dependencies=[Operator])
    async def open_admin(request: Request, cafe_id: int):
        """The operator fills in the menu and settings for the owner, in the owner's admin."""
        if not store.cafe(cafe_id):
            return back("/ops")
        return cookie(
            request, RedirectResponse("/orders", status_code=303), COOKIE, store.create_session(cafe_id, OPERATOR)
        )

    return r
