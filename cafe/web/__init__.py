"""Web admin for cafe owners: orders, menu with photos and modifiers, settings.
There are no passwords: /admin in the bot sends a one-time link that opens a session for that cafe."""

import pathlib
import secrets
from collections.abc import Callable
from datetime import time
from io import BytesIO
from typing import Annotated
from urllib.parse import quote

from aiogram import Bot
from fastapi import Depends, FastAPI, File, Form, Path, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps, UnidentifiedImageError

from ..bot import BUTTON, change_status
from ..config import Settings
from ..models import NEXT, PAYMENT_METHODS, PAYMENT_STATUSES, STATUSES, Cafe
from ..store import Store

HERE = pathlib.Path(__file__).parent
COOKIE = "sid"
PHOTO_SIZE = 1280


class LoginRequired(Exception):
    pass


def save_photo(data: bytes, media_dir: pathlib.Path) -> str:
    """Phone photos are big and rotated via EXIF: normalise to a ≤1280px JPEG, which Telegram accepts as is."""
    img = ImageOps.exif_transpose(Image.open(BytesIO(data))).convert("RGB")
    img.thumbnail((PHOTO_SIZE, PHOTO_SIZE))
    name = f"{secrets.token_hex(8)}.jpg"
    img.save(media_dir / name, "JPEG", quality=85)
    return name


