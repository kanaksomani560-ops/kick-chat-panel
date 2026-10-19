@echo off
title Push Kick Bot to GitHub
cd /d "%~dp0"
echo ========================================================
echo   PUSHING TO https://github.com/kanaksomani560-ops/kick-chat-panel
echo ========================================================
echo.
git remote remove origin >nul 2>&1
git remote add origin https://github.com/kanaksomani560-ops/kick-chat-panel.git
git branch -M main
echo Connecting to GitHub and pushing files...
git push -u origin main
echo.
echo ========================================================
echo   SUCCESS!
echo   Go to railway.com to deploy your repo now!
echo ========================================================
pause
