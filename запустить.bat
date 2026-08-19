@echo off
chcp 65001 >nul
cd /d "%~dp0"

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Запрашиваю права администратора...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

title Minecraft Auto-Responder
where python >nul 2>&1
if errorlevel 1 (
    echo Не найден python. Установи Python 3.11 с python.org и не забудь галку "Add python.exe to PATH".
    pause
    exit /b 1
)

python -m pip install --quiet -r requirements.txt
python main.py

echo.
echo === Бот остановлен ===
pause
