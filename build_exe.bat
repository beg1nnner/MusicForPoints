@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Пересборка удаляет dist\MusicForPoints, поэтому сохраняем настройки, вход в Twitch и чёрный список
set "OUT=dist\MusicForPoints"
set "KEEP=.keep_settings"
if exist "%KEEP%" rmdir /s /q "%KEEP%"
mkdir "%KEEP%" 2>nul
for %%F in (settings.json tokens.json blocklist.json .env deno.exe) do if exist "%OUT%\%%F" copy /y "%OUT%\%%F" "%KEEP%\" >nul

if not exist .venv python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt pyinstaller || goto :fail

.venv\Scripts\pyinstaller --noconfirm --clean --name MusicForPoints --onedir ^
  --add-data "overlay;overlay" ^
  --collect-submodules yandex_music ^
  --hidden-import aiofiles ^
  --collect-all yt_dlp ^
  --collect-all yt_dlp_ejs ^
  --collect-all imageio_ffmpeg ^
  --collect-data certifi ^
  main.py || goto :fail

for %%F in (settings.json tokens.json blocklist.json .env deno.exe) do if exist "%KEEP%\%%F" copy /y "%KEEP%\%%F" "%OUT%\" >nul

echo.
echo Готово: dist\MusicForPoints\MusicForPoints.exe
echo Раздавайте всю папку dist\MusicForPoints целиком (лучше в zip).
pause
exit /b 0

:fail
if not exist "%OUT%" mkdir "%OUT%"
for %%F in (settings.json tokens.json blocklist.json .env deno.exe) do if exist "%KEEP%\%%F" copy /y "%KEEP%\%%F" "%OUT%\" >nul
echo.
echo Сборка не удалась, смотрите ошибку выше.
pause
exit /b 1
