@echo off
chcp 65001 >nul
echo ========================================================
echo   MilaDownloadBot - Upload to GitHub
echo ========================================================
echo.
echo Sending all project files to:
echo   https://github.com/miladmrsaadat/MilaDownloadBot
echo.
echo Please wait...
git push -u origin main
echo.
if %errorlevel% equ 0 (
    echo ========================================================
    echo  [OK] Done! Your code is now live on GitHub!
    echo ========================================================
) else (
    echo ========================================================
    echo  [X] Failed. Make sure your VPN is ON and try again.
    echo ========================================================
)
echo.
pause
