"""Starting content for a new cafe: clients/<name>.json is validated here and copied into the database."""

from datetime import time
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

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
    card_number: str = ""  # for payment by card transfer; empty: cash only
    card_holder: str = ""
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


def import_cafe(store: Store, slug: str, bot_token: str, template: Template) -> int:
    t = template
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
        card_number=t.card_number,
        card_holder=t.card_holder,
    )
    groups = {}
    for g in t.modifiers:
        groups[g.name] = store.add_group(cafe_id, g.name, g.multiple)
        for o in g.options:
            store.add_option(cafe_id, groups[g.name], o.name, o.price)
    for cat in t.menu:
        category_id = store.add_category(cafe_id, cat.category)
        for it in cat.items:
            store.add_item(cafe_id, category_id, it.name, it.price, it.description, [groups[m] for m in it.modifiers])
    return cafe_id


def load_template(path: Path) -> Template:
    return Template.model_validate_json(path.read_text("utf-8"))
