"""Admin: a printable poster in the cafe's colours with a QR code to the bot — for the counter, tables and the door.
A bot nobody knows about gets no orders, so this ships with every cafe."""

from typing import Annotated

import segno
from fastapi import APIRouter, Depends, Request

from ..models import Cafe
from .common import Web


def router(web: Web) -> APIRouter:
    r = APIRouter()
    Admin = Annotated[tuple[Cafe, int], Depends(web.admin)]

    @r.get("/promo")
    async def promo(request: Request, admin: Admin):
        cafe, _ = admin
        link = f"https://t.me/{cafe.bot_username}?start=qr" if cafe.bot_username else None
        qr = segno.make(link, error="m").svg_inline(border=2, dark="#111", omitsize=True) if link else None
        return web.page(request, "promo.html", cafe, link=link, qr=qr)

    return r
