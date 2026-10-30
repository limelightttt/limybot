# limybot — контекст для Claude

White-label Telegram-бот заказов для кафе Ташкента, продаётся кафе по подписке (SaaS): одна установка на сервере
оператора, много кафе. У каждого кафе свой бот (токен), меню, оформление и настройки в SQLite. Гости заказывают в
Telegram Mini App, владелец управляет всем в веб-админке. Пользователь общается по-русски; тексты бота и админки —
на русском (гостю — ещё и на узбекском), код и комментарии — на английском.

## Структура

```
cafe/
  __main__.py   CLI: run (по умолчанию) | add | token | invite | paid | disable | enable | backup | list
  runner.py     один процесс: Dispatcher на каждое кафе + uvicorn; каждые 15 с сверяет кафе с БД; кнопка «Меню»
                и команды бота; раз в день бэкап
  bot.py        build(store, cafe_id, settings) -> Dispatcher: приветствие, «Мои заказы», отмена, «Я перевёл»,
                кнопки персонала и курьеров, /admin /staff /couriers /finance /report, отчёты. Общие для бота, админки и Mini App: staff_card,
                send_to_staff, refresh_staff, send_to_customer, change_status, mark_paid
  store.py      весь SQL (sqlite3); SCHEMA — текущая схема, MIGRATIONS — шаги для старых баз; методы скоупятся по cafe_id
  models.py     dataclass'ы (Cafe, Item, CartLine, Order, Customer...), STATUSES/FLOW, PAYMENT_*
  i18n.py       тексты гостю ru/uz: BOT_TEXTS (бот), APP_TEXTS (Mini App), STATUS, order_text
  media.py      фото: JPEG ≤1280px + превью ≤480px (thumb), удаление, превью для старых фото
  backup.py     data/backups/limybot-<дата>.zip (база + фото), хранит KEEP=14
  template.py   pydantic-валидация clients/*.json и импорт в БД (import_cafe)
  config.py     Settings из .env: DB_PATH, PUBLIC_URL, WEB_HOST, WEB_PORT, SUPPORT
  web/          FastAPI, модуль на фичу, каждый отдаёт router(web: Web):
    common.py     Web (store, settings, bot_for, templates, admin-зависимость, page), back()
    orders.py menu.py cafe_settings.py promo.py — админка владельца (templates/admin/*.html, static/style.css)
    miniapp.py    витрина гостя /app/<slug> (templates/app/index.html, static/app.js, static/app.css)
clients/        стартовые шаблоны кафе (demo_coffee.json)
tests/          conftest.py (FakeSession, фикстуры, order(), init_data()), test_store, test_flow, test_miniapp,
                test_web, schema_v0.sql (схема до миграций — для теста миграции)
Dockerfile, compose.yaml, Caddyfile — деплой: docker compose up -d, Caddy сам выпускает HTTPS для DOMAIN
```

## Команды

```bash
uv sync                      # зависимости
uv run pytest                # все тесты (без сети)
uv run ruff check . && uv run ruff format .
uv run python -m cafe        # боты + админка + Mini App на :8000
uv run python -m cafe add demo --token <T> --from clients/demo_coffee.json
```
Windows: `run.cmd [аргументы]`, Linux: `./run.sh [аргументы]`. Сервер: `docker compose up -d`, CLI —
`docker compose exec app python -m cafe ...`.

## Соглашения и важные решения

- **Один поток.** sqlite3-соединение создаётся в event loop и используется только из него. Все aiogram-фильтры и
  FastAPI-зависимости/эндпоинты — `async def` (синхронные гоняются в потоках → `sqlite3.ProgrammingError`).
  Тяжёлое без БД (zip бэкапа) — через `asyncio.to_thread`.
- **Мульти-тенантность:** любые id из callback_data/URL/тела запроса проверяются через `cafe_id` в WHERE, заказы
  гостя — ещё и через `user_id`. Новые методы store обязаны принимать `cafe_id`. Тесты изоляции:
  `test_cafes_are_isolated`, `test_two_cafes_one_process`, `test_admin_sees_only_own_cafe`.
- **Кнопки персонала** (`st:`, `pay:`) работают только в группе персонала или у админа (`is_staff`): Telegram не
  проверяет, что callback_data пришла от настоящей кнопки, клиент может её подделать.
