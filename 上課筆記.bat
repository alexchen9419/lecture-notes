@echo off
cd /d "%~dp0"
rem 安裝過 (有 runtime\python) 就用自帶的 Python；沒安裝但這台有 Python 和 GenieX，就直接用 (開發用的電腦) 
set "PY=runtime\python\python.exe"
if exist "%PY%" goto check
set "PY=python"
where python >nul 2>nul || goto notinstalled
where geniex >nul 2>nul || goto notinstalled
goto run
:check
if not exist "config.json" goto notinstalled
:run
echo 上課筆記網頁：http://127.0.0.1:18484/
echo 處理音檔或錄音時請不要關掉這個視窗；用完直接關掉即可。
echo.
"%PY%" -u -X utf8 lecture_app.py
pause
exit /b
:notinstalled
echo 還沒安裝，請先點兩下「安裝.bat」。
pause
