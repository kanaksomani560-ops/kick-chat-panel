@echo off
title Push Kick Bot to GitHub
cd /d "%~dp0"
echo ========================================================
echo   PUSH CODE TO GITHUB (FOR RAILWAY DEPLOYMENT)
echo ========================================================
echo.
echo Make sure you created a NEW PRIVATE REPOSITORY on github.com
echo.
set /p REPO_URL="Paste your GitHub Repository URL here: "

if "%REPO_URL%"=="" (
    echo Error: No URL entered.
    pause
    exit /b
)

git remote remove origin >nul 2>&1
git remote add origin %REPO_URL%
git branch -M main
echo.
echo Pushing code to GitHub...
git push -u origin main
echo.
echo ========================================================
echo Done! Now go to railway.com and select "Deploy from GitHub repo".
echo ========================================================
pause