def create_app(store: Store, settings: Settings, bot_for: Callable[[int], Bot | None]) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    media = settings.media_dir
    media.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.mount("/media", StaticFiles(directory=media), name="media")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        ACTIONS=BUTTON, STATUSES=STATUSES, PAYMENT_METHODS=PAYMENT_METHODS, PAYMENT_STATUSES=PAYMENT_STATUSES
    )

    async def current(request: Request) -> tuple[Cafe, int]:  # async: sync dependencies run in a thread
        s = store.session(request.cookies.get(COOKIE, ""))
        if not s or not store.is_admin(*s):
            raise LoginRequired
        return store.cafe(s[0]), s[1]

    Admin = Annotated[tuple[Cafe, int], Depends(current)]

    def page(request: Request, name: str, cafe: Cafe, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"cafe": cafe, "msg": request.query_params.get("msg"), **ctx})

    def back(url: str, msg: str | None = None) -> RedirectResponse:
        sep = "&" if "?" in url else "?"
        return RedirectResponse(url + (f"{sep}msg={quote(msg)}" if msg else ""), status_code=303)

    def drop_photo(name: str | None) -> None:
        if name:
            (media / name).unlink(missing_ok=True)

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, _: LoginRequired):
        return templates.TemplateResponse(request, "login.html", {"cafe": None}, status_code=401)

    # --- login ---
    @app.get("/login/{token}")
    async def login(request: Request, token: str):
        session = store.consume_login(token)
        if not session:
            raise LoginRequired
        resp = RedirectResponse("/orders", status_code=303)
        resp.set_cookie(
            COOKIE,
            session,
            max_age=30 * 24 * 3600,
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

    # --- orders ---
    @app.get("/orders")
    async def orders(request: Request, admin: Admin, all: bool = False):
        cafe, _ = admin
        n, total = store.stats(cafe.id, cafe.today())
        return page(
            request,
            "orders.html",
            cafe,
            orders=store.orders(cafe.id, active_only=not all),
            show_all=all,
            next_statuses=NEXT,
            today=(n, total),
        )

    @app.post("/orders/{order_id}/status")
    async def order_status(admin: Admin, order_id: int, status: Annotated[str, Form()], all: bool = False):
        cafe, _ = admin
        url = "/orders?all=1" if all else "/orders"
        o = store.order(cafe.id, order_id)
        bot = bot_for(cafe.id)
        if not o:
            return back(url)
        if not bot:
            return back(url, "Бот кафе сейчас не запущен — статус не изменён")
        if not await change_status(bot, store, cafe, o, status):
            return back(url, f"Заказ №{o.number} уже в статусе «{STATUSES[o.status]}»")
        return back(url)

    # --- menu ---
    @app.get("/menu")
    async def menu(request: Request, admin: Admin):
        cafe, _ = admin
        return page(request, "menu.html", cafe, categories=store.menu(cafe.id, only_available=False))

    @app.post("/categories")
    async def add_category(admin: Admin, name: Annotated[str, Form()]):
        store.add_category(admin[0].id, name.strip())
        return back("/menu")

    @app.post("/categories/{category_id}")
    async def rename_category(admin: Admin, category_id: int, name: Annotated[str, Form()]):
        store.rename_category(admin[0].id, category_id, name.strip())
        return back("/menu")

    @app.post("/categories/{category_id}/move")
    async def move_category(admin: Admin, category_id: int, step: Annotated[int, Form()]):
        store.move_category(admin[0].id, category_id, step)
        return back("/menu")

    @app.post("/categories/{category_id}/delete")
    async def delete_category(admin: Admin, category_id: int):
        if not store.delete_category(admin[0].id, category_id):
            return back("/menu", "В разделе есть позиции: сначала удалите или перенесите их")
        return back("/menu")

    @app.get("/items/new")
    async def new_item(request: Request, admin: Admin, category_id: int):
        cafe, _ = admin
        return page(
            request,
            "item.html",
            cafe,
            item=None,
            category_id=category_id,
            categories=store.menu(cafe.id, only_available=False),
            groups=store.groups(cafe.id),
        )

    @app.get("/items/{item_id}")
    async def edit_item(request: Request, admin: Admin, item_id: int):
        cafe, _ = admin
        it = store.item(cafe.id, item_id, only_available=False)
        if not it:
            return back("/menu")
        return page(
            request,
            "item.html",
            cafe,
            item=it,
            category_id=it.category_id,
            categories=store.menu(cafe.id, only_available=False),
            groups=store.groups(cafe.id),
        )

    @app.post("/items/{item_id}")
    async def save_item(
        admin: Admin,
        item_id: Annotated[str, Path(pattern=r"^(new|\d+)$")],
        name: Annotated[str, Form()],
        price: Annotated[int, Form(ge=0)],
        category_id: Annotated[int, Form()],
        description: Annotated[str, Form(max_length=500)] = "",
        groups: Annotated[list[int] | None, Form()] = None,
        photo: Annotated[UploadFile | None, File()] = None,
        remove_photo: Annotated[bool, Form()] = False,
    ):
        cafe, _ = admin
        fields = {"name": name.strip(), "description": description.strip(), "price": price}
        groups = groups or []
        if item_id == "new":
            saved = store.add_item(cafe.id, category_id, group_ids=groups, **fields)
        else:
            ok = store.update_item(cafe.id, int(item_id), category_id=category_id, group_ids=groups, **fields)
            saved = int(item_id) if ok else None
        if saved is None:
            return back("/menu")
        data = await photo.read() if photo else b""
        if data:
            try:
                filename = save_photo(data, media)
            except (UnidentifiedImageError, Image.DecompressionBombError):
                return back(f"/items/{saved}", "Не получилось прочитать фото — загрузите JPG или PNG")
            drop_photo(store.set_photo(cafe.id, saved, filename))
        elif remove_photo:
            drop_photo(store.set_photo(cafe.id, saved, None))
        return back("/menu", f"«{fields['name']}» сохранено")

    @app.post("/items/{item_id}/available")
    async def toggle_item(admin: Admin, item_id: int, available: Annotated[bool, Form()] = False):
        store.set_available(admin[0].id, item_id, available)
        return back(f"/menu#item-{item_id}")

    @app.post("/items/{item_id}/move")
    async def move_item(admin: Admin, item_id: int, step: Annotated[int, Form()]):
        store.move_item(admin[0].id, item_id, step)
        return back(f"/menu#item-{item_id}")

    @app.post("/items/{item_id}/delete")
    async def delete_item(admin: Admin, item_id: int):
        drop_photo(store.delete_item(admin[0].id, item_id))
        return back("/menu")

    # --- modifiers ---
    @app.get("/modifiers")
    async def modifiers(request: Request, admin: Admin):
        cafe, _ = admin
        return page(request, "modifiers.html", cafe, groups=store.groups(cafe.id))

    @app.post("/modifiers")
    async def add_group(admin: Admin, name: Annotated[str, Form()], multiple: Annotated[bool, Form()] = False):
        group_id = store.add_group(admin[0].id, name.strip(), multiple)
        return back(f"/modifiers#group-{group_id}")

    @app.post("/modifiers/{group_id}")
    async def update_group(
        admin: Admin,
        group_id: int,
        name: Annotated[str, Form()],
        multiple: Annotated[bool, Form()] = False,
    ):
        store.update_group(admin[0].id, group_id, name.strip(), multiple)
        return back(f"/modifiers#group-{group_id}")

    @app.post("/modifiers/{group_id}/delete")
    async def delete_group(admin: Admin, group_id: int):
        store.delete_group(admin[0].id, group_id)
        return back("/modifiers")

    @app.post("/modifiers/{group_id}/options")
    async def add_option(
        admin: Admin,
        group_id: int,
        name: Annotated[str, Form()],
        price: Annotated[int, Form(ge=0)] = 0,
    ):
        store.add_option(admin[0].id, group_id, name.strip(), price)
        return back(f"/modifiers#group-{group_id}")

    @app.post("/options/{option_id}")
    async def update_option(
        admin: Admin,
        option_id: int,
        name: Annotated[str, Form()],
        price: Annotated[int, Form(ge=0)] = 0,
    ):
        store.update_option(admin[0].id, option_id, name.strip(), price)
        return back("/modifiers")

    @app.post("/options/{option_id}/delete")
    async def delete_option(admin: Admin, option_id: int):
        store.delete_option(admin[0].id, option_id)
        return back("/modifiers")

    # --- settings ---
    @app.get("/settings")
    async def settings_page(request: Request, admin: Admin):
        cafe, user_id = admin
        return page(request, "settings.html", cafe, admins=store.admins(cafe.id), me=user_id)

    @app.post("/settings")
    async def save_settings(
        admin: Admin,
        name: Annotated[str, Form()],
        greeting: Annotated[str, Form()],
        pickup_address: Annotated[str, Form()] = "",
        payment_note: Annotated[str, Form()] = "",
        always_open: Annotated[bool, Form()] = False,
        open_time: Annotated[str, Form()] = "",
        close_time: Annotated[str, Form()] = "",
        delivery_enabled: Annotated[bool, Form()] = False,
        delivery_min: Annotated[int, Form(ge=0)] = 0,
        delivery_fee: Annotated[int, Form(ge=0)] = 0,
    ):
        cafe, _ = admin
        try:
            hours = (None, None) if always_open else (time.fromisoformat(open_time), time.fromisoformat(close_time))
        except ValueError:
            return back("/settings", "Укажите часы работы или отметьте «круглосуточно»")
        store.update_cafe(
            cafe.id,
            name=name.strip(),
            greeting=greeting.strip(),
            pickup_address=pickup_address.strip(),
            payment_note=payment_note.strip(),
            open_time=hours[0],
            close_time=hours[1],
            delivery_enabled=delivery_enabled,
            delivery_min=delivery_min,
            delivery_fee=delivery_fee,
        )
        return back("/settings", "Настройки сохранены")

    @app.post("/settings/invite")
    async def invite(admin: Admin):
        store.new_invite(admin[0].id)
        return back("/settings#admins")

    @app.post("/admins/{user_id}/delete")
    async def remove_admin(admin: Admin, user_id: int):
        cafe, me = admin
        if user_id != me:  # nobody locks themselves out
            store.remove_admin(cafe.id, user_id)
        return back("/settings#admins")

    return app
