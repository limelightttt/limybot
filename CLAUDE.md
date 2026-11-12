# limybot — контекст для Claude

White-label Telegram-бот заказов для кафе Ташкента, продаётся кафе по подписке (SaaS): одна установка на сервере
оператора, много кафе. У каждого кафе свой бот (токен), меню, оформление и настройки в SQLite. Гости заказывают в
Telegram Mini App, владелец управляет всем в веб-админке. Пользователь общается по-русски; тексты бота и админки —
на русском (гостю — ещё и на узбекском), код и комментарии — на английском.

## Структура

```
cafe/
  __main__.py   CLI: run (по умолчанию) | ops | add | remove | token | invite | paid | disable | enable | backup | list
  runner.py     один процесс: Dispatcher на каждое кафе + uvicorn; сверяет кафе с БД каждые 15 с и сразу после правок
                в панели оператора (sync под lock); состояние ботов для панели; сводка в терминале при старте
                (ссылка в /ops); проверка занятого порта; кнопка «Меню» и команды бота; раз в день бэкап
  tunnel.py     пустой PUBLIC_URL (или старый *.trycloudflare.com) → сам запускает cloudflared quick tunnel
  tokens.py     bot_profile(token) → (username, имя) или TokenError с русским текстом — для CLI, панели, раннера
  bot.py        build(store, cafe_id, settings) -> Dispatcher: приветствие, «Мои заказы», отмена, «Я перевёл»,
                кнопки персонала, /admin /staff /stats, вход/выход бота из групп, миграция группы. Общие для бота, админки и Mini App: staff_card,
                send_to_staff, refresh_staff, send_to_customer, change_status, mark_paid
  store.py      весь SQL (sqlite3); SCHEMA — текущая схема, MIGRATIONS — шаги для старых баз; методы скоупятся по cafe_id
  models.py     dataclass'ы (Cafe, Item, CartLine, Order, Customer...), STATUSES/FLOW, PAYMENT_*
  i18n.py       тексты гостю ru/uz: BOT_TEXTS (бот), APP_TEXTS (Mini App), STATUS, order_text
  media.py      фото: JPEG ≤1280px + превью ≤480px (thumb), удаление, превью для старых фото
  backup.py     data/backups/limybot-<дата>.zip (база + фото), хранит KEEP=14
  delivery.py   доставка по карте: km() (по прямой), fee(cafe, point) — цена по зонам или одна цена, map_link (Яндекс),
                parse_zones (форма админки)
  reports.py    отчёты владельца: build() — выручка без отменённых, товары/доставка, по способу оплаты, оплачено и
                неподтверждённые переводы, топ позиций по снимкам заказов, по дням (в часовом поясе кафе);
                csv_export() — для Excel (";", UTF-8 с BOM); daily_summary() — утренние итоги вчера
  billing.py    подписка: цена, пробный период, отсрочка (GRACE_DAYS), extend() — продление от старой даты,
                state() — ok/soon/grace/off/paused/unlimited; Payment
  notify.py     напоминания владельцам в их бот (за 3 дня, за 1, отсрочка, отключено, «оплата получена»),
                служебный бот оператора (build_ops: /start op-<код>, /panel, /status), утренняя сводка и тревоги;
                Notifier.tick() раннер зовёт каждые 15 с, каждое сообщение — один раз (таблица reminders)
  template.py   pydantic-валидация clients/*.json, импорт в БД (import_cafe), список шаблонов, zip-пакеты,
                connect_cafe — подключение заведения (общее для CLI и панели)
  config.py     Settings из .env: DB_PATH, PUBLIC_URL, WEB_HOST, WEB_PORT, SUPPORT
  web/          FastAPI, модуль на фичу, каждый отдаёт router(web: Web):
    common.py     Web (store, settings, bots: Bots, templates, admin-зависимость, page), back(); Bots — протокол
                  раннера (bot, state, check, sync), в тестах FakeBots
    orders.py menu.py cafe_settings.py promo.py reports.py — админка владельца (templates/admin/*.html, static/style.css)
    ops.py        панель оператора /ops (templates/ops/*.html): заведения и их статус, подключение, приглашение,
                  подписка, пауза, смена токена, удаление, вход в админку заведения
    miniapp.py    витрина гостя /app/<slug> (templates/app/index.html, static/app.js, static/app.css)
clients/        стартовые шаблоны кафе (demo_coffee.json); clients/private/<бренд>/ — пакеты с фото, не в git
tests/          conftest.py (FakeSession, FakeBots, фикстуры, order(), init_data()), test_store, test_flow,
                test_miniapp, test_web, test_ops, test_runner, schema_v0.sql (схема до миграций)
Dockerfile, compose.yaml, Caddyfile — деплой: docker compose up -d, Caddy сам выпускает HTTPS для DOMAIN
server.sh       установка/обновление на Ubuntu одной строкой (curl | sh): Docker, /opt/limybot, домен в .env,
                проверка A-записи, compose up, ссылка в /ops
```

