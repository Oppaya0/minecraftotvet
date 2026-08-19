@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo === Сборка .exe через PyInstaller ===
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo Не найден python в PATH. Установи Python 3.11 и повтори.
    pause
    exit /b 1
)

echo [1/3] Устанавливаем pyinstaller...
python -m pip install --upgrade --quiet pyinstaller
if errorlevel 1 (
    echo Не удалось установить pyinstaller.
    pause
    exit /b 1
)

echo [2/3] Собираем main.py в один .exe...
python -m PyInstaller ^
    --noconfirm ^
    --onefile ^
    --console ^
    --name minecraftotvet ^
    --uac-admin ^
    main.py
if errorlevel 1 (
    echo Ошибка сборки.
    pause
    exit /b 1
)

echo [3/3] Копируем responses.json рядом с .exe...
copy /Y responses.json dist\responses.json >nul

echo.
echo === Готово ===
echo Результат: %~dp0dist\minecraftotvet.exe
echo Раздавай папку "dist" целиком (там exe + responses.json).
echo Пользователь просто запускает minecraftotvet.exe — Python ему не нужен.
echo.
pause
