-- The schema before migrations existed (user_version 0), as shipped in the first releases.
-- tests/test_store.py migrates a database built from it and checks the result equals store.SCHEMA.
create table if not exists cafes (
  id integer primary key, slug text not null unique, bot_token text not null unique, bot_username text,
  name text not null, greeting text not null default '', currency text not null default 'сум',
  timezone text not null default 'Asia/Tashkent', open_time text, close_time text,
  pickup_address text not null default '', delivery_enabled integer not null default 0,
  delivery_min integer not null default 0, delivery_fee integer not null default 0,
  payment_note text not null default 'Оплата наличными или картой при получении',
  staff_chat_id integer, invite_code text
);
create table if not exists admins (
  cafe_id integer not null references cafes on delete cascade, user_id integer not null,
  name text not null default '', primary key (cafe_id, user_id)
);
create table if not exists categories (
  id integer primary key, cafe_id integer not null references cafes on delete cascade,
  name text not null, position integer not null default 0
);
create table if not exists items (
  id integer primary key, cafe_id integer not null references cafes on delete cascade,
  category_id integer not null references categories on delete cascade, name text not null,
  description text not null default '', price integer not null, photo text, photo_file_id text,
  available integer not null default 1, position integer not null default 0
);
create table if not exists modifier_groups (
  id integer primary key, cafe_id integer not null references cafes on delete cascade,
  name text not null, multiple integer not null default 0
);
create table if not exists modifier_options (
  id integer primary key, group_id integer not null references modifier_groups on delete cascade,
  name text not null, price integer not null default 0
);
create table if not exists item_modifiers (
  item_id integer not null references items on delete cascade,
  group_id integer not null references modifier_groups on delete cascade, primary key (item_id, group_id)
);
create table if not exists carts (
  id integer primary key, cafe_id integer not null, user_id integer not null,
  item_id integer not null references items on delete cascade, options text not null, qty integer not null,
  unique (cafe_id, user_id, item_id, options)
);
create table if not exists orders (
  id integer primary key, cafe_id integer not null references cafes on delete cascade, number integer not null,
  user_id integer not null, customer text not null, phone text not null, method text not null, address text,
  comment text, lines text not null, subtotal integer not null, delivery_fee integer not null,
  total integer not null, status text not null default 'new',
  payment_method text not null default 'cash', payment_status text not null default 'pending',
  created_at text not null, unique (cafe_id, number)
);
create table if not exists staff_messages (
  order_id integer not null references orders on delete cascade, chat_id integer not null,
  message_id integer not null, primary key (order_id, chat_id)
);
create table if not exists login_links (token text primary key, cafe_id integer not null,
  user_id integer not null, expires integer not null);
create table if not exists sessions (token text primary key, cafe_id integer not null,
  user_id integer not null, expires integer not null);
