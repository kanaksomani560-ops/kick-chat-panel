@echo off
title Share Kick Panel With Friend (Internet Tunnel)
cd /d "%~dp0"
echo ========================================================
echo   CREATING SECURE INTERNET LINK FOR YOUR FRIEND...
echo ========================================================
echo.
echo Make sure your server is running (START_SERVER.bat).
echo Generating public HTTPS link...
echo.
cloudflared.exe tunnel --url http://127.0.0.1:8080
pause
