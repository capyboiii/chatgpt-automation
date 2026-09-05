@echo off
title Mockup Forge - ChatGPT Automation
cd /d "%~dp0"

echo ============================================================
echo   Mockup Forge - ChatGPT Automation
echo ============================================================
echo.

echo Dong server cu tren port 8010 neu co...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8010 ^| findstr LISTENING') do taskkill /F /PID %%a 2>nul

echo Dang mo giao dien web tai http://127.0.0.1:8010 ...
start "" http://127.0.0.1:8010

python server.py
if errorlevel 1 (
    echo.
    echo [ERROR] Server bi loi hoac chua chay setup.bat!
    echo Neu may moi, vui long chay setup.bat truoc.
    echo.
)
pause
