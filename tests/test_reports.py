"""Reports must add up: revenue without cancelled orders, goods + delivery = revenue, the split by payment, what is
confirmed paid, best sellers from the orders' own snapshots, days in the cafe's time zone, the CSV for Excel, the
owner's morning summary — and nothing from another cafe."""

import csv
import io
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from cafe import reports
from cafe.notify import SUMMARY_HOUR, Notifier
from cafe.web import create_app
from tests.conftest import FakeBots, order, pick

OWNER = 7


@pytest.fixture
def week(store, demo, other):
    """Orders of the demo cafe today and yesterday; another cafe that must never see them."""
    store.update_cafe(demo.id, card_number="8600 1234 5678 9012", card_holder="A")
    cafe = store.cafe(demo.id)
    latte = pick(store, demo.id, "Латте", ["M", "овсяное", "ваниль"], 2)  # 2 × 44 000
    espresso = pick(store, demo.id, "Эспрессо", qty=3)  # 3 × 18 000
    a = order(store, cafe, 100, latte)  # 88 000 pickup cash, handed over → paid
    store.set_status(demo.id, a.id, "done")
    order(store, cafe, 101, espresso, method="delivery", address="Юнусабад", payment_method="transfer")
    # 54 000 + 15 000 delivery = 69 000, transfer not confirmed yet
    c = order(store, cafe, 102, espresso, payment_method="transfer")  # 54 000 transfer, confirmed
    store.confirm_payment(demo.id, c.id)
    d = order(store, cafe, 103, latte)  # 88 000 — cancelled: not revenue
    store.set_status(demo.id, d.id, "cancelled")
    e = order(store, cafe, 104, espresso)  # yesterday: 54 000
    yesterday = datetime.now(UTC) - timedelta(days=1)
    store.db.execute("update orders set created_at = ? where id = ?", (yesterday.isoformat(), e.id))
    return cafe, other


def test_the_numbers_add_up(store, week):
    cafe, _ = week
    today = cafe.now().date()
    r = reports.build(store, cafe, today, today)
    assert (r.orders, r.revenue) == (3, 88000 + 69000 + 54000)
    assert r.goods + r.delivery == r.revenue and r.delivery == 15000
    assert (r.delivery_orders, r.pickup_orders) == (1, 2)
    assert r.by_method == {"cash": 88000, "transfer": 69000 + 54000}
    assert r.paid == 88000 + 54000 and r.unpaid_transfers == 69000
    assert (r.cancelled, r.cancelled_sum) == (1, 88000)
    assert r.average == (88000 + 69000 + 54000) // 3
    assert r.top == [("Эспрессо", 6, 108000), ("Латте (M, овсяное, ваниль)", 2, 88000)]
    assert r.days == [(today, 3, 211000)]

    week_report = reports.build(store, cafe, *reports.period(cafe, "week"))
    assert week_report.orders == 4 and week_report.revenue == 211000 + 54000
    assert sum(n for _, n, _ in week_report.days) == 4 and len(week_report.days) == 7
    assert reports.build(store, cafe, *reports.period(cafe, "yesterday")).revenue == 54000


def test_menu_changes_do_not_rewrite_reports(store, week):
    cafe, _ = week
    today = cafe.now().date()
    before = reports.build(store, cafe, today, today)
    store.db.execute("update items set price = price * 10, name = 'Новое' where cafe_id = ?", (cafe.id,))
    assert reports.build(store, cafe, today, today) == before


def test_csv_opens_in_excel(store, week):
    cafe, _ = week
    today = cafe.now().date()
    data = reports.csv_export(store, cafe, today, today)
    assert data.startswith("﻿".encode())
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig")), delimiter=";"))
    assert rows[0][:4] == ["Номер", "Дата", "Время", "Статус"] and len(rows) == 1 + 4  # cancelled ones listed too
    delivery = next(r for r in rows if r[4] == "Доставка")
    assert delivery[9:12] == ["54000", "15000", "69000"] and delivery[5] == "Юнусабад"


@pytest.fixture
def client(store, demo, settings, fake):
    bot, _ = fake
    store.join(demo.id, store.new_invite(demo.id), OWNER, "Owner")
    app = create_app(store, settings, FakeBots({demo.id: bot}))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://admin.example.uz")


async def test_reports_page_and_download(client, store, week):
    cafe, other = week
    await client.get(f"/login/{store.create_login(cafe.id, OWNER)}")
    page = (await client.get("/reports")).text
    assert "211 000 сум" in page and "Переводы без подтверждения" in page and "69 000 сум" in page
    assert "Эспрессо" in page and "Отменено (1)" in page
    week_page = (await client.get("/reports?p=week")).text
    assert "265 000 сум" in week_page and "По дням" in week_page
    today = cafe.now().date().isoformat()
    backwards = (await client.get(f"/reports?since={today}&until=2020-01-01")).text
    assert "Выручка" in backwards  # dates the wrong way round or too far apart are fixed, not an error
    r = await client.get(f"/reports.csv?since={today}&until={today}")
    assert r.headers["content-disposition"].startswith('attachment; filename="demo-') and "Юнусабад" in r.text
    store.join(other.id, store.new_invite(other.id), OWNER, "Owner")
    await client.get(f"/login/{store.create_login(other.id, OWNER)}")
    assert "Эспрессо" not in (await client.get("/reports?p=week")).text  # another cafe sees only its own


async def test_morning_summary_for_the_owner(store, demo, settings, fake, week):
    cafe, _ = week
    bot, session = fake
    store.join(demo.id, store.new_invite(demo.id), OWNER, "Owner")
    notifier = Notifier(store, settings, {demo.id: bot}.get, lambda _: "running")
    tomorrow = cafe.now().replace(hour=SUMMARY_HOUR - 1) + timedelta(days=1)
    await notifier.tick(tomorrow.astimezone(UTC))
    assert not any(t.startswith("Итоги") for t in session.texts(OWNER))
    morning = tomorrow.replace(hour=SUMMARY_HOUR, minute=1)
    await notifier.tick(morning.astimezone(UTC))
    await notifier.tick(morning.astimezone(UTC))
    summaries = [t for t in session.texts(OWNER) if t.startswith("Итоги")]
    assert len(summaries) == 1
    assert "Заказов: 3 на 211 000 сум" in summaries[0] and "Переводы без подтверждения: 69 000 сум" in summaries[0]
    assert "Отменено: 1" in summaries[0] and "Эспрессо × 6" in summaries[0]
