@echo off
cd /d "%~dp0"
echo 上課筆記網頁：http://127.0.0.1:18484/
echo 處理音檔或錄音時請不要關掉這個視窗；用完直接關掉即可。
echo.
python -u -X utf8 lecture_app.py
pause
