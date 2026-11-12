"""The address line under the pin: what OpenStreetMap answers becomes "street, house"; one request per place, failures
are quiet, and only Telegram users may ask."""

import httpx
import pytest

from cafe import geocode
from cafe.web import create_app
from tests.conftest import FakeBots, init_data


def test_a_line_from_openstreetmap():
    assert geocode.line({"address": {"road": "улица Амира Темура", "house_number": "15"}}) == "улица Амира Темура, 15"
    assert geocode.line({"address": {"road": "улица Бобура"}}) == "улица Бобура"
    assert geocode.line({"address": {"neighbourhood": "махалля Олмазор", "house_number": "7"}}) == "махалля Олмазор, 7"
    assert geocode.line({"display_name": "Чиланзар-9, Чиланзарский район, Ташкент", "address": {}}) == (
        "Чиланзар-9, Чиланзарский район"
    )
    assert geocode.line({"address": {}}) is None


@pytest.fixture
def calls(monkeypatch):
    seen = []

    async def fake(lat, lon, lang):
        seen.append((lat, lon, lang))
        return {"address": {"road": "улица Шота Руставели", "house_number": "12"}}

    monkeypatch.setattr(geocode, "fetch", fake)
    monkeypatch.setattr(geocode, "SPACING", 0)
    geocode._cache.clear()
    return seen


async def test_lookups_are_cached(calls):
    assert await geocode.address(41.29701, 69.27012) == "улица Шота Руставели, 12"
    assert await geocode.address(41.29702, 69.27013) == "улица Шота Руставели, 12"  # the same ~10 m: no new request
    assert len(calls) == 1
    await geocode.address(41.29701, 69.27012, "uz")
    assert len(calls) == 2


async def test_failures_are_quiet(monkeypatch):
    async def down(*_):
        raise TimeoutError

    monkeypatch.setattr(geocode, "fetch", down)
    geocode._cache.clear()
    assert await geocode.address(41.3, 69.2) is None


async def test_the_mini_app_asks_for_the_line(calls, store, demo, settings):
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(store, settings, FakeBots({}))), base_url="https://admin.example.uz"
    )
    url = "/app/demo/where?lat=41.29701&lon=69.27012&lang=ru"
    assert (await client.get(url)).status_code == 401  # only from inside Telegram
    r = await client.get(url, headers={"X-Telegram-Init-Data": init_data("1:demo", 100)})
    assert r.json() == {"address": "улица Шота Руставели, 12"}
    bad = await client.get("/app/demo/where?lat=500&lon=0", headers={"X-Telegram-Init-Data": init_data("1:demo", 100)})
    assert bad.status_code == 422
