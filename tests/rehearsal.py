"""A dress rehearsal of a working day, without Telegram: the real bot, Mini App and admin code, a fake Telegram that
records every message. Three staff groups (kitchen, couriers, finance), an owner and three guests go through the
usual and the awkward cases; the result is a readable log of what each chat saw, step by step.

    uv run python -m tests.rehearsal [out.html]

Useful before showing the bot to a cafe, and after changing anything in bot.py."""

import asyncio
import html
import sys
import tempfile
from pathlib import Path

import httpx
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendLocation, SendMessage
from aiogram.types import Chat

from cafe.bot import build
from cafe.config import Settings
from cafe.store import Store
from cafe.template import import_cafe, load_template
from cafe.web import create_app
from tests.conftest import DEMO, FakeBots, FakeSession, init_data, message, pick, tap

OWNER, AZIZ, MALIKA, TIMUR, STRANGER, COOK, COURIER, ACCOUNTANT = 7, 101, 102, 103, 999, 201, 202, 203
KITCHEN, COURIERS, FINANCE = -1001, -1002, -1003
NAMES = {
    OWNER: "Владелец (личка)",
    AZIZ: "Гость Азиз",
    MALIKA: "Гостья Малика",
    TIMUR: "Гость Тимур",
    STRANGER: "Посторонний",
    KITCHEN: "Группа «Кухня»",
    COURIERS: "Группа «Курьеры»",
    FINANCE: "Группа «Финансы»",
}
CAFE_POINT = (41.3111, 69.2797)
HOME = (41.3340, 69.2840)  # ~2.6 km away


class Log:
    def __init__(self, session: FakeSession):
        self.session = session
        self.seen = 0
        self.steps: list[tuple[str, list[str]]] = []

    def step(self, title: str) -> None:
        """Everything Telegram was asked to show since the previous step, under this title."""
        lines = []
        for m in self.session.sent[self.seen :]:
            if isinstance(m, SendMessage | EditMessageText):
                who = NAMES.get(m.chat_id, str(m.chat_id))
                kind = "✏️ сообщение изменилось" if isinstance(m, EditMessageText) else "💬 новое сообщение"
                buttons = [b for row in m.reply_markup.inline_keyboard for b in row] if m.reply_markup else []
                labels = " ".join(f"[{b.text}]" for b in buttons)
                lines.append(
                    f"<b>{who}</b> · {kind}<pre>{html.escape(m.text)}</pre>" + (f"<p>{labels}</p>" if labels else "")
                )
            elif isinstance(m, SendLocation):
                lines.append(f"<b>{NAMES.get(m.chat_id, m.chat_id)}</b> · 📍 точка на карте ответом на карточку")
            elif isinstance(m, AnswerCallbackQuery) and m.text:
                lines.append(f"<i>всплывающая подсказка: «{html.escape(m.text)}»</i>")
        self.seen = len(self.session.sent)
        self.steps.append((title, lines))

    def html(self) -> str:
        body = "".join(
            f"<section><h2>{n}. {html.escape(t)}</h2>{''.join(f'<div class=msg>{x}</div>' for x in lines) or '<p class=none>(ничего не отправлено)</p>'}</section>"
            for n, (t, lines) in enumerate(self.steps, 1)
        )
        return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>limybot — прогон сценариев</title>
