"""The customers' Mini App: menu with photos, item options, cart and checkout, in the cafe's colours.
The page and the menu are public. Everything about a customer is signed by Telegram (initData in the
X-Telegram-Init-Data header) and checked with the cafe's bot token. The cart lives on the phone, so every order
is re-checked against the current menu here."""

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from aiogram.utils.web_app import WebAppUser, safe_parse_webapp_init_data
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import delivery, geocode
from ..bot import send_to_customer, send_to_staff
from ..i18n import APP_TEXTS, lang_of
from ..media import thumb
from ..models import Cafe
from ..store import MAX_QTY
from .common import Web

log = logging.getLogger(__name__)
INIT_DATA_TTL = 24 * 3600  # Telegram re-signs it every time the Mini App is opened
MAX_WAITING = 3  # a guest's orders the kitchen has not accepted yet
MAX_PER_HOUR = 10  # a guest's orders in the last hour


class LineIn(BaseModel):
    item_id: int
    option_ids: list[int] = Field(default_factory=list, max_length=30)
    qty: int = Field(ge=1, le=MAX_QTY)


class OrderIn(BaseModel):
    lines: list[LineIn] = Field(min_length=1, max_length=50)
    method: Literal["pickup", "delivery"]
    phone: str = Field(max_length=30)
    address: str = Field("", max_length=300)
    comment: str = Field("", max_length=300)
    payment: Literal["cash", "transfer"]
    lang: Literal["ru", "uz"]
    lat: float | None = Field(None, ge=-90, le=90)  # the delivery pin
    lon: float | None = Field(None, ge=-180, le=180)


class LangIn(BaseModel):
    lang: Literal["ru", "uz"]


