@echo off
REM ==============================================================================
REM  Start-All-KDServices.bat
REM  Start every deployed KD-* Windows service (LicenseServer first, then others).
REM  NiFi waits for the Flow Controller marker in nifi-app.log.
REM
REM  Requires Administrator.
REM  Wrapper around: python manage_kd_services.py start --all-deployed --non-interactive
REM ==============================================================================
setlocal
set "SCRIPT_DIR=%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo ERROR: Run this script as Administrator.
    echo Right-click Start-All-KDServices.bat -^> Run as administrator
    pause
    exit /b 1
)

echo === Starting all deployed KD-* services ===
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%Manage-KDServices.ps1" start -AllDeployed -NonInteractive
set "EXITCODE=%ERRORLEVEL%"
echo.
if not "%EXITCODE%"=="0" (
    echo Finished with errors ^(exit %EXITCODE%^).
) else (
    echo All requested starts completed.
)
pause
exit /b %EXITCODE%
