@echo off
title Install Requirements for Kick Bot
cd /d "%~dp0"
echo ========================================================
echo   INSTALLING REQUIRED PACKAGES FOR OLD LAPTOP...
echo ========================================================
echo.
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo.
echo ========================================================
echo   INSTALLATION COMPLETE!
echo   You can now run START_SERVER.bat
echo ========================================================
pause
