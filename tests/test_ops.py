"""The operator panel through HTTP: login, connecting cafes (templates, zip packs, mistakes), the cafe page actions."""

import io
import json
import zipfile

import httpx
import pytest
from PIL import Image

from cafe.store import OPERATOR, OPS
from cafe.web import create_app
from tests.conftest import FakeBots

OWNER = 7
TOKEN = "5:marry"


@pytest.fixture
def ops(store, demo, settings, fake):
    bot, _ = fake
    store.join(demo.id, store.new_invite(demo.id), OWNER, "Owner")
    bots = FakeBots({demo.id: bot})
    bots.tokens[TOKEN] = ("marry_me_bot", "Marry Me")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(store, settings, bots)), base_url="https://admin.example.uz"
    )
    return client, bots


async def login(client, store):
    r = await client.get(f"/ops/login/{store.create_login(OPS, OPERATOR)}")
    assert r.status_code == 303 and r.headers["location"] == "/ops"


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (600, 400), "gold").save(buf, "PNG")
    return buf.getvalue()


def pack(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


MENU = {
    "name": "Marry Me Street Foods",
    "brand_color": "#FFC72C",
    "logo": "logo.png",
    "menu": [{"category": "Бургеры", "items": [{"name": "Классик", "price": 75000, "photo": "photos/classic.jpg"}]}],
}


async def test_only_the_operator_gets_in(ops, store, demo):
    client, _ = ops
    r = await client.get("/ops")
    assert r.status_code == 401 and "./run.sh ops" in r.text
    owner_link = store.create_login(demo.id, OWNER)
    assert (await client.get(f"/ops/login/{owner_link}")).status_code == 401  # an owner's /admin link is not for /ops
    ops_link = store.create_login(OPS, OPERATOR)
    assert (await client.get(f"/login/{ops_link}")).status_code == 401  # and the other way round
    await client.get(f"/login/{store.create_login(demo.id, OWNER)}")
    assert (await client.get("/ops")).status_code == 401  # an owner's session does not open /ops
    await login(client, store)
    page = (await client.get("/ops")).text
    assert "Demo Coffee" in page and "Работает" in page
    await client.post("/ops/logout")
    assert (await client.get("/ops")).status_code == 401


async def test_connect_a_cafe_from_a_template(ops, store):
    client, bots = ops
    await login(client, store)
    form = (await client.get("/ops/new")).text
    assert 'value="demo_coffee.json"' in form and "Demo Coffee" in form
    r = await client.post("/ops/new", data={"token": f"  {TOKEN} ", "template": "demo_coffee.json"})
    cafe = store.cafe_by_slug("marry_me")
    assert r.status_code == 303 and r.headers["location"].startswith(f"/ops/cafes/{cafe.id}")
    assert (cafe.name, cafe.bot_token, cafe.bot_username) == ("Demo Coffee", TOKEN, "marry_me_bot")
    assert store.menu(cafe.id) and bots.synced == 1  # the bot starts right away, not in 15 seconds
    page = (await client.get(r.headers["location"])).text
    assert "Владелец ещё не подключился" in page and cafe.invite_link() in page

    again = await client.post("/ops/new", data={"token": TOKEN})
    assert again.status_code == 200 and "уже подключён — «Demo Coffee»" in again.text


async def test_connect_with_an_empty_menu_and_mistakes(ops, store):
    client, _ = ops
    await login(client, store)
    r = await client.post("/ops/new", data={"token": "123:wrong"})
    assert r.status_code == 200 and "Telegram не принял токен" in r.text and 'value="123:wrong"' in r.text
    r = await client.post("/ops/new", data={"token": TOKEN, "template": "../.env.example"})
    assert "Такого шаблона нет" in r.text and not store.cafe_by_token(TOKEN)
    await client.post("/ops/new", data={"token": TOKEN})
    cafe = store.cafe_by_token(TOKEN)
    assert cafe.name == "Marry Me" and store.menu(cafe.id, only_available=False) == []  # named after the bot


async def test_connect_from_a_zip_pack(ops, store, settings):
    client, _ = ops
    await login(client, store)
    good = pack(
        {
            "marry_me/marry_me.json": json.dumps(MENU).encode(),
            "marry_me/logo.png": png(),
            "marry_me/photos/classic.jpg": png(),
            "__MACOSX/marry_me/._marry_me.json": b"junk a Mac adds",
        }
    )
    r = await client.post("/ops/new", data={"token": TOKEN}, files={"pack": ("marry_me.zip", good, "application/zip")})
    assert r.status_code == 303
    cafe = store.cafe_by_token(TOKEN)
    burger = store.menu(cafe.id)[0].items[0]
    assert (cafe.name, cafe.brand_color) == ("Marry Me Street Foods", "#ffc72c")
    assert (settings.media_dir / cafe.logo).is_file() and (settings.media_dir / burger.photo).is_file()


@pytest.mark.parametrize(
    ("files", "error"),
    [
        ({"../evil.json": b"{}"}, "за пределами папки"),
        ({"a.json": json.dumps(MENU).encode(), "b.json": b"{}"}, "ровно один .json"),
        ({"m.json": json.dumps(MENU).encode()}, "pictures not found"),  # the photos were left out of the zip
        (None, "Это не zip-архив"),
    ],
)
async def test_bad_packs_are_explained(ops, store, files, error):
    client, _ = ops
    await login(client, store)
    data = pack(files) if files else b"not a zip"
    r = await client.post("/ops/new", data={"token": TOKEN}, files={"pack": ("p.zip", data, "application/zip")})
    assert r.status_code == 200 and error in r.text and not store.cafe_by_token(TOKEN)


async def test_cafe_page_actions(ops, store, demo, settings):
    client, _ = ops
    await login(client, store)
    url = f"/ops/cafes/{demo.id}"
    store.set_bot_username(demo.id, "demo_bot")
    old = store.cafe(demo.id).invite_link()
    await client.post(f"{url}/invite")
    assert store.cafe(demo.id).invite_link() not in (None, old)

    await client.post(f"{url}/paid", data={"until": "2026-12-31"})
    assert str(store.cafe(demo.id).paid_until) == "2026-12-31"
    r = await client.post(f"{url}/paid", data={"until": "завтра"})
    assert "msg=" in r.headers["location"] and str(store.cafe(demo.id).paid_until) == "2026-12-31"
    await client.post(f"{url}/paid", data={"until": ""})
    assert store.cafe(demo.id).paid_until is None

    await client.post(f"{url}/enabled", data={})
    assert not store.cafe(demo.id).enabled
    assert "Приостановлено" in (await client.get(url)).text
    await client.post(f"{url}/enabled", data={"on": "1"})
    assert store.cafe(demo.id).enabled

    await client.post(f"{url}/token", data={"token": "9:bad"})
    assert store.cafe(demo.id).bot_token == "1:demo"
    await client.post(f"{url}/token", data={"token": TOKEN})
    assert (store.cafe(demo.id).bot_token, store.cafe(demo.id).bot_username) == (TOKEN, "marry_me_bot")


async def test_operator_fills_in_a_cafe_for_the_owner(ops, store, demo):
    client, _ = ops
    await login(client, store)
    r = await client.post(f"/ops/cafes/{demo.id}/admin")
    assert r.status_code == 303 and r.headers["location"] == "/orders"
    page = (await client.get("/settings")).text
    assert "Вы в админке как оператор" in page and "Demo Coffee" in page
    await client.post("/categories", data={"name": "Завтраки"})
    assert "Завтраки" in [c.name for c in store.menu(demo.id, only_available=False)]


async def test_delete_a_cafe(ops, store, demo, settings):
    client, _ = ops
    await login(client, store)
    await client.post(
        "/ops/new",
        files={
            "pack": (
                "p.zip",
                pack({"m/m.json": json.dumps(MENU).encode(), "m/logo.png": png(), "m/photos/classic.jpg": png()}),
                "application/zip",
            )
        },
        data={"token": TOKEN},
    )
    cafe = store.cafe_by_token(TOKEN)
    pictures = [cafe.logo, store.menu(cafe.id)[0].items[0].photo]
    r = await client.post(f"/ops/cafes/{cafe.id}/delete", data={"confirm": "не то"})
    assert store.cafe(cafe.id) and "delete" in r.headers["location"]
    r = await client.post(f"/ops/cafes/{cafe.id}/delete", data={"confirm": cafe.slug})
    assert r.headers["location"].startswith("/ops?") and store.cafe(cafe.id) is None
    assert not any((settings.media_dir / p).exists() for p in pictures)
    assert store.cafe(demo.id) and store.menu(demo.id)  # the other cafe is untouched
    await client.post("/ops/new", data={"token": TOKEN})  # the bot is free again
    assert store.cafe_by_token(TOKEN).slug == "marry_me"


async def test_login_by_a_localhost_link_works_in_any_browser(ops, store, settings):
    client, _ = ops
    local = httpx.AsyncClient(transport=client._transport, base_url="http://localhost:8000")
    r = await local.get(f"/ops/login/{store.create_login(OPS, OPERATOR)}")
    assert "secure" not in r.headers["set-cookie"].lower()  # Safari drops https-only cookies on http://localhost
    r = await client.get(f"/ops/login/{store.create_login(OPS, OPERATOR)}")
    assert "secure" in r.headers["set-cookie"].lower()
