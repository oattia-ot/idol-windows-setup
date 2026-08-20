@echo off
REM ==============================================================================
REM  Stop-All-KDServices.bat
REM  Stop every deployed KD-* Windows service (others first, LicenseServer last).
REM
REM  Requires Administrator.
REM  Wrapper around: python manage_kd_services.py stop --all-deployed --non-interactive
REM ==============================================================================
setlocal
set "SCRIPT_DIR=%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo ERROR: Run this script as Administrator.
    echo Right-click Stop-All-KDServices.bat -^> Run as administrator
    pause
    exit /b 1
)

echo === Stopping all deployed KD-* services ===
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%Manage-KDServices.ps1" stop -AllDeployed -NonInteractive
set "EXITCODE=%ERRORLEVEL%"
echo.
if not "%EXITCODE%"=="0" (
    echo Finished with errors ^(exit %EXITCODE%^).
) else (
    echo All requested stops completed.
)
pause
exit /b %EXITCODE%
