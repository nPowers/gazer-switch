@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
set "ERR=%ERRORLEVEL%"
echo.
if not "%ERR%"=="0" echo Install failed.
pause
exit /b %ERR%
