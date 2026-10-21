@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv python -m venv .venv
.venv\Scripts\python -m pip install -q -r requirements.txt
rem yt-dlp часто меняется из-за обновлений YouTube, поэтому обновляем его при каждом запуске (без сети просто пропускаем)
.venv\Scripts\python -m pip install -q -U "yt-dlp[default]" >nul 2>&1
.venv\Scripts\python main.py %*
pause
