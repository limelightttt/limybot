"""Admin, "Продвижение": news to the cafe's guests in their Telegram (news.py) and a printable poster with a QR code
to the bot — for the counter, tables and the door. A bot nobody knows about gets no orders, and guests who ordered
once come back when they hear about a new burger: both ship with every cafe."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

import segno
from fastapi import APIRouter, Depends, Form, Request

from .. import news
from ..models import Cafe
from .common import Web, back


def router(web: Web) -> APIRouter:
    r = APIRouter()
    Admin = Annotated[tuple[Cafe, int], Depends(web.admin)]

    @r.get("/promo")
    async def promo(request: Request, admin: Admin):
        cafe, _ = admin
        link = f"https://t.me/{cafe.bot_username}?start=qr" if cafe.bot_username else None
        qr = segno.make(link, error="m").svg_inline(border=2, dark="#111", omitsize=True) if link else None
        return web.page(
            request,
            "promo.html",
            cafe,
            link=link,
            qr=qr,
            audience=len(web.store.audience(cafe.id)),
            broadcasts=web.store.broadcasts(cafe.id),
            left=news.PER_DAY - web.store.broadcasts_since(cafe.id, datetime.now(UTC) - timedelta(days=1)),
            max_len=news.MAX_LEN,
        )

    @r.post("/promo/news")
    async def send_news(admin: Admin, text: Annotated[str, Form(max_length=news.MAX_LEN)]):
        cafe, _ = admin
        text = text.strip()
        bot = web.bots.bot(cafe.id)
        if not text:
            return back("/promo#news", "Напишите текст новости")
        if not bot:
            return back("/promo#news", "Бот заведения сейчас не запущен — попробуйте позже")
        if web.store.broadcasts_since(cafe.id, datetime.now(UTC) - timedelta(days=1)) >= news.PER_DAY:
            return back(
                "/promo#news", f"Не больше {news.PER_DAY} рассылок в сутки — иначе гости начнут блокировать бота"
            )
        if not web.store.audience(cafe.id):
            return back("/promo#news", "Пока некому отправлять: гости появятся, когда откроют бота")
        news.start(bot, web.store, cafe.id, text)
        return back("/promo#news", "Рассылка пошла — прогресс ниже, обновите страницу через минуту")

    return r
