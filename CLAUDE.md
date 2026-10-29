# limybot — контекст для Claude

White-label Telegram-бот заказов для кафе Ташкента, который продаётся кафе как продукт. Много кафе в одной
установке: у каждого свой бот (токен), меню и настройки в SQLite. Владелец управляет всем в веб-админке.
Пользователь общается по-русски; тексты бота и админки — на русском, код и комментарии — на английском.

## Структура

```
cafe/
  __main__.py   CLI: run (по умолчанию) | add | token | invite | list
  runner.py     один процесс: Dispatcher на каждое кафе + uvicorn; каждые 15 с сверяет список кафе с БД
  bot.py        build(store, cafe_id, settings) -> Dispatcher; кафе внедряется middleware на каждый апдейт;
                staff_card / send_to_staff / change_status — общие для бота и админки
  store.py      весь SQL (sqlite3, схема в SCHEMA, create if not exists); каждый метод скоупится по cafe_id
  models.py     dataclass'ы (Cafe, Item, ModifierGroup, CartLine, Order...), STATUSES/NEXT, PAYMENT_*
  template.py   pydantic-валидация clients/*.json и импорт в БД (import_cafe)
  config.py     Settings из .env: DB_PATH, PUBLIC_URL, WEB_HOST, WEB_PORT; media_dir = data/photos
  web/          FastAPI-админка: __init__.py (create_app), templates/*.html (Jinja2), static/style.css
clients/        стартовые шаблоны кафе (demo_coffee.json)
tests/          conftest.py (FakeSession — фейковый Telegram, фикстуры demo/store), test_store, test_flow, test_web
```

## Команды

```bash
uv sync                      # зависимости
uv run pytest                # все тесты (без сети)
uv run ruff check . && uv run ruff format .
uv run python -m cafe        # боты + админка на :8000
uv run python -m cafe add demo --token <T> --from clients/demo_coffee.json
```
Windows: `run.cmd [аргументы]`, Linux: `./run.sh [аргументы]` — то же самое плюс создание `.env` и `uv sync`.

## Соглашения и важные решения

- **Один поток.** sqlite3-соединение создаётся в event loop и используется только из него. Поэтому все
  aiogram-фильтры и FastAPI-зависимости/эндпоинты — `async def` (синхронные aiogram/FastAPI гоняют в потоках →
  `sqlite3.ProgrammingError`).
- **Мульти-тенантность:** любые id из callback_data/URL проверяются через `cafe_id` в WHERE. Новые методы store
  обязаны принимать `cafe_id`. Тесты изоляции: `test_cafes_are_isolated`, `test_two_cafes_one_process`,
  `test_admin_sees_only_own_cafe`.
- **callback_data:** `cat:<id>`, `it:<item>[:<opt.opt>]` (карточка с выбранными опциями), `add:<item>:<opts>`,
  `inc:/dec:<cart line id>`, `st:<order id>:<status>`. Лимит Telegram — 64 байта.
- **Модификаторы:** группа `multiple=False` — ровно один вариант (первый по умолчанию), `True` — любое число.
  Валидация выбора — `Store.pick`. Строка корзины = item + отсортированные id опций.
- **Заказ** хранит снимок строк (JSON) — правки меню не меняют старые заказы. Номер заказа сквозной внутри кафе.
  Оплата: `payment_method` (`cash` — при получении; сюда добавятся payme/click) и `payment_status`
  (`pending`/`paid`; наличные становятся `paid` при статусе «Выдан»).
- **Вход в админку:** `/admin` в боте → одноразовая ссылка `PUBLIC_URL/login/<token>` (15 мин) → cookie-сессия
  на 30 дней. Админы кафе — таблица `admins`, пополняется через инвайт-ссылку `t.me/<bot>?start=join-<code>`.
  CSRF закрыт `SameSite=Lax`, поэтому изменения — только POST-формы.
- **Фото** приводятся к JPEG ≤1280px (`web.save_photo`), лежат в `data/photos/`, `photo_file_id` кэширует
  загрузку в Telegram (сбрасывается при смене фото).
- Схема БД пока без миграций (`create table if not exists`). При изменении существующих таблиц — добавить
  простую миграцию по `pragma user_version`.
- Не коммитить `.env`, `data/` (база, фото), токены.
- Тесты пишутся как в `tests/test_flow.py`: апдейты через `dp.feed_update` с `FakeSession`; веб — через
  `httpx.AsyncClient(transport=ASGITransport(app))` в том же event loop.
