"""Admin: categories, items with photos, the stop-list, and modifier groups (size, milk, syrups)."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Path, Request, UploadFile
from PIL import Image, UnidentifiedImageError

from ..media import drop_photo, save_photo
from ..models import Cafe
from .common import Web, back


def router(web: Web) -> APIRouter:
    r = APIRouter()
    store, media = web.store, web.settings.media_dir
    Admin = Annotated[tuple[Cafe, int], Depends(web.admin)]

    @r.get("/menu")
    async def menu(request: Request, admin: Admin):
        cafe, _ = admin
        return web.page(request, "menu.html", cafe, categories=store.menu(cafe.id, only_available=False))

    @r.post("/categories")
    async def add_category(admin: Admin, name: Annotated[str, Form()]):
        store.add_category(admin[0].id, name.strip())
        return back("/menu")

    @r.post("/categories/{category_id}")
    async def rename_category(admin: Admin, category_id: int, name: Annotated[str, Form()]):
        store.rename_category(admin[0].id, category_id, name.strip())
        return back("/menu")

    @r.post("/categories/{category_id}/move")
    async def move_category(admin: Admin, category_id: int, step: Annotated[int, Form()]):
        store.move_category(admin[0].id, category_id, step)
        return back("/menu")

    @r.post("/categories/{category_id}/delete")
    async def delete_category(admin: Admin, category_id: int):
        if not store.delete_category(admin[0].id, category_id):
            return back("/menu", "В разделе есть позиции: сначала удалите или перенесите их")
        return back("/menu")

    def item_page(request: Request, cafe: Cafe, item, category_id: int):
        return web.page(
            request,
            "item.html",
            cafe,
            item=item,
            category_id=category_id,
            categories=store.menu(cafe.id, only_available=False),
            groups=store.groups(cafe.id),
        )

    @r.get("/items/new")
    async def new_item(request: Request, admin: Admin, category_id: int):
        return item_page(request, admin[0], None, category_id)

    @r.get("/items/{item_id}")
    async def edit_item(request: Request, admin: Admin, item_id: int):
        cafe, _ = admin
        it = store.item(cafe.id, item_id, only_available=False)
        if not it:
            return back("/menu")
        return item_page(request, cafe, it, it.category_id)

    @r.post("/items/{item_id}")
    async def save_item(
        admin: Admin,
        item_id: Annotated[str, Path(pattern=r"^(new|\d+)$")],
        name: Annotated[str, Form()],
        price: Annotated[int, Form(ge=0)],
        category_id: Annotated[int, Form()],
        description: Annotated[str, Form(max_length=500)] = "",
        groups: Annotated[list[int] | None, Form()] = None,
        photo: Annotated[UploadFile | None, File()] = None,
        remove_photo: Annotated[bool, Form()] = False,
    ):
        cafe, _ = admin
        fields = {"name": name.strip(), "description": description.strip(), "price": price}
        groups = groups or []
        if item_id == "new":
            saved = store.add_item(cafe.id, category_id, group_ids=groups, **fields)
        else:
            ok = store.update_item(cafe.id, int(item_id), category_id=category_id, group_ids=groups, **fields)
            saved = int(item_id) if ok else None
        if saved is None:
            return back("/menu")
        data = await photo.read() if photo else b""
        if data:
            try:
                filename = save_photo(data, media)
            except (UnidentifiedImageError, Image.DecompressionBombError):
                return back(f"/items/{saved}", "Не получилось прочитать фото — загрузите JPG или PNG")
            drop_photo(media, store.set_photo(cafe.id, saved, filename))
        elif remove_photo:
            drop_photo(media, store.set_photo(cafe.id, saved, None))
        return back("/menu", f"«{fields['name']}» сохранено")

    @r.post("/items/{item_id}/available")
    async def toggle_item(admin: Admin, item_id: int, available: Annotated[bool, Form()] = False):
        store.set_available(admin[0].id, item_id, available)
        return back(f"/menu#item-{item_id}")

    @r.post("/items/{item_id}/move")
    async def move_item(admin: Admin, item_id: int, step: Annotated[int, Form()]):
        store.move_item(admin[0].id, item_id, step)
        return back(f"/menu#item-{item_id}")

    @r.post("/items/{item_id}/delete")
    async def delete_item(admin: Admin, item_id: int):
        drop_photo(media, store.delete_item(admin[0].id, item_id))
        return back("/menu")

    # --- modifiers ---
    @r.get("/modifiers")
    async def modifiers(request: Request, admin: Admin):
        cafe, _ = admin
        return web.page(request, "modifiers.html", cafe, groups=store.groups(cafe.id))

    @r.post("/modifiers")
    async def add_group(admin: Admin, name: Annotated[str, Form()], multiple: Annotated[bool, Form()] = False):
        group_id = store.add_group(admin[0].id, name.strip(), multiple)
        return back(f"/modifiers#group-{group_id}")

    @r.post("/modifiers/{group_id}")
    async def update_group(
        admin: Admin,
        group_id: int,
        name: Annotated[str, Form()],
        multiple: Annotated[bool, Form()] = False,
    ):
        store.update_group(admin[0].id, group_id, name.strip(), multiple)
        return back(f"/modifiers#group-{group_id}")

    @r.post("/modifiers/{group_id}/delete")
    async def delete_group(admin: Admin, group_id: int):
        store.delete_group(admin[0].id, group_id)
        return back("/modifiers")

    @r.post("/modifiers/{group_id}/options")
    async def add_option(
        admin: Admin,
        group_id: int,
        name: Annotated[str, Form()],
        price: Annotated[int, Form(ge=0)] = 0,
    ):
        store.add_option(admin[0].id, group_id, name.strip(), price)
        return back(f"/modifiers#group-{group_id}")

    @r.post("/options/{option_id}")
    async def update_option(
        admin: Admin,
        option_id: int,
        name: Annotated[str, Form()],
        price: Annotated[int, Form(ge=0)] = 0,
    ):
        store.update_option(admin[0].id, option_id, name.strip(), price)
        return back("/modifiers")

    @r.post("/options/{option_id}/delete")
    async def delete_option(admin: Admin, option_id: int):
        store.delete_option(admin[0].id, option_id)
        return back("/modifiers")

    return r
