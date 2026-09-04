@echo off
echo ============================================================
echo   Mockup Forge - ChatGPT Automation
echo ============================================================
echo Closing any previous server on port 8010...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8010 ^| findstr LISTENING') do taskkill /F /PID %%a 2>nul
echo Starting server at http://127.0.0.1:8010 ...
start "" http://127.0.0.1:8010
python server.py
pause
