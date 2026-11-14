@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
set "ERR=%ERRORLEVEL%"
echo.
if not "%ERR%"=="0" echo Uninstall failed.
pause
exit /b %ERR%
