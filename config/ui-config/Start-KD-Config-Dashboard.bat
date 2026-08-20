@echo off
setlocal EnableExtensions
title KD Configuration Dashboard

REM ---------------------------------------------------------------------------
REM  Location:
REM    This script lives in:  <setup>\config\ui-config\
REM    Config is written to:  <setup>\config\my-config.json
REM ---------------------------------------------------------------------------

cd /d "%~dp0"

if not exist "%~dp0kd-config-server.py" (
  echo.
  echo  ERROR: kd-config-server.py not found in:
  echo    %~dp0
  echo.
  pause
  exit /b 1
)
if not exist "%~dp0kd-config-dashboard.html" (
  echo.
  echo  ERROR: kd-config-dashboard.html not found in:
  echo    %~dp0
  echo.
  pause
  exit /b 1
)
if not exist "%~dp0kd-nifi-auto-fix.js" (
  echo.
  echo  WARNING: kd-nifi-auto-fix.js is missing.
  echo  NiFi auto-sync features may not work.
  echo.
)

REM Prefer explicit override, otherwise resolve setup root from folder layout
if not defined KD_SETUP_ROOT (
  if exist "%~dp0..\my-config.json" (
    for %%I in ("%~dp0..\..") do set "KD_SETUP_ROOT=%%~fI"
  ) else if exist "C:\KD-Setup\idol-windows-setup\config\my-config.json" (
    set "KD_SETUP_ROOT=C:\KD-Setup\idol-windows-setup"
  ) else if exist "C:\KD-Setup\idol-windows-setup-main\config\my-config.json" (
    set "KD_SETUP_ROOT=C:\KD-Setup\idol-windows-setup-main"
  )
)

echo.
echo  ================================================================
echo   KD Configuration Dashboard  (pre-install step)
echo   Edit Ports / Components / Browser URLs, then Save / Export JSON
echo   before running the installer.
echo  ================================================================
echo.
if defined KD_SETUP_ROOT (
  echo   Setup root  : %KD_SETUP_ROOT%
  echo   Export to   : %KD_SETUP_ROOT%\config\my-config.json
) else (
  echo   WARNING: could not resolve setup root; export path may be wrong.
  echo   Set KD_SETUP_ROOT if needed, e.g.:
  echo     set KD_SETUP_ROOT=C:\KD-Setup\idol-windows-setup
)
echo   Local URL   : http://127.0.0.1:5000/kd-config-dashboard.html
echo   Bind        : 0.0.0.0:5000  (external clients allowed)
echo   Press Ctrl+C in this window to stop the server.
echo.

REM Open Windows Firewall for dashboard port 5000 (idempotent; safe if already open)
REM Single-line PowerShell to avoid fragile ^ continuations leaving a bad ERRORLEVEL.
echo   Opening Windows Firewall for dashboard port 5000 ...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "try { $tcp='Temp-KD-5000'; $icmp='Temp-ICMP'; if (-not (Get-NetFirewallRule -DisplayName $tcp -EA SilentlyContinue)) { New-NetFirewallRule -DisplayName $tcp -Direction Inbound -Protocol TCP -LocalPort 5000 -Action Allow -Profile Any | Out-Null; Write-Host '  Firewall rule created: Temp-KD-5000 (TCP 5000 inbound Allow)' -ForegroundColor Green } else { Write-Host '  Firewall rule already present: Temp-KD-5000 (TCP 5000)' -ForegroundColor DarkGray }; if (-not (Get-NetFirewallRule -DisplayName $icmp -EA SilentlyContinue)) { New-NetFirewallRule -DisplayName $icmp -Direction Inbound -Protocol ICMPv4 -Action Allow -Profile Any | Out-Null; Write-Host '  Firewall rule created: Temp-ICMP (ICMPv4 inbound Allow)' -ForegroundColor Green } else { Write-Host '  Firewall rule already present: Temp-ICMP' -ForegroundColor DarkGray } } catch { Write-Host ('  WARNING: could not open firewall for port 5000: ' + $_.Exception.Message) -ForegroundColor Yellow }"
REM Firewall is best-effort; always continue to start the server.

set "PYEXE="
where python >nul 2>&1 && set "PYEXE=python"
if not defined PYEXE (
  where py >nul 2>&1 && set "PYEXE=py -3"
)
if not defined PYEXE (
  echo  ERROR: Python was not found on PATH.
  echo  Install Python 3 from https://www.python.org/downloads/
  echo  and ensure "Add python.exe to PATH" is checked.
  echo.
  pause
  exit /b 1
)

REM Listen on all interfaces so external clients can reach the dashboard
if defined KD_SETUP_ROOT (
  %PYEXE% "%~dp0kd-config-server.py" --host 0.0.0.0 --port 5000 --setup-root "%KD_SETUP_ROOT%"
) else (
  %PYEXE% "%~dp0kd-config-server.py" --host 0.0.0.0 --port 5000
)
set "RC=%ERRORLEVEL%"
REM Ctrl+C / clean stop from Python returns 0. Closing the console with X
REM often reports 255 — treat both as a normal end so callers do not WARN.
if "%RC%"=="0" goto :done_ok
if "%RC%"=="255" goto :done_ok
echo.
echo  Server exited with code %RC%.
pause
exit /b %RC%
:done_ok
exit /b 0
