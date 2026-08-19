@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Объединение викторин

if "%~1"=="" (
    echo Перетащи чужой JSON-файл на этот батник.
    echo Или запусти так:  merge_quiz.bat "путь\к\чужому.json"
    pause
    exit /b 1
)

where python >nul 2>&1
if errorlevel 1 (
    echo Не найден python в PATH. Установи Python 3.11 и повтори.
    pause
    exit /b 1
)

python merge_quiz.py "%~1"
echo.
pause
