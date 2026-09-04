@echo off
chcp 65001 >nul
title Setup - Mockup Forge ChatGPT Automation

echo ============================================================
echo   Mockup Forge - ChatGPT Automation Setup
echo ============================================================
echo.

:: 1. Kiểm tra Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [LỖI] Không tìm thấy Python trên máy tính của bạn!
    echo Vui lòng tải và cài đặt Python từ https://www.python.org/
    echo Lưu ý tích chọn "Add Python to PATH" khi cài đặt.
    echo.
    pause
    exit /b 1
)

echo [1/3] Kiểm tra phiên bản Python...
python --version
echo.

:: 2. Cài đặt các thư viện từ requirements.txt
echo [2/3] Đang cài đặt các thư viện cần thiết từ requirements.txt...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo [LỖI] Quá trình cài đặt thư viện thất bại. Vui lòng kiểm tra lại kết nối mạng hoặc môi trường Python.
    pause
    exit /b 1
)
echo ✓ Cài đặt thư viện Python thành công!
echo.

:: 3. Cài đặt trình duyệt cho Playwright
echo [3/3] Đang tải và cấu hình trình duyệt Playwright (Chrome)...
python -m playwright install chrome
if %errorlevel% neq 0 (
    echo [CẢNH BÁO] Không thể cài kênh chrome của Playwright, đang thử tải chromium mặc định...
    python -m playwright install chromium
)
echo ✓ Cài đặt trình duyệt hoàn tất!
echo.

echo ============================================================
echo   CÀI ĐẶT HOÀN TẤT THÀNH CÔNG!
echo ============================================================
echo.
echo Bước tiếp theo:
echo 1. Đăng nhập tài khoản ChatGPT cho profile (bắt buộc trước khi gen):
echo    Chạy lệnh:  python login.py acc1
echo.
echo 2. Bật giao diện làm việc:
echo    Chạy file:  start_ui.bat
echo ============================================================
echo.
pause
