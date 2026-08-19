@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

if "%~1"=="" (
    echo Использование: sort_quiz.bat файл.txt
    echo Сортирует строки по алфавиту и удаляет дубли.
    pause
    exit /b 1
)

if not exist "%~1" (
    echo Файл не найден: %~1
    pause
    exit /b 1
)

set "INPUT=%~1"
set "TEMP_SORTED=%~dpn1_sorted.tmp"
set "TEMP_DEDUP=%~dpn1_dedup.tmp"

:: Сортировка
sort "%INPUT%" /o "%TEMP_SORTED%"

:: Удаление дублей (после сортировки дубли идут подряд)
set "PREV="
set "TOTAL=0"
set "DUPES=0"
(
    for /f "usebackq delims=" %%L in ("%TEMP_SORTED%") do (
        set "LINE=%%L"
        if "!LINE!" neq "!PREV!" (
            echo(!LINE!
            set /a TOTAL+=1
        ) else (
            set /a DUPES+=1
        )
        set "PREV=!LINE!"
    )
) > "%TEMP_DEDUP%"

:: Замена оригинала
move /y "%TEMP_DEDUP%" "%INPUT%" >nul
del "%TEMP_SORTED%" 2>nul

echo Готово: %INPUT%
echo Строк: !TOTAL!, удалено дублей: !DUPES!
pause
