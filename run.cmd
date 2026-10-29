@echo off
cd /d "%~dp0"
if not exist .env copy .env.example .env >nul && echo Created .env - put BOT_TOKEN from @BotFather there, then run this again. && notepad .env && exit /b 1
uv sync --frozen || exit /b 1
uv run python -m cafe