def router(web: Web) -> APIRouter:
    r = APIRouter(prefix="/app")
    store = web.store

    def cafe_or_404(slug: str) -> Cafe:
        cafe = store.cafe_by_slug(slug)
        if not cafe:
            raise HTTPException(404)
        return cafe

    async def customer(
        slug: str, x_telegram_init_data: Annotated[str, Header()] = ""
    ) -> tuple[Cafe, WebAppUser]:  # async: sync dependencies run in a thread
        cafe = cafe_or_404(slug)
        try:
            data = safe_parse_webapp_init_data(cafe.bot_token, x_telegram_init_data)
        except ValueError:
            raise HTTPException(401)
        if not data.user or time.time() - data.auth_date.timestamp() > INIT_DATA_TTL:
            raise HTTPException(401)
        return cafe, data.user

    Customer = Annotated[tuple[Cafe, WebAppUser], Depends(customer)]

    def menu_data(cafe: Cafe) -> list[dict]:
        return [
            {
                "id": c.id,
                "name": c.name,
                "items": [
                    {
                        "id": it.id,
                        "name": it.name,
                        "description": it.description,
                        "price": it.price,
                        "thumb": f"/media/{thumb(it.photo)}" if it.photo else None,
                        "photo": f"/media/{it.photo}" if it.photo else None,
                        "groups": [
                            {
                                "name": g.name,
                                "multiple": g.multiple,
                                "options": [{"id": o.id, "name": o.name, "price": o.price} for o in g.options],
                            }
                            for g in it.groups
                        ],
                    }
                    for it in c.items
                ],
            }
            for c in store.menu(cafe.id)
        ]

    @r.get("/{slug}")
    async def page(request: Request, slug: str):
        cafe = cafe_or_404(slug)
        data = {
            "slug": cafe.slug,
            "name": cafe.name,
            "currency": cafe.currency,
            "open": cafe.is_open(),
            "hours": cafe.hours(),
            "active": cafe.active(),
            "pickup_address": cafe.pickup_address,
            "delivery": {
                "enabled": cafe.delivery_enabled,
                "min": cafe.delivery_min,
                "fee": cafe.delivery_fee,
                "zones": cafe.delivery_zones if delivery.by_zones(cafe) else [],
                "point": cafe.point,
                "center": cafe.point or delivery.TASHKENT,
            },
            "payments": cafe.payment_methods(),
            "payment_note": cafe.payment_note,
            "menu": menu_data(cafe),
            "texts": APP_TEXTS,
        }
        return web.templates.TemplateResponse(request, "app/index.html", {"cafe": cafe, "data": data})

    @r.get("/{slug}/me")
    async def me(who: Customer, repeat: int | None = None):
        """Saved phone and address, the chosen language and, for "repeat", a past order as cart lines."""
        cafe, user = who
        c = store.customer(cafe.id, user.id)
        result = {
            "phone": c.phone if c else None,
            "address": c.address if c else None,
            "point": [c.lat, c.lon] if c and c.lat is not None else None,
            "lang": c.lang if c else lang_of(user.language_code),
        }
        if repeat and (found := store.repeat(cafe.id, user.id, repeat)):
            lines, missing = found
            result["repeat"] = {
                "lines": [
                    {"item_id": ln.item.id, "option_ids": [o.id for o in ln.options], "qty": ln.qty} for ln in lines
                ],
                "missing": missing,
            }
        return result

    @r.get("/{slug}/where")
    async def where(
        who: Customer,
        lat: Annotated[float, Query(ge=-90, le=90)],
        lon: Annotated[float, Query(ge=-180, le=180)],
        lang: Literal["ru", "uz"] = "ru",
    ):
        """A suggestion for the address line under the pin (geocode.py); null if none — the guest types it."""
        return {"address": await geocode.address(lat, lon, lang)}

    @r.post("/{slug}/lang")
    async def set_lang(who: Customer, body: LangIn):
        cafe, user = who
        store.set_lang(cafe.id, user.id, body.lang)
        return {"ok": True}

    @r.post("/{slug}/orders")
    async def place(who: Customer, body: OrderIn):
        cafe, user = who

        def refuse(code: str) -> JSONResponse:  # the Mini App shows APP_TEXTS[lang]["error_" + code]
            return JSONResponse({"error": code}, status_code=409)

        bot = web.bots.bot(cafe.id)
        waiting, last_hour = store.open_orders(cafe.id, user.id, datetime.now(UTC) - timedelta(hours=1))
        if waiting >= MAX_WAITING or last_hour >= MAX_PER_HOUR:  # a stuck button, a prank or a bot
            return refuse("too_many")
        if not cafe.active():
            return refuse("inactive")
        if not cafe.is_open():
            return refuse("closed")
        if not bot:
            return refuse("unavailable")
        lines = store.lines(cafe.id, [(ln.item_id, ln.option_ids, ln.qty) for ln in body.lines])
        if any(ln is None for ln in lines):
            return refuse("menu_changed")
        address = body.address.strip()
        point = (body.lat, body.lon) if body.lat is not None and body.lon is not None else None
        if body.method == "delivery":
            if not cafe.delivery_enabled:
                return refuse("no_delivery")
            if sum(ln.sum for ln in lines) < cafe.delivery_min:
                return refuse("min_delivery")
            if delivery.fee(cafe, point) is None:
                return refuse("far" if point else "point")
            if not address:
                return refuse("address")
        phone = body.phone.strip()
        if sum(ch.isdigit() for ch in phone) < 7:
            return refuse("phone")
        if body.payment not in cafe.payment_methods():
            return refuse("payment")
        o = store.place(
            cafe,
            user.id,
            " ".join(filter(None, [user.first_name, user.last_name])),
            lines,
            phone=phone,
            method=body.method,
            address=address,
            comment=body.comment.strip() or None,
            payment_method=body.payment,
            lang=body.lang,
            point=point,
        )
        try:  # the order is saved: a Telegram hiccup now must not look like a failure (the guest would order twice)
            await send_to_staff(bot, store, cafe, o)
            await send_to_customer(bot, store, cafe, o)
        except Exception:
            log.exception("%s: order %s saved, but Telegram did not take the messages", cafe.slug, o.number)
        result = {"number": o.number, "total": o.total}
        if o.payment_method == "transfer":  # the card is shown only to someone who has to pay to it
            result["card"] = {"number": cafe.card_number, "holder": cafe.card_holder}
        return result

    return r
