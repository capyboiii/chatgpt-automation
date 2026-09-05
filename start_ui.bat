@echo off
chcp 65001 >nul
title Mockup Forge - ChatGPT Automation

echo ============================================================
echo   Mockup Forge - ChatGPT Automation
echo ============================================================
echo.

:: 1. Tự động kiểm tra môi trường, nếu chưa cài đặt thì gọi setup.bat
python -c "import fastapi, uvicorn, playwright, PIL, yaml, pyotp" >nul 2>nul
if %errorlevel% neq 0 (
    echo [THÔNG BÁO] Phát hiện chưa cài đặt đủ thư viện cần thiết!
    echo Đang tự động chuyển sang setup.bat để cài đặt...
    echo.
    call setup.bat
    if %errorlevel% neq 0 (
        echo [ERROR] Cài đặt không thành công.
        pause
        exit /b 1
    )
)

:: 2. Đóng server cũ nếu còn treo port 8010
echo Đang kiểm tra và giải phóng cổng 8010...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8010 ^| findstr LISTENING') do taskkill /F /PID %%a 2>nul

:: 3. Mở trình duyệt sau khi server khởi động (đợi 2 giây để server sẵn sàng)
echo Đang khởi động server tại http://127.0.0.1:8010 ...
start /b cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:8010"

:: 4. Chạy server chính
python server.py

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Server dừng với mã lỗi %errorlevel%.
)
pause
