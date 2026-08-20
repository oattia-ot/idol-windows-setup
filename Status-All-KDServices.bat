@echo off
REM ==============================================================================
REM  Status-All-KDServices.bat
REM  Show Running / Stopped / Missing for every deployed KD-* Windows service.
REM ==============================================================================
setlocal
set "SCRIPT_DIR=%~dp0"

echo === Status of all deployed KD-* services ===
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%Manage-KDServices.ps1" status -AllDeployed
set "EXITCODE=%ERRORLEVEL%"
echo.
pause
exit /b %EXITCODE%
