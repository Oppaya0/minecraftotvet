@echo off
if "%~1"=="" (
    echo Drag and drop a .txt file onto this bat, or run:
    echo   sort_quiz.bat questions.txt
    pause
    exit /b 1
)
if not exist "%~1" (
    echo File not found: %~1
    pause
    exit /b 1
)
powershell -NoProfile -Command ^
  "$f='%~1';" ^
  "$all = Get-Content $f -Encoding UTF8;" ^
  "$clean = $all | Where-Object { $_.Trim() -ne '' } | Sort-Object -Unique;" ^
  "$dupes = $all.Count - $clean.Count;" ^
  "$clean | Set-Content $f -Encoding UTF8;" ^
  "Write-Host \"Done: $f\";" ^
  "Write-Host \"Lines: $($clean.Count), duplicates removed: $dupes\""
pause
