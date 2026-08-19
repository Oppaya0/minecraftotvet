@echo off
set "FILE=%~1"
if "%FILE%"=="" (
    echo Perетащите .txt файл сюда или введите путь:
    set /p "FILE="
)
if "%FILE%"=="" (
    echo Файл не указан.
    pause
    exit /b 1
)
if not exist "%FILE%" (
    echo Файл не найден: %FILE%
    pause
    exit /b 1
)
powershell -NoProfile -Command ^
  "$f='%FILE%';" ^
  "$all = Get-Content $f -Encoding UTF8;" ^
  "$clean = $all | Where-Object { $_.Trim() -ne '' } | Sort-Object -Unique;" ^
  "$dupes = $all.Count - $clean.Count;" ^
  "$clean | Set-Content $f -Encoding UTF8;" ^
  "Write-Host \"Готово: $f\";" ^
  "Write-Host \"Строк: $($clean.Count), удалено дублей: $dupes\""
pause
