"""Starting content for a new cafe: clients/<name>.json is validated here and copied into the database.
Pictures (logo, cover, item photos) are paths relative to the JSON file, so a client pack is one folder."""

import re
import zipfile
from datetime import date, time
from io import BytesIO
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .media import save_photo
from .models import APP_THEMES
from .store import Store


class Hours(BaseModel):
    open: time
    close: time


class Delivery(BaseModel):
    enabled: bool = False
    min_order: int = 0
    fee: int = 0


class OptionIn(BaseModel):
    name: str
    price: int = 0


class GroupIn(BaseModel):
    name: str
    multiple: bool = False
    options: list[OptionIn]


class ItemIn(BaseModel):
    name: str
    price: int
    description: str = ""
    modifiers: list[str] = []
    photo: Path | None = None


class CategoryIn(BaseModel):
    category: str
    items: list[ItemIn]


class Template(BaseModel):
    name: str
    greeting: str = ""
    currency: str = "сум"
    timezone: str = "Asia/Tashkent"
    hours: Hours | None = None
    pickup_address: str = ""
    delivery: Delivery = Field(default_factory=Delivery)
    payment_note: str = "Оплата наличными или картой при получении"
    brand_color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    app_theme: Literal[tuple(APP_THEMES)] = "auto"
    logo: Path | None = None
    cover: Path | None = None
    modifiers: list[GroupIn] = []
    menu: list[CategoryIn] = []

    @model_validator(mode="after")
    def _known_modifiers(self):
        names = {g.name for g in self.modifiers}
        for cat in self.menu:
            for it in cat.items:
                if unknown := set(it.modifiers) - names:
                    raise ValueError(f"{it.name}: unknown modifiers {sorted(unknown)}")
        return self

    def pictures(self) -> list[Path]:
        return [p for p in (self.logo, self.cover, *(it.photo for c in self.menu for it in c.items)) if p]


def import_cafe(store: Store, slug: str, bot_token: str, template: Template, media_dir: Path | None = None) -> int:
    t = template
    if t.pictures() and media_dir is None:
        raise ValueError("the template has pictures: pass media_dir")

    def picture(path: Path) -> str:
        return save_photo(path.read_bytes(), media_dir)

    cafe_id = store.add_cafe(
        slug,
        bot_token,
        name=t.name,
        greeting=t.greeting or f"Привет! Это {t.name}. Выберите, что заказать 👇",
        currency=t.currency,
        timezone=t.timezone,
        open_time=t.hours.open if t.hours else None,
        close_time=t.hours.close if t.hours else None,
        pickup_address=t.pickup_address,
        delivery_enabled=t.delivery.enabled,
        delivery_min=t.delivery.min_order,
        delivery_fee=t.delivery.fee,
        payment_note=t.payment_note,
        app_theme=t.app_theme,
        **({"brand_color": t.brand_color.lower()} if t.brand_color else {}),
    )
    for kind in ("logo", "cover"):
        if path := getattr(t, kind):
            store.set_cafe_image(cafe_id, kind, picture(path))
    groups = {}
    for g in t.modifiers:
        groups[g.name] = store.add_group(cafe_id, g.name, g.multiple)
        for o in g.options:
            store.add_option(cafe_id, groups[g.name], o.name, o.price)
    for cat in t.menu:
        category_id = store.add_category(cafe_id, cat.category)
        for it in cat.items:
            item_id = store.add_item(
                cafe_id, category_id, it.name, it.price, it.description, [groups[m] for m in it.modifiers]
            )
            if it.photo:
                store.set_photo(cafe_id, item_id, picture(it.photo))
    return cafe_id


def load_template(path: Path) -> Template:
    """Reads clients/<name>.json; picture paths become relative to the file and must exist."""
    t = Template.model_validate_json(path.read_text("utf-8"))
    base = path.parent
    t.logo, t.cover = (base / p if p else None for p in (t.logo, t.cover))
    for cat in t.menu:
        for it in cat.items:
            it.photo = base / it.photo if it.photo else None
    if missing := [str(p) for p in t.pictures() if not p.is_file()]:
        raise ValueError(f"{path}: pictures not found: {', '.join(missing)}")
    return t


def available_templates(clients: Path) -> list[tuple[str, str]]:
    """(path relative to clients/, cafe name) of every template there that loads: clients/*.json and the packs in
    clients/private/<brand>/*.json. A broken file is just not offered."""
    found = []
    for path in sorted([*clients.glob("*.json"), *clients.glob("private/*/*.json")]):
        try:
            found.append((path.relative_to(clients).as_posix(), load_template(path).name))
        except ValueError:
            continue
    return found


MAX_PACK = 50 * 1024 * 1024


def unpack_pack(data: bytes, into: Path) -> Path:
    """A client pack sent as a zip (the folder with the JSON and its pictures) → the JSON inside.
    ValueError with a message for the operator when the archive is not a usable pack."""
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("Это не zip-архив. Нужен архив папки, где лежат .json с меню и фото")
    with archive:
        files = [f for f in archive.infolist() if not f.is_dir() and not f.filename.startswith("__MACOSX/")]
        if sum(f.file_size for f in files) > MAX_PACK:
            raise ValueError("Архив больше 50 МБ — уменьшите фото")
        root = into.resolve()
        if any(not (root / f.filename).resolve().is_relative_to(root) for f in files):
            raise ValueError("В архиве файлы с путями за пределами папки — такой архив не открываю")
        archive.extractall(into, members=files)
    menus = list(into.rglob("*.json"))
    if len(menus) != 1:
        raise ValueError(f"В архиве должен быть ровно один .json с меню, а их {len(menus)}")
    return menus[0]


def slug_for(username: str) -> str:
    """The cafe's short name (in URLs and the CLI) from its bot: limy_coffee_bot → limy_coffee."""
    return re.sub(r"_?bot$", "", re.sub(r"[^a-z0-9_-]", "", username.lower())) or "cafe"


def connect_cafe(
    store: Store,
    media_dir: Path,
    token: str,
    username: str,
    bot_name: str,
    template: Template | None,
    paid_until: date | None = None,
) -> int:
    """A new cafe on a checked bot token: menu and look from the template (an empty menu named after the bot without
    one), a short name from the bot's username, an invite link for the owner and the trial period (paid_until;
    None — no limit, for a demo)."""
    media_dir.mkdir(parents=True, exist_ok=True)
    cafe_id = import_cafe(
        store, store.free_slug(slug_for(username)), token, template or Template(name=bot_name), media_dir
    )
    store.set_bot_username(cafe_id, username)
    store.set_paid_until(cafe_id, paid_until)
    store.new_invite(cafe_id)
    return cafe_id
