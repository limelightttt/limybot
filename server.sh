#!/bin/sh
# limybot on a server, in one line. On a fresh Ubuntu server, as root:
#   curl -fsSL https://raw.githubusercontent.com/limelightttt/limybot/main/server.sh | sh
# It installs Docker, puts limybot in /opt/limybot, asks for the domain once, starts everything with HTTPS (Caddy
# gets the certificate itself) and prints the link to the operator panel. Run the same line again to update:
# the data (/opt/limybot/data: database, photos, backups) and .env are kept.
set -e
REPO=https://github.com/limelightttt/limybot
DIR=/opt/limybot
say() { printf '\n▸ %s\n' "$1"; }

if [ "$(id -u)" != 0 ]; then
  echo "Нужны права root: войдите на сервер как root или запустите через sudo." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  say "Ставлю Docker (пара минут)"
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
command -v git >/dev/null 2>&1 || { say "Ставлю git"; apt-get update -qq && apt-get install -y -qq git >/dev/null; }

if [ -d "$DIR/.git" ]; then
  say "Обновляю limybot"
  git -C "$DIR" pull -q --ff-only
else
  say "Скачиваю limybot в $DIR"
  git clone -q "$REPO" "$DIR"
fi
cd "$DIR"
[ -f .env ] || cp .env.example .env

domain=$(sed -n 's/^DOMAIN=//p' .env)
if [ -z "$domain" ]; then
  echo
  echo "Какой домен будет у limybot? Например: admin.limybot.uz"
  echo "(в настройках домена у регистратора должна быть A-запись на IP этого сервера)"
  printf "Домен: "
  read -r domain </dev/tty
  domain=$(echo "$domain" | sed 's#^https\?://##; s#/.*##' | tr 'A-Z' 'a-z')
  [ -n "$domain" ] || { echo "Домен не указан — запустите ещё раз." >&2; exit 1; }
  sed -i "s/^DOMAIN=.*/DOMAIN=$domain/" .env
fi

# the certificate is issued only if the domain already points here
here=$(hostname -I 2>/dev/null || true)
there=$(getent ahostsv4 "$domain" | awk 'NR==1 {print $1}')
if [ -z "$there" ]; then
  say "⚠️  Домен $domain пока никуда не указывает. Добавьте у регистратора A-запись на IP сервера ($here)"
  echo "   и подождите 5–30 минут. limybot запустится сейчас, https заработает сам, когда запись дойдёт."
elif ! echo " $here " | grep -q " $there "; then
  say "⚠️  $domain указывает на $there, а у этого сервера: $here. Проверьте A-запись домена."
fi

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null
fi

say "Запускаю (первый раз — несколько минут на сборку)"
docker compose up -d --build --quiet-pull 2>&1 | grep -v "^ *$" | tail -3 || true

link=""
for _ in $(seq 1 30); do
  link=$(docker compose exec -T app python -m cafe ops 2>/dev/null | grep -o 'https://[^ ]*') && [ -n "$link" ] && break
  sleep 2
done
cat <<END

Готово. limybot работает на https://$domain — и будет работать сам, в том числе после перезагрузки сервера.

Панель оператора (ссылка одноразовая, 15 минут; потом вход запомнится в браузере):
  ${link:-не получил — подождите минуту и выполните: cd $DIR && docker compose exec app python -m cafe ops}

Дальше всё в панели: «Подключить» заведение, «Настройки» → служебный бот (команда /panel в нём даёт вход в панель
с телефона, без сервера). Обновить limybot — ещё раз эта же строка установки.
Данные: $DIR/data (каждый день бэкап в $DIR/data/backups).
END
