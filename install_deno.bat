@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Устанавливаю Deno (нужен для заказов с YouTube)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_deno.ps1"
if errorlevel 1 (
    echo.
    echo Установка не удалась, подробности в install_deno.log
) else (
    echo.
    echo Готово. Перезапустите MusicForPoints, если он открыт.
)
pause
