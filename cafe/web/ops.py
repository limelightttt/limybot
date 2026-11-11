"""The operator panel /ops — for whoever runs this installation and sells it to cafes. Everything that used to need
the command line: connect a cafe (bot token + a menu template or a zip pack), see whether each bot works, send the
owner their invite link, subscription, pause, a new token, deleting, and opening any cafe's admin to fill it in.
The login link is printed by ./run.sh at start (or `python -m cafe ops`)."""

import tempfile
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse

from ..config import ROOT
from ..media import drop_photo
from ..models import BOT_STATES, Cafe
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
        if not cafe.enabled:
            return "warn", "Приостановлено — заказы не принимаются"
        if not cafe.active():
            return "bad", f"Подписка закончилась {cafe.paid_until:%d.%m.%Y} — заказы не принимаются"
        if not store.admins(cafe.id):
            return "warn", "Владелец ещё не подключился — отправьте ему ссылку-приглашение"
        return "ok", "Работает"

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
        return page(request, "index.html", cafes=cafes, public_url=settings.public_url)

    @r.get("/new", dependencies=[Operator])
    async def new(request: Request):
        return page(request, "new.html", templates=available_templates(CLIENTS), token="", chosen="")

    @r.post("/new", dependencies=[Operator])
    async def create(
        request: Request,
        token: Annotated[str, Form()],
        template: Annotated[str, Form()] = "",
        pack: Annotated[UploadFile | None, File()] = None,
    ):
        token = token.strip()
        templates = available_templates(CLIENTS)

        def again(error: str):  # the form back with what was typed, so nothing is pasted twice
            return page(request, "new.html", templates=templates, token=token, chosen=template, error=error)

        if taken := store.cafe_by_token(token):
            return again(f"Этот бот уже подключён — «{taken.name}». Для другого заведения нужен другой бот.")
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
            cafe_id = connect_cafe(store, settings.media_dir, token, username, name, chosen)
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

    @r.post("/cafes/{cafe_id}/admin", dependencies=[Operator])
    async def open_admin(request: Request, cafe_id: int):
        """The operator fills in the menu and settings for the owner, in the owner's admin."""
        if not store.cafe(cafe_id):
            return back("/ops")
        return cookie(
            request, RedirectResponse("/orders", status_code=303), COOKIE, store.create_session(cafe_id, OPERATOR)
        )

    return r
