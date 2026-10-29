"""Admin: cafe settings — hours, delivery, payment by card transfer, branding for the Mini App — and administrators."""

import re
from datetime import time
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from PIL import Image, UnidentifiedImageError

from ..media import drop_photo, save_photo
from ..models import Cafe
from .common import Web, back


def card_digits(raw: str) -> str | None:
    """Uzcard/Humo/Visa numbers are 16 digits; stored as "8600 1234 5678 9012". Empty switches transfers off."""
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if len(digits) != 16:
        return None
    return " ".join(digits[i : i + 4] for i in range(0, 16, 4))


def router(web: Web) -> APIRouter:
    r = APIRouter()
    store, media = web.store, web.settings.media_dir
    Admin = Annotated[tuple[Cafe, int], Depends(web.admin)]

    @r.get("/settings")
    async def settings_page(request: Request, admin: Admin):
        cafe, user_id = admin
        return web.page(request, "settings.html", cafe, admins=store.admins(cafe.id), me=user_id)

    @r.post("/settings")
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
        card_number: Annotated[str, Form()] = "",
        card_holder: Annotated[str, Form()] = "",
    ):
        cafe, _ = admin
        try:
            hours = (None, None) if always_open else (time.fromisoformat(open_time), time.fromisoformat(close_time))
        except ValueError:
            return back("/settings", "Укажите часы работы или отметьте «круглосуточно»")
        card = card_digits(card_number)
        if card is None:
            return back("/settings#payment", "Номер карты — 16 цифр")
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
            card_number=card,
            card_holder=card_holder.strip(),
        )
        return back("/settings", "Настройки сохранены")

    @r.post("/settings/brand")
    async def save_brand(
        admin: Admin,
        brand_color: Annotated[str, Form(pattern=r"^#[0-9a-fA-F]{6}$")],
        logo: Annotated[UploadFile | None, File()] = None,
        cover: Annotated[UploadFile | None, File()] = None,
        remove_logo: Annotated[bool, Form()] = False,
        remove_cover: Annotated[bool, Form()] = False,
    ):
        cafe, _ = admin
        store.update_cafe(cafe.id, brand_color=brand_color.lower())
        for kind, upload, remove in (("logo", logo, remove_logo), ("cover", cover, remove_cover)):
            data = await upload.read() if upload else b""
            if data:
                try:
                    name = save_photo(data, media)
                except (UnidentifiedImageError, Image.DecompressionBombError):
                    return back("/settings#brand", "Не получилось прочитать картинку — загрузите JPG или PNG")
                drop_photo(media, store.set_cafe_image(cafe.id, kind, name))
            elif remove:
                drop_photo(media, store.set_cafe_image(cafe.id, kind, None))
        return back("/settings#brand", "Оформление сохранено")

    @r.post("/settings/invite")
    async def invite(admin: Admin):
        store.new_invite(admin[0].id)
        return back("/settings#admins")

    @r.post("/admins/{user_id}/delete")
    async def remove_admin(admin: Admin, user_id: int):
        cafe, me = admin
        if user_id != me:  # nobody locks themselves out
            store.remove_admin(cafe.id, user_id)
        return back("/settings#admins")

    return r
