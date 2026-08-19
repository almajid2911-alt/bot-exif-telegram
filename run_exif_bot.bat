@echo off
title Telegram EXIF Photo Editor Bot
echo Menjalankan Bot EXIF & Metadata Foto (3 Fitur)...
echo.
cd /d "%~dp0"
call ..\.venv\Scripts\activate.bat 2>nul || call .venv\Scripts\activate.bat 2>nul
python exif_editor_bot.py
pause
