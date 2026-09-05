@echo off
title Mockup Forge - Setup
cd /d "%~dp0"

echo ============================================================
echo   Mockup Forge - ChatGPT Automation Setup
echo ============================================================
echo.

:: ------------------------------------------------------------
:: 1. Kiem tra va Tu dong cai dat Python (neu chua co)
:: ------------------------------------------------------------
echo [1/4] Kiem tra Python...
python --version >nul 2>nul
if errorlevel 1 (
    echo [INFO] Chua tim thay Python. Dang tu dong tai va cai dat Python 3.12...
    echo Dang tai bo cai Python tu python.org...
    curl.exe -L -o "%TEMP%\python_setup.exe" "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"
    if errorlevel 1 (
        echo [ERROR] Khong the tai Python tu dong. Vui long kiem tra ket noi mang.
        pause
        exit /b 1
    )
    
    echo Dang cai dat Python ngam va tu dong tich Add Python to PATH...
    start /wait "" "%TEMP%\python_setup.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_pip=1
    del "%TEMP%\python_setup.exe" 2>nul
    
    :: Nap PATH vao session CMD hien tai
    set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
    
    python --version >nul 2>nul
    if errorlevel 1 (
        echo [THONG BAO] Da cai dat xong Python.
        echo Vui long dong cua so nay va chay lai setup.bat 1 lan nua de he thong nhan dien day du!
        pause
        exit /b 0
    )
)

echo [OK] Python da san sang:
python --version
echo.

:: ------------------------------------------------------------
:: 2. Kiem tra va Tu dong cai dat Google Chrome (neu chua co)
:: ------------------------------------------------------------
echo [2/4] Kiem tra Google Chrome...
set "CHROME_FOUND=0"
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME_FOUND=1"
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME_FOUND=1"
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "CHROME_FOUND=1"
reg query "HKLM\Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" >nul 2>nul && set "CHROME_FOUND=1"
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" >nul 2>nul && set "CHROME_FOUND=1"

if "%CHROME_FOUND%"=="0" (
    echo [INFO] Chua tim thay Google Chrome. Dang tu dong tai va cai dat...
    curl.exe -L -o "%TEMP%\chrome_installer.exe" "https://dl.google.com/chrome/install/latest/chrome_installer.exe"
    if not errorlevel 1 (
        echo Dang cai dat Google Chrome ngam...
        start /wait "" "%TEMP%\chrome_installer.exe" /silent /install
        del "%TEMP%\chrome_installer.exe" 2>nul
        echo [OK] Cai dat Google Chrome thanh cong.
    ) else (
        echo [CANH BAO] Khong tai duoc Chrome tu dong.
    )
) else (
    echo [OK] Google Chrome da san sang tren may.
)
echo.

:: ------------------------------------------------------------
:: 3. Tao thu muc can thiet
:: ------------------------------------------------------------
echo [3/4] Tao thu muc du lieu...
if not exist "data" mkdir "data"
if not exist "data\templates" mkdir "data\templates"
if not exist "designs" mkdir "designs"
if not exist ".chrome-profiles" mkdir ".chrome-profiles"
if not exist "data\state.json" echo {"prompts": []} > "data\state.json"
echo [OK] Thu muc da san sang.
echo.

:: ------------------------------------------------------------
:: 4. Cai dat thu vien Python va Playwright
:: ------------------------------------------------------------
echo [4/4] Dang cai dat cac thu vien Python...
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
echo Ban co the chay file start_ui.bat de bat dau su dung.
echo.
pause