- **Без сборки фронта.** Админка — Jinja + CSS, Mini App — Jinja + один `app.js` (vanilla) + `app.css`. SPA/React/FSD
  не вводим: это второй деплой и node в проекте ради нескольких экранов.
- **Mini App:** страница и меню публичные; всё про гостя (`/me`, `/lang`, `/orders`) — с заголовком
  `X-Telegram-Init-Data`, проверяется `safe_parse_webapp_init_data` токеном бота кафе, не старше суток.
  Корзина живёт на телефоне (localStorage), сервер перепроверяет каждую строку через `Store.lines`.
  Ошибки заказа — 409 `{"error": код}`, текст — `APP_TEXTS[lang]["error_" + код]`. Telegram открывает Mini App
  только по https: без https в `PUBLIC_URL` бот работает, но без кнопки меню.
- **Модификаторы:** группа `multiple=False` — ровно один вариант (первый по умолчанию), `True` — любое число.
  Валидация выбора — `Store.pick` (та же логика продублирована в `app.js` для UI).
- **Заказ** хранит снимок строк (JSON: item_id, option_ids, name, options, qty, unit) — правки меню не меняют старые
  заказы; ids нужны для «Повторить» (по текущим ценам; чего нет — перечисляется). Номер сквозной внутри кафе.
  У самовывоза адрес не сохраняется никогда (`store.place`).
- **Статусы** (`models.FLOW`): самовывоз `new → accepted → cooking → ready → done`, доставка
  `new → accepted → cooking → courier → delivered`; `cancelled` — персоналом до `ready`/`courier`, гостем только из
  `new` (одним UPDATE ... WHERE status='new', без гонки с «Принять»). Гостю пишем на accepted, cooking, ready,
  courier, cancelled (тексты в `i18n.BOT_TEXTS["status_*"]`); на done/delivered — нет, заказ уже у него.
- **Оплата:** `payment_method` — `cash` (при получении) или `transfer` (перевод на карту кафе, доступен, если в
  настройках есть номер карты); `payment_status` — `pending` → (`claimed`, гость нажал «Я перевёл») → `paid`.
  Наличные становятся `paid` при выдаче/доставке, перевод — только кнопкой «Оплата получена».
- **Роли = группы Telegram.** Персонал — группа, где админ написал `/staff`; курьеры — группа с `/couriers`
  (`cafes.courier_chat_id`). Добавить/убрать человека — добавить/удалить из группы. При статусе `courier` в группу
  курьеров уходит `courier_card` («Беру» → `orders.courier_id/courier_name` → «Доставлен»); без группы курьеров
  «Доставлен» жмёт персонал. Карточки курьеров хранятся в той же `staff_messages`, `refresh_staff` рисует нужную.
  Заказы в `ready`/`courier` дольше `runner.AUTO_CLOSE_HOURS` (6 ч от создания) закрываются сами (`close_stale`).
- **Финансы** — группа с `/finance` (`cafes.finance_chat_id`): строка-чек на каждый закрытый/отменённый заказ
  (`receipt`: кто и как оплатил, какой курьер получил наличные), карточка перевода (`finance_card`) с «Оплата
  получена» и скриншотом от гостя (фото в личке → `claimed_transfer` → copy_message). С группой финансов персонал
  переводы не подтверждает (`checks_money`). Кухня заказ не ждёт: деньги проверяются параллельно.
- **Рабочий день** (`Cafe.day_began/day_ends`): от открытия до закрытия (ночное кафе — один день), круглосуточное —
  с `cafes.day_start` (час, по умолчанию 5). `/report` [yesterday|Nh] — в группе финансов или админу в личке;
  в конце дня `daily_report` шлёт итог один раз (`cafes.reported_until`). Ручных смен /open /end нет: их забывают.
- **Сдача:** `orders.change_from` — купюра гостя при наличных (Mini App: без сдачи / 50 / 100 / 200 тыс. / своя
  сумма), видна персоналу, курьеру и гостю; меньше суммы заказа — ошибка `change`.
- **Подписка:** `cafes.paid_until` (включительно) и `cafes.enabled` задаёт оператор через CLI; `Cafe.active()`.
  Неактивное кафе не принимает заказы (бот и Mini App), владелец видит баннер в админке.
- **Профиль гостя** — таблица `customers` (cafe_id, user_id): телефон, последний адрес доставки, язык. Пишется при
  заказе и выборе языка.
