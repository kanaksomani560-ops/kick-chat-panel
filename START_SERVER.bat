@echo off
title Kick VIP Bot Server
cd /d "%~dp0"
echo Starting Kick VIP Bot Server on http://0.0.0.0:8080 ...

REM Try default python in PATH first
python --version >nul 2>&1
if %ERRORLEVEL% equ 0 (
    python server.py
    goto end
)

REM Fallback to specific python path if PATH is not configured
if exist "C:\Users\kanak\AppData\Local\Python\pythoncore-3.14-64\python.exe" (
    "C:\Users\kanak\AppData\Local\Python\pythoncore-3.14-64\python.exe" server.py
    goto end
)

echo.
echo [ERROR] Python was not found!
echo Please install Python from https://www.python.org and make sure to check "Add Python to PATH".
echo.

:end
pause
