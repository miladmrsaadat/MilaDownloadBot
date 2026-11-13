@echo off
chcp 65001 >nul
title MilaDownloadBot
echo ========================================================
echo   MilaDownloadBot - Telegram Bot Starter
echo ========================================================
echo.
if not exist .env (
    echo [!] .env file not found.
    echo Please copy .env.example to .env and fill in BOT_TOKEN.
    echo You can open .env with Notepad.
    echo.
    pause
    exit /b 1
)
if not exist venv (
    echo [i] First-time setup: creating Python environment...
    python -m venv venv
    call venv\Scripts\activate.bat
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    echo.
) else (
    call venv\Scripts\activate.bat
)
echo [i] Starting bot... Keep this window open!
echo.
python bot.py
echo.
pause
