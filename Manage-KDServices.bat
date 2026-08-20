@echo off
REM Bootstrap for Manage-KDServices.ps1 (avoids execution-policy chicken-and-egg)
setlocal
set "SCRIPT_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%Manage-KDServices.ps1" %*
exit /b %ERRORLEVEL%
