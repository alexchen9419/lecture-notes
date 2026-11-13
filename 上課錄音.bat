@echo off
cd /d "%~dp0"
set /p NAME=課名（可留空）：
set /p KW=這堂課的關鍵字／專有名詞，用逗號分隔（可留空）：
echo.
python -u -X utf8 live_lecture.py "%NAME%" --prompt "%KW%"
echo.
pause
