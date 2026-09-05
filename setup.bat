@echo off
chcp 65001 >nul
title Cài Đặt Hệ Thống - Mockup Forge ChatGPT Automation

echo ============================================================
echo   Mockup Forge - ChatGPT Automation Setup
echo ============================================================
echo.

:: 1. Kiểm tra Python
python --version >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Không tìm thấy Python trên máy tính!
    echo Vui lòng cài đặt Python (phiên bản 3.10 trở lên) từ https://www.python.org/
    echo LƯU Ý: Nhớ tích chọn "Add Python to PATH" khi cài đặt.
    echo.
    pause
    exit /b 1
)

echo [1/4] Kiểm tra phiên bản Python:
python --version
echo.

:: 2. Khởi tạo thư mục và file cấu hình cần thiết
echo [2/4] Đang khởi tạo các thư mục và file hệ thống...
if not exist "data" mkdir "data"
if not exist "data\templates" mkdir "data\templates"
if not exist "designs" mkdir "designs"
if not exist ".chrome-profiles" mkdir ".chrome-profiles"

if not exist "data\state.json" (
    echo {"prompts": []} > "data\state.json"
)

if not exist "config.yaml" (
    echo Tạo file cấu hình config.yaml mặc định...
    (
        echo browser:
        echo   headless: false
        echo   profiles_dir: ./.chrome-profiles
        echo   profiles:
        echo   - name: acc1
        echo     tabs: 1
        echo     email: ""
        echo   tabs_per_account: 1
        echo   generation_timeout: 300
        echo   nav_timeout: 90
        echo run:
        echo   max_retries: 3
        echo   batch_size: 6
        echo   settle_seconds: 8
        echo   stall_timeout: 120
        echo   idle_exit_seconds: 2
        echo   quiet_limit_seconds: 10
        echo   thinking: vừa
    ) > "config.yaml"
)
echo [OK] Đã sẵn sàng các thư mục lưu trữ và cấu hình.
echo.

:: 3. Cài đặt thư viện Python từ requirements.txt
echo [3/4] Đang cài đặt thư viện từ requirements.txt...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Cài đặt thư viện Python thất bại! Vui lòng kiểm tra kết nối mạng.
    pause
    exit /b 1
)
echo [OK] Cài đặt thư viện Python thành công!
echo.

:: 4. Cài đặt trình duyệt Playwright
echo [4/4] Đang cài đặt trình duyệt Playwright (Chrome/Chromium)...
python -m playwright install chrome
if %errorlevel% neq 0 (
    echo [WARNING] Không thể cài kênh chrome, đang thử cài chromium mặc định...
    python -m playwright install chromium
)
echo [OK] Cài đặt trình duyệt hoàn tất!
echo.

:: Kiểm tra tổng thể môi trường
echo Đang kiểm tra tính toàn vẹn hệ thống...
python -c "import fastapi, uvicorn, playwright, PIL, yaml, pyotp; print('[OK] Mọi thư viện cốt lõi đã sẵn sàng!')"
if %errorlevel% neq 0 (
    echo [ERROR] Kiểm tra thư viện thất bại.
    pause
    exit /b 1
)

echo ============================================================
echo   CÀI ĐẶT HOÀN TẤT THÀNH CÔNG!
echo ============================================================
echo.
echo Bạn có thể đăng nhập ChatGPT bằng lệnh: python login.py acc1
echo.
set /p START_NOW="Bạn có muốn khởi động Giao diện Web ngay bây giờ không? (Y/N, mặc định Y): "
if /i "%START_NOW%"=="" set START_NOW=Y
if /i "%START_NOW%"=="Y" (
    echo Đang khởi động Giao diện...
    start start_ui.bat
    exit /b 0
)

pause
