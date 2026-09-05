@echo off
title Mockup Forge - Setup
cd /d "%~dp0"

echo ============================================================
echo   Mockup Forge - ChatGPT Automation Setup
echo ============================================================
echo.

:: 1. Kiem tra Python
python --version >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Khong tim thay Python tren may tinh!
    echo Vui long cai dat Python 3.10 tro len tu https://www.python.org/
    echo LUU Y: Nho tich chon Add Python to PATH khi cai dat.
    echo.
    pause
    exit /b 1
)

echo [1/3] Kiem tra Python thanh cong:
python --version
echo.

:: 2. Tao thu muc can thiet
echo [2/3] Tao thu muc du lieu...
if not exist "data" mkdir "data"
if not exist "data\templates" mkdir "data\templates"
if not exist "designs" mkdir "designs"
if not exist ".chrome-profiles" mkdir ".chrome-profiles"
if not exist "data\state.json" echo {"prompts": []} > "data\state.json"
echo [OK] Thu muc da san sang.
echo.

:: 3. Cai dat thu vien tu requirements.txt va playwright browser
echo [3/3] Dang cai dat cac thu vien Python...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Cai dat requirements.txt that bai!
    pause
    exit /b 1
)

echo.
echo Dang cai dat trinh duyet Playwright...
python -m playwright install chrome
python -m playwright install chromium

echo.
echo ============================================================
echo   CAI DAT HOAN TAT THANH CONG!
echo ============================================================
echo.
echo Ban co the chay file start_ui.bat de bat giao dien web.
echo.
pause
