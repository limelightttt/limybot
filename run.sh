#!/bin/sh
# ./run.sh                      — all cafe bots + web admin
# ./run.sh add demo --token ... --from clients/demo_coffee.json — connect a cafe (see README)
set -e
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
uv sync --frozen
exec uv run python -m cafe "$@"
