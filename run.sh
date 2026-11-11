#!/bin/sh
# ./run.sh          — start limybot: every cafe bot, the admins, the operator panel (the link is printed)
# ./run.sh ops      — a new login link to the operator panel
# ./run.sh --help   — all commands
set -e
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
unset VIRTUAL_ENV  # a venv activated for another folder only makes uv print warnings
uv sync --frozen --quiet
exec uv run python -m cafe "$@"
