@echo off
REM ============================================================================
REM  Push-Version.bat - write version.txt (repo root) and push to GitHub
REM
REM  Usage:
REM    Push-Version.bat 0.6.21
REM    Push-Version.bat 0.6.21 "Release 0.6.21"
REM    set KD_VERSION=v6.42
REM    Push-Version.bat
REM
REM  Version resolution (first wins):
REM    1) command-line argument
REM    2) environment variable KD_VERSION
REM    3) environment variable VERSION
REM
REM  Script lives in:  <repo>\tools\  (or any subfolder under root)
REM  version.txt written to: <repo>\version.txt  (one level up)
REM  GitHub remote: https://github.com/oattia-ot/idol-windows-setup.git
REM ============================================================================
setlocal
set "SCRIPT_DIR=%~dp0"

set "VERSION_ARG=%~1"
set "MSG=%~2"

if not "%VERSION_ARG%"=="" (
    if "%MSG%"=="" (
        powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%Push-Version.ps1" -Version "%VERSION_ARG%"
    ) else (
        powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%Push-Version.ps1" -Version "%VERSION_ARG%" -Message "%MSG%"
    )
) else (
    REM No CLI version - PS1 will read KD_VERSION or VERSION from the environment
    if "%MSG%"=="" (
        powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%Push-Version.ps1"
    ) else (
        powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%Push-Version.ps1" -Message "%MSG%"
    )
)

exit /b %ERRORLEVEL%
