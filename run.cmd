@echo off
rem run.cmd          - start limybot: every cafe bot, the admins, the operator panel (the link is printed)
rem run.cmd ops      - a new login link to the operator panel
cd /d "%~dp0"
if not exist .env copy .env.example .env >nul
set VIRTUAL_ENV=
uv sync --frozen --quiet || exit /b 1
uv run python -m cafe %*
