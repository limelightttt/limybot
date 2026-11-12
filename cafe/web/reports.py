"""Admin: reports — money and orders over a period, best sellers, days, and the orders as a CSV for Excel."""

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from .. import reports
from ..models import PAYMENT_METHODS, Cafe
from .common import Web

EARLIEST, LATEST = date(2020, 1, 1), date(2100, 12, 31)


def router(web: Web) -> APIRouter:
    r = APIRouter()
    store = web.store
    Admin = Annotated[tuple[Cafe, int], Depends(web.admin)]

    def chosen(cafe: Cafe, p: str, since: str, until: str) -> tuple[date, date, str]:
        """A named period, or the dates typed in (swapped if given backwards, at most a year)."""
        try:
            a, b = date.fromisoformat(since), date.fromisoformat(until)
        except ValueError:
            a, b = reports.period(cafe, p)
            return a, b, p if p in reports.PERIODS else "today"
        a, b = (min(max(d, EARLIEST), LATEST) for d in (a, b))  # 0001-01-01 or 9999-12-31 break date maths
        a, b = min(a, b), max(a, b)
        return max(a, b - timedelta(days=366)), b, ""  # at most a year: a report, not a dump

    @r.get("/reports")
    async def page(request: Request, admin: Admin, p: str = "today", since: str = "", until: str = ""):
        cafe, _ = admin
        a, b, name = chosen(cafe, p, since, until)
        report = reports.build(store, cafe, a, b)
        return web.page(
            request,
            "reports.html",
            cafe,
            r=report,
            periods=reports.PERIODS,
            p=name,
            methods=PAYMENT_METHODS,
            best=max((total for _, _, total in report.days), default=0),
        )

    @r.get("/reports.csv")
    async def export(admin: Admin, p: str = "today", since: str = "", until: str = ""):
        cafe, _ = admin
        a, b, _ = chosen(cafe, p, since, until)
        name = f"{cafe.slug}-{a:%Y%m%d}-{b:%Y%m%d}.csv"
        return Response(
            reports.csv_export(store, cafe, a, b),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    return r
