#!/bin/sh
# Install or update limybot on a Mac (or Linux) with one line in Terminal:
#   curl -fsSL https://raw.githubusercontent.com/limelightttt/limybot/main/install.sh | sh
# It installs what is missing (uv, cloudflared), downloads or updates the code and puts a "limybot" icon on the
# Desktop. The icon updates and starts limybot and opens the operator panel. Running it again is safe: the
# database (data/) and .env are never touched.
set -e
REPO=limelightttt/limybot
BIN="$HOME/.local/bin"
export PATH="$BIN:/opt/homebrew/bin:/usr/local/bin:$PATH"
say() { printf '▸ %s\n' "$1"; }

# where the project lives: an existing checkout, else ~/limybot
if [ -n "$LIMYBOT_DIR" ]; then DIR="$LIMYBOT_DIR"
elif [ -f "$(dirname "$0")/cafe/__main__.py" ]; then DIR="$(cd "$(dirname "$0")" && pwd)"
elif [ -d "$HOME/Projects/limybot/cafe" ]; then DIR="$HOME/Projects/limybot"
else DIR="$HOME/limybot"
fi

if ! command -v uv >/dev/null 2>&1; then
  say "Ставлю uv — он сам поставит Python"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
fi

if ! command -v cloudflared >/dev/null 2>&1; then
  say "Ставлю cloudflared — без него Telegram не откроет меню"
  mkdir -p "$BIN"
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64) f=cloudflared-darwin-arm64.tgz ;;
    Darwin-*) f=cloudflared-darwin-amd64.tgz ;;
    Linux-aarch64 | Linux-arm64) f=cloudflared-linux-arm64 ;;
    *) f=cloudflared-linux-amd64 ;;
  esac
  url="https://github.com/cloudflare/cloudflared/releases/latest/download/$f"
  case "$f" in
    *.tgz) curl -fsSL "$url" | tar -xz -C "$BIN" cloudflared ;;
    *) curl -fsSL "$url" -o "$BIN/cloudflared" ;;
  esac
  chmod +x "$BIN/cloudflared"
fi

# the code: git when it really works, else an archive
has_git() {  # on a Mac without the developer tools /usr/bin/git only pops up their installer
  if [ "$(uname -s)" = Darwin ] && ! xcode-select -p >/dev/null 2>&1; then return 1; fi
  git --version >/dev/null 2>&1
}
if [ -d "$DIR/.git" ] && has_git; then
  say "Обновляю limybot в $DIR"
  ( cd "$DIR" && { [ "$(git branch --show-current)" = main ] || git checkout -q main; } && git pull -q --ff-only ) \
    || say "Обновить не вышло (нет интернета или правки в файлах) — запускаю то, что есть"
elif has_git && [ -z "$(ls -A "$DIR" 2>/dev/null)" ]; then
  say "Скачиваю limybot в $DIR"
  git clone -q "https://github.com/$REPO" "$DIR" || true
else
  say "Скачиваю limybot в $DIR"
  tgz="$(mktemp)"
  curl -fsSL "https://github.com/$REPO/archive/refs/heads/main.tar.gz" -o "$tgz" \
    && mkdir -p "$DIR" && tar -xzf "$tgz" -C "$DIR" --strip-components 1 \
    || say "Скачать не вышло — проверю, что уже есть"
  rm -f "$tgz"
fi
if [ ! -f "$DIR/run.sh" ]; then
  echo "Не получилось скачать limybot в $DIR. Проверьте интернет (если включён VPN — выключите)" >&2
  echo "и вставьте строку установки ещё раз." >&2
  exit 1
fi
chmod +x "$DIR/run.sh" "$DIR/install.sh" 2>/dev/null || true

if [ -d "$HOME/Desktop" ]; then
  icon="$HOME/Desktop/limybot.command"
  cat >"$icon" <<EOF
#!/bin/sh
# limybot: update, start all the bots and open the operator panel. Keep this window open; Ctrl+C stops.
"$DIR/install.sh" --quiet && exec "$DIR/run.sh" run --open
EOF
  chmod +x "$icon"
fi

[ "$1" = --quiet ] && exit 0
cat <<EOF

Готово. limybot лежит в $DIR
Запуск — два клика по значку «limybot» на рабочем столе (или: $DIR/run.sh run --open).
Откроется окно Терминала и сразу панель в браузере. Окно не закрывайте, пока боты нужны.
EOF