<style>:root{{--bg:#f4f2ef;--card:#fff;--text:#1c1a18;--muted:#7b736c;--line:#e5e0da}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151515;--card:#222;--text:#eee;--muted:#999;--line:#333}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--text);font:15px/1.45 -apple-system,system-ui,sans-serif}}
h1{{font-size:22px}} h2{{font-size:16px;margin:24px 0 8px}} section{{max-width:720px;margin:auto}}
.msg{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin:8px 0}}
pre{{white-space:pre-wrap;font:inherit;margin:6px 0}} p{{margin:4px 0;color:var(--muted)}} .none{{color:var(--muted)}}</style>
</head><body><section><h1>limybot — прогон рабочего дня</h1><p>Настоящий код бота, витрины и групп; вместо Telegram —
запись того, что бот отправил. Три группы, владелец, три гостя.</p></section>{body}</body></html>"""


async def rehearse() -> Log:
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(db_path=tmp / "limybot.db", public_url="https://admin.limybot.uz")
    store = Store(settings.db_path)
    cafe_id = import_cafe(store, "demo", "1:demo", load_template(DEMO))
    store.update_cafe(
        cafe_id,
        open_time=None,
        close_time=None,
        card_number="8600 1234 5678 9012",
        card_holder="Алишер А.",
        lat=CAFE_POINT[0],
        lon=CAFE_POINT[1],
        delivery_zones=[(3, 10000), (7, 15000)],
    )
    session = FakeSession()
    bot = Bot("1:demo", session=session)
    dp = build(store, cafe_id, settings)
    app = create_app(store, settings, FakeBots({cafe_id: bot}))
    web = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=settings.public_url)
    log = Log(session)

    async def feed(update):
        await dp.feed_update(bot, update)

    def group(chat_id: int) -> Chat:
        return Chat(id=chat_id, type="supergroup", title=NAMES[chat_id])

    async def order(uid: int, **body) -> int:
        signed = {"X-Telegram-Init-Data": init_data("1:demo", uid)}
        r = await web.post(
            "/app/demo/orders", json={"phone": "+998 90 123 45 67", "lang": "ru", **body}, headers=signed
        )
        assert r.status_code == 200, r.text
        return store.orders(cafe_id, active_only=False)[0].id

    burgers = [pick(store, cafe_id, "Латте", ["M", "овсяное", "ваниль"], 2)]
    espresso = [pick(store, cafe_id, "Эспрессо", qty=3)]

    # --- setting up ---
    code = store.new_invite(cafe_id)
    await feed(message(OWNER, f"/start join-{code}"))
    log.step("Владелец открыл ссылку-приглашение")
    await feed(message(STRANGER, "/staff", chat=group(KITCHEN)))
    log.step("Посторонний пишет /staff в группе — ничего не меняется")
    for chat, cmd in ((KITCHEN, "/staff"), (COURIERS, "/couriers"), (FINANCE, "/finance")):
        await feed(message(OWNER, cmd, chat=group(chat)))
    log.step("Владелец пишет /staff, /couriers, /finance в трёх группах")

    # --- 1: delivery, card transfer, everything goes right ---
    o1 = await order(
        AZIZ,
        lines=burgers,
        method="delivery",
        payment="transfer",
        lat=HOME[0],
        lon=HOME[1],
        address="улица Шота Руставели, 12, подъезд 2, кв 18",
        comment="домофон 18",
    )
    log.step("Азиз заказал доставку с переводом на карту (2,6 км → 10 000)")
    await feed(tap(COURIER, f"st:{o1}:accepted", chat=group(COURIERS)))
    log.step("Курьер пытается нажать «Принять» за кухню (подделанная кнопка) — нельзя")
    await feed(tap(COOK, f"st:{o1}:accepted", chat=group(KITCHEN)))
    log.step("Кухня приняла — курьерам пришла доставка")
    await feed(tap(AZIZ, f"paid:{o1}"))
    log.step("Азиз нажал «Я перевёл» — пинг только финансам")
    await feed(tap(COOK, f"pay:{o1}", chat=group(KITCHEN)))
    log.step("Кухня пытается подтвердить деньги (подделанная кнопка) — нельзя, это финансы")
    await feed(tap(ACCOUNTANT, f"pay:{o1}", chat=group(FINANCE)))
    log.step("Финансы подтвердили перевод")
    await feed(tap(COOK, f"st:{o1}:cooking", chat=group(KITCHEN)))
    log.step("Кухня: «Готовится» — у Азиза пропадает «Отменить»")
    await feed(tap(AZIZ, f"oc:{o1}"))
    log.step("Азиз всё-таки жмёт «Отменить» со старого экрана — нельзя")
    await feed(tap(COURIER, f"st:{o1}:courier", chat=group(COURIERS)))
    await feed(tap(COURIER, f"st:{o1}:delivered", chat=group(COURIERS)))
    log.step("Курьер: «Передан курьеру», потом «Доставлен» — гостю вопрос про оценку")
    await feed(tap(AZIZ, f"rate:{o1}:2"))
    log.step("Азиз поставил 2⭐ — владельцу сразу сообщение, позвонить гостю")

    # --- 2: pickup, cash, the guest changes their mind after it was accepted ---
    o2 = await order(MALIKA, lines=espresso, method="pickup", payment="cash")
    log.step("Малика заказала самовывоз, оплата при получении (финансам и курьерам не приходит)")
    await feed(tap(COOK, f"st:{o2}:accepted", chat=group(KITCHEN)))
    log.step("Кухня приняла")
    await feed(tap(MALIKA, f"oc:{o2}"))
    log.step("Малика отменила (принят, но ещё не готовится — можно)")

    # --- 3: delivery, cash, the kitchen cancels while cooking ---
    o3 = await order(
        TIMUR, lines=burgers, method="delivery", payment="cash", lat=HOME[0], lon=HOME[1], address="Юнусабад 4, кв 7"
    )
    await feed(tap(COOK, f"st:{o3}:accepted", chat=group(KITCHEN)))
    await feed(tap(COOK, f"st:{o3}:cooking", chat=group(KITCHEN)))
    log.step("Тимур заказал доставку наличными; кухня приняла и готовит (курьеру: «получить с гостя»)")
    await feed(tap(COOK, f"st:{o3}:cancelled", chat=group(KITCHEN)))
    log.step("Кухня отменила (закончились продукты) — курьерам и гостю сообщение")

    # --- the rest ---
    await feed(message(TIMUR, "/orders"))
    log.step("Тимур смотрит «Мои заказы»")
    await feed(message(OWNER, "/stats"))
    log.step("Владелец: /stats")
    await feed(message(OWNER, "/couriers"))
    log.step("Владелец пишет /couriers в личку — группа курьеров отключена")
    o4 = await order(
        AZIZ,
        lines=burgers,
        method="delivery",
        payment="cash",
        lat=HOME[0],
        lon=HOME[1],
        address="улица Шота Руставели, 12",
    )
    await feed(tap(COOK, f"st:{o4}:accepted", chat=group(KITCHEN)))
    log.step("Новая доставка без группы курьеров — её ведёт кухня")

    # --- spam and news ---
    for _ in range(3):
        r = await web.post(
            "/app/demo/orders",
            json={"lines": espresso, "method": "pickup", "payment": "cash", "phone": "+998 90 000 00 00", "lang": "ru"},
            headers={"X-Telegram-Init-Data": init_data("1:demo", STRANGER)},
        )
    log.step(f"Посторонний шлёт заказы подряд — третий принят, дальше: {r.json().get('error', 'принят')}")
    r = await web.post(
        "/app/demo/orders",
        json={"lines": espresso, "method": "pickup", "payment": "cash", "phone": "+998 90 000 00 00", "lang": "ru"},
        headers={"X-Telegram-Init-Data": init_data("1:demo", STRANGER)},
    )
    log.step(f"Четвёртый заказ подряд, пока кухня не приняла прошлые — отказ: {r.json().get('error')}")
    for uid in (AZIZ, MALIKA, TIMUR):
        await feed(message(uid, "/start"))
    await feed(tap(MALIKA, "news:off"))
    log.step("Гости открыли бота, Малика отписалась от новостей")
    from cafe import news

    news.PACE = 0
    await web.post(f"/login/{store.create_login(cafe_id, OWNER)}")
    await web.post("/promo/news", data={"text": "Новинка: чизкейк с фисташкой −20% до воскресенья 🍰"})
    await asyncio.gather(*news.running)
    log.step("Владелец отправил рассылку из админки — Малике не пришло")
    return log


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "rehearsal.html")
    log = asyncio.run(rehearse())
    out.write_text(log.html())
    print(f"{len(log.steps)} шагов → {out}")


if __name__ == "__main__":
    main()
