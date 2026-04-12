@echo off
cd /d %~dp0
echo Auto-Seat Launcher
echo ==========================================
echo Step 1/2: Installing dependencies via Tsinghua mirror...
echo.

python -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple --quiet
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

if %errorlevel% neq 0 (
    echo [ERROR] Dependency installation failed.
    pause
    exit /b %errorlevel%
)

echo.
echo Step 2/2: Launching app.pyw ...
start "" pythonw app.pyw

echo Launch command sent. Closing in 3 seconds.
timeout /t 3 >nul
exit