## Команды

```bash
uv sync                      # зависимости
uv run pytest                # все тесты (без сети)
uv run ruff check . && uv run ruff format .
uv run python -m cafe        # боты + админка + Mini App на :8000
uv run python -m cafe add demo --token <T> --from clients/demo_coffee.json
```
Mac/Linux: `install.sh` (curl | sh) ставит uv и cloudflared, качает/обновляет код и кладёт на рабочий стол
`limybot.command` (обновить + `./run.sh run --open`). Windows: `run.cmd [аргументы]`, Linux: `./run.sh [аргументы]`. Сервер: `docker compose up -d`, CLI —
`docker compose exec app python -m cafe ...`.

## Соглашения и важные решения

- **Продукт не должен путать оператора.** Оператор — не программист: всё ежедневное делается в панели `/ops`, CLI —
  запасной путь. Бот никогда не молчит на команду (объясняет, почему не сделал), ошибки — по-русски с тем, что
  делать, без трейсбеков. Никаких инструкций «удалите data» — для этого есть удаление заведения.

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
- **Доставка по карте:** у кафе точка (`lat`/`lon`) и зоны `delivery_zones` [(до км, цена)], всегда по
  возрастанию. Есть и точка, и зоны → гость обязан поставить булавку (ошибки `point`/`far`), цену считает сервер
  (`delivery.fee`, та же логика в `app.js quote()`); иначе — `delivery_fee` везде, булавка необязательна. Карта —
  MapLibre 4.7.1 из `static/vendor` (BSD, свой файл, не CDN) + тайлы OpenFreeMap (бесплатно, без ключей; бесплатный
  Яндекс запрещает перепродажу). Булавка в центре, двигается карта; «Я здесь» — `Telegram.WebApp.LocationManager`
  (8.0+), иначе `navigator.geolocation`. Карта создаётся один раз и переносится между перерисовками корзины
  (`mapElement()`), при движении меняются только цена и кнопка (`updateQuote()`). Сотрудникам — «📍 N км от вас»,
  кнопка «Открыть на карте» (Яндекс) и `send_location` ответом на карточку.
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
- **Подписка:** `cafes.paid_until` — последний оплаченный день (None — без срока: демо/своё), `cafes.enabled` —
  пауза оператора. Новое заведение — 14 дней пробного периода (`connect_cafe(..., paid_until)`). После paid_until
  ещё `GRACE_DAYS`=2 дня заказы принимаются, потом `Cafe.active()` = False: бот и Mini App не принимают заказы,
  ничего не удаляется. Оплата — кнопкой в /ops (`store.add_payment`): продлевает от старой даты, если оплатили вовремя
  или в отсрочку, иначе от сегодня; таблица `payments` хранит историю и `prev_until` для отмены последней оплаты.
  Цена и контакт для оплаты — в /ops «Настройки» (таблица `meta`), владелец видит их в напоминаниях и баннере.
- **Служебный бот оператора** — токен в `meta.ops_bot_token`, раннер держит его в `running[OPS]` рядом с ботами
  кафе. Оператор подключается ссылкой `t.me/<бот>?start=op-<код>` со страницы настроек (`operators`).
- **Профиль гостя** — таблица `customers` (cafe_id, user_id): телефон, последний адрес доставки, язык. Пишется при
  заказе и выборе языка.
- **Вход в админку:** `/admin` в боте → одноразовая ссылка `PUBLIC_URL/login/<token>` (15 мин) → cookie `sid`
  на 30 дней. Админы кафе — таблица `admins`, пополняется через инвайт `t.me/<bot>?start=join-<code>`.
  CSRF закрыт `SameSite=Lax`, поэтому изменения — только POST-формы.
