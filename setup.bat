@echo off
title Setup - Mockup Forge ChatGPT Automation

echo ============================================================
echo   Mockup Forge - ChatGPT Automation Setup
echo ============================================================
echo.

:: 1. Kiem tra Python
python --version >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Khong tim thay Python tren may tinh!
    echo Vui long cai dat Python tu https://www.python.org/
    echo Nho tich chon: "Add Python to PATH" khi cai dat.
    echo.
    pause
    exit /b 1
)

echo [1/3] Kiem tra phien ban Python:
python --version
echo.

:: 2. Cai dat thu vien tu requirements.txt
echo [2/3] Dang cai dat cac thu vien can thiet tu requirements.txt...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Cai dat thu vien that bai! Vui long kiem tra ket noi mang.
    pause
    exit /b 1
)
echo [OK] Cai dat thu vien Python thanh cong!
echo.

:: 3. Cai dat Playwright browser
echo [3/3] Dang cai dat trinh duyet Playwright (Chrome)...
python -m playwright install chrome
if %errorlevel% neq 0 (
    echo [WARNING] Khong the cai kenh chrome, dang thu cai chromium mac dinh...
    python -m playwright install chromium
)
echo [OK] Cai dat trinh duyet hoan tat!
echo.

echo ============================================================
echo   CAI DAT HOAN TAT THANH CONG!
echo ============================================================
echo.
echo Buoc tiep theo:
echo 1. Dang nhap ChatGPT cho profile (bat buoc truoc khi gen):
echo    Chay lenh:  python login.py acc1
echo.
echo 2. Bat giao dien web:
echo    Chay file:  start_ui.bat
echo ============================================================
echo.
pause
