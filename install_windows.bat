@echo off
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_windows.ps1"

if errorlevel 1 (
    echo.
    echo Installation failed.
    pause
    exit /b 1
)

echo.
echo Installation completed successfully.
pause