- **Вход в админку:** `/admin` в боте → одноразовая ссылка `PUBLIC_URL/login/<token>` (15 мин) → cookie-сессия
  на 30 дней. Админы кафе — таблица `admins`, пополняется через инвайт `t.me/<bot>?start=join-<code>`.
  CSRF закрыт `SameSite=Lax`, поэтому изменения — только POST-формы.
- **Фото** (позиции, логотип, обложка) — `media.save_photo`: JPEG ≤1280px + `_s.jpg` ≤480px, прозрачность → белый.
- **Миграции:** `SCHEMA` — всегда актуальная схема (новая база создаётся из неё). Любое изменение существующей
  базы — новый шаг в конце `MIGRATIONS` (пишем `pragma user_version`), старые шаги не править.
  `test_migration_from_the_first_release` проверяет, что миграции приводят к той же схеме, что `SCHEMA`.
- Не коммитить `.env`, `data/` (база, фото, бэкапы), токены.
- Тесты: бот — `dp.feed_update` с `FakeSession`, заказы создаются через `tests.conftest.order()`; веб и Mini App —
  `httpx.AsyncClient(transport=ASGITransport(app))` в том же event loop, подпись — `init_data()`.

## callback_data (лимит Telegram — 64 байта)

| data | где | что |
|---|---|---|
| `orders` | приветствие | «Мои заказы» (последние 5) |
| `lang:ru` / `lang:uz` | приветствие | язык гостя |
| `oc:<order>` / `oc:<order>:list` | сообщение о заказе / «Мои заказы» | гость отменяет свой новый заказ |
| `paid:<order>` | сообщение о заказе | гость сообщил о переводе |
| `st:<order>:<status>` | карточка персонала | сменить статус (только персонал) |
| `pay:<order>` | карточка персонала | перевод получен (только персонал) |
| `take:<order>` | карточка в группе курьеров | курьер берёт доставку (первый успел) |
| `dlv:<order>` | карточка в группе курьеров | доставлено (только взявший курьер) |

Кнопки «Открыть меню» и «🔁 Повторить» — web_app (`/app/<slug>`, `/app/<slug>?repeat=<order>`), номер карты —
`copy_text`. Цвет кнопок — `style` (Bot API 9.4: success/danger/primary).

## Бэклог (что дальше и почему)

Порядок: пилоты в 3–5 кафе → по их обратной связи остальное.


1. **Пилоты и метрика.** Заказы через бота в неделю на кафе; меньше 20 — проблема продукта или продвижения.
   Учитывать переходы по QR (`/start qr`) — видно, работает ли постер.
2. **Напоминания о подписке оператору и владельцу** (за 3 дня, в Telegram). Сейчас «оплачено до» видно только в
   админке и `list`; без напоминаний кафе отключаются внезапно, а оператор теряет деньги.
3. **Click / Payme** в Mini App с автоматическим `paid` по callback. Позже, чем перевод на карту: многие кафе
   работают неофициально, а эквайринг требует ИП/юрлицо и договор. Каждое кафе — свой мерчант, ключи в настройках.
4. **Панель оператора `/ops`** (вход по Telegram id из `.env`): кафе, статус бота, отклонённые токены, подписка,
   бэкапы, последние ошибки, уведомление оператору при падении бота. Пока хватает CLI.
5. **Админка владельца v2:** заказы без перезагрузки (SSE) и звук нового заказа, дашборд (выручка, топ позиций).
   Сейчас обновление раз в 30 с и основной канал персонала — Telegram.
6. **Меню на двух языках** (название/описание позиции на узбекском). Сейчас переведён интерфейс, а меню — на том
   языке, на котором его ввёл владелец.
7. **Иконки на кнопках** (`icon_custom_emoji_id`, свой пак из Remix Icon): работают, только если у владельца бота
   Telegram Premium — создавать ботов кафе с аккаунта оператора с Premium.
8. **Бэкапы вне сервера** встроенно (S3-совместимое хранилище) вместо rclone по cron.
9. **Проверка узбекских текстов носителем языка** (`i18n.py`) до первого узбекоязычного кафе.
10. **Защита от спама заказами** (лимит новых заказов на гостя). Сейчас заказ может оформить любой, кто открыл
    бота; персонал отменит лишнее, но при первой атаке понадобится лимит.
