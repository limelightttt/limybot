@echo off
rem run.cmd                      - all cafe bots + web admin
rem run.cmd add demo --token ... --from clients\demo_coffee.json - connect a cafe (see README)
cd /d "%~dp0"
if not exist .env copy .env.example .env >nul
uv sync --frozen || exit /b 1
uv run python -m cafe %*