- **Оператор:** `store.OPERATOR = 0` (user id) и `store.OPS = 0` (cafe id сессий панели). Ссылку
  `/ops/login/<token>` печатает раннер при старте и `cafe ops`; cookie `ops` отдельно от `sid`. «Открыть админку
  заведения» создаёт `sid`-сессию (cafe, OPERATOR) — `Web.admin` пускает OPERATOR в любое кафе. `/login` не
  принимает ссылки панели, `/ops/login` — ссылки владельцев (тест `test_only_the_operator_gets_in`).
- **Группа сотрудников:** бота добавил админ кафе (my_chat_member) → группа становится `staff_chat_id`; добавил
  чужой — бот просит `/staff`; бота удалили — заказы снова админам в личку, им сообщение; группа стала
  супергруппой (`migrate_to_chat_id`) — id обновляется. `/admin` `/staff` `/stats` от не-админа и от анонимного
  админа группы — ответ с объяснением. Нажатия на кнопки, пролежавшие в очереди, пока бот был выключен
  («query is too old»), глушит `dp.errors`.
- **https локально:** `PUBLIC_URL` пустой → `tunnel.open_tunnel` (cloudflared), адрес только в памяти процесса.
  Кнопки web_app в уже отправленных сообщениях после перезапуска ведут на старый адрес — гостю `/start`.
- **Оформление витрины:** `brand_color`, `logo`, `cover`, `app_theme` (`auto` — тема Telegram гостя, `light`/`dark` —
  своя у всех; `html[data-theme]`, JS красит шапку Telegram в `--bg`). Текст на фирменном цвете — `Cafe.on_brand()`
  (чёрный или белый по контрасту WCAG) → `--on-brand`; `color: #fff` на `--brand` не писать: жёлтый бренд нечитаем.
- **Шаблон заведения** (`template.py`) может нести оформление и фото: пути относительно JSON, `load_template`
  проверяет, что файлы есть, `import_cafe(..., media_dir)` прогоняет их через `media.save_photo`.
- **Не только кофейни.** Тексты гостю нейтральны для любого общепита (бургерные, донерные): без ☕ и «без сахара».
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

Кнопки «Открыть меню» и «🔁 Повторить» — web_app (`/app/<slug>`, `/app/<slug>?repeat=<order>`), номер карты —
`copy_text`. Цвет кнопок — `style` (Bot API 9.4: success/danger/primary).

## Бэклог (что дальше и почему)

Порядок: пилоты в 3–5 кафе → по их обратной связи остальное.

1. **Пилоты и метрика.** Заказы через бота в неделю на кафе; меньше 20 — проблема продукта или продвижения.
   Учитывать переходы по QR (`/start qr`) — видно, работает ли постер.
2. ~~Напоминания о подписке~~ — сделано (notify.py). Дальше: локация и карта (MapLibre + OpenFreeMap, без ключей:
   бесплатный Яндекс запрещает перепродажу), зоны доставки по км, отчёты с Excel, сервер в Узбекистане.
3. **Click / Payme** в Mini App с автоматическим `paid` по callback. Позже, чем перевод на карту: многие кафе
   работают неофициально, а эквайринг требует ИП/юрлицо и договор. Каждое кафе — свой мерчант, ключи в настройках.
4. **Панель оператора — дальше:** уведомление оператору в Telegram, когда бот заведения упал или токен отклонён;
   последние ошибки и бэкапы в панели. Сейчас статус видно в панели и в сводке при запуске.
5. **Админка владельца v2:** заказы без перезагрузки (SSE) и звук нового заказа. Отчёты уже есть (/reports, Excel,
   утренние итоги в боте). Сейчас обновление раз в 30 с и основной канал персонала — Telegram.
6. **Меню на двух языках** (название/описание позиции на узбекском). Сейчас переведён интерфейс, а меню — на том
   языке, на котором его ввёл владелец.
7. **Иконки на кнопках** (`icon_custom_emoji_id`, свой пак из Remix Icon): работают, только если у владельца бота
   Telegram Premium — создавать ботов кафе с аккаунта оператора с Premium.
8. **Бэкапы вне сервера** встроенно (S3-совместимое хранилище) вместо rclone по cron.
9. **Проверка узбекских текстов носителем языка** (`i18n.py`) до первого узбекоязычного кафе.
10. **Защита от спама заказами** (лимит новых заказов на гостя). Сейчас заказ может оформить любой, кто открыл
    бота; персонал отменит лишнее, но при первой атаке понадобится лимит.
