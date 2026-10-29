"""Admin: the order list with status buttons; every change also reaches the Telegram cards and the customer."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request

from ..bot import change_status, mark_paid
from ..models import STATUSES, Cafe
from .common import Web, back


def router(web: Web) -> APIRouter:
    r = APIRouter()
    store = web.store
    Admin = Annotated[tuple[Cafe, int], Depends(web.admin)]

    @r.get("/orders")
    async def orders(request: Request, admin: Admin, all: bool = False):
        cafe, _ = admin
        n, total = store.stats(cafe.id, cafe.today())
        return web.page(
            request,
            "orders.html",
            cafe,
            orders=store.orders(cafe.id, active_only=not all),
            show_all=all,
            today=(n, total),
        )

    @r.post("/orders/{order_id}/status")
    async def order_status(admin: Admin, order_id: int, status: Annotated[str, Form()], all: bool = False):
        cafe, _ = admin
        url = "/orders?all=1" if all else "/orders"
        o = store.order(cafe.id, order_id)
        bot = web.bot_for(cafe.id)
        if not o:
            return back(url)
        if not bot:
            return back(url, "Бот кафе сейчас не запущен — статус не изменён")
        if not await change_status(bot, store, cafe, o, status):
            return back(url, f"Заказ №{o.number} уже в статусе «{STATUSES[o.status]}»")
        return back(url)

    @r.post("/orders/{order_id}/paid")
    async def order_paid(admin: Admin, order_id: int, all: bool = False):
        cafe, _ = admin
        url = "/orders?all=1" if all else "/orders"
        bot = web.bot_for(cafe.id)
        if not bot:
            return back(url, "Бот кафе сейчас не запущен — оплата не отмечена")
        if not await mark_paid(bot, store, cafe, order_id):
            return back(url, "Оплата уже отмечена")
        return back(url)

    return r
