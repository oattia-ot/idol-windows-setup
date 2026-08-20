@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  Ensure-ExtractTools.bat
::  Check for 7-Zip or Windows tar.exe. If neither is present,
::  install 7-Zip automatically (winget -> direct download).
::
::  Usage:
::    Ensure-ExtractTools.bat
::    Ensure-ExtractTools.bat /quiet
::
::  Exit codes:
::    0 = 7z or tar available
::    1 = could not install / still missing
:: ============================================================

set "QUIET=0"
if /I "%~1"=="/quiet" set "QUIET=1"
if /I "%~1"=="-quiet" set "QUIET=1"

call :FindTools
if defined SEVENZ (
    if "%QUIET%"=="0" echo [OK] 7-Zip found: %SEVENZ%
    exit /b 0
)
if defined TAR (
    if "%QUIET%"=="0" echo [OK] tar.exe found: %TAR%
    exit /b 0
)

if "%QUIET%"=="0" (
    echo [WARN] Neither 7-Zip nor tar.exe was found.
    echo        Installing 7-Zip...
)

:: --- Method 1: winget ---
where winget.exe >nul 2>&1
if not errorlevel 1 (
    if "%QUIET%"=="0" echo Trying winget install 7zip.7zip ...
    winget install --id 7zip.7zip -e --accept-package-agreements --accept-source-agreements --silent
    call :FindTools
    if defined SEVENZ (
        if "%QUIET%"=="0" echo [OK] 7-Zip installed via winget: %SEVENZ%
        exit /b 0
    )
)

:: --- Method 2: chocolatey (if present) ---
where choco.exe >nul 2>&1
if not errorlevel 1 (
    if "%QUIET%"=="0" echo Trying choco install 7zip ...
    choco install 7zip -y
    call :FindTools
    if defined SEVENZ (
        if "%QUIET%"=="0" echo [OK] 7-Zip installed via chocolatey: %SEVENZ%
        exit /b 0
    )
)

:: --- Method 3: download official installer (x64) ---
set "INSTALLER=%TEMP%\7z-setup.exe"
set "URL=https://www.7-zip.org/a/7z2409-x64.exe"

if "%QUIET%"=="0" echo Downloading 7-Zip from %URL% ...

:: Prefer curl (Win10+), else bitsadmin, else powershell
where curl.exe >nul 2>&1
if not errorlevel 1 (
    curl.exe -L -o "%INSTALLER%" "%URL%" 2>nul
) else (
    powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%URL%' -OutFile '%INSTALLER%' -UseBasicParsing } catch { exit 1 }"
)

if not exist "%INSTALLER%" (
    if "%QUIET%"=="0" (
        echo [FAIL] Could not download 7-Zip installer.
        echo        Install manually from https://www.7-zip.org/
    )
    exit /b 1
)

if "%QUIET%"=="0" echo Running silent install...
"%INSTALLER%" /S
:: Wait a moment for install to finish
timeout /t 3 /nobreak >nul

del "%INSTALLER%" >nul 2>&1

call :FindTools
if defined SEVENZ (
    if "%QUIET%"=="0" echo [OK] 7-Zip installed: %SEVENZ%
    exit /b 0
)

if "%QUIET%"=="0" (
    echo [FAIL] 7-Zip still not found after install attempt.
    echo        Install manually from https://www.7-zip.org/
    echo        Or use a Windows 10/11 machine with tar.exe in System32.
)
exit /b 1


:FindTools
set "SEVENZ="
set "TAR="

if exist "C:\Program Files\7-Zip\7z.exe" set "SEVENZ=C:\Program Files\7-Zip\7z.exe"
if exist "C:\Program Files (x86)\7-Zip\7z.exe" set "SEVENZ=C:\Program Files (x86)\7-Zip\7z.exe"
if exist "%ProgramFiles%\7-Zip\7z.exe" set "SEVENZ=%ProgramFiles%\7-Zip\7z.exe"
if exist "%ProgramFiles(x86)%\7-Zip\7z.exe" set "SEVENZ=%ProgramFiles(x86)%\7-Zip\7z.exe"
where 7z.exe >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%A in ('where 7z.exe 2^>nul') do (
        if not defined SEVENZ set "SEVENZ=%%A"
    )
)

where tar.exe >nul 2>&1
if not errorlevel 1 set "TAR=tar.exe"
if exist "%SystemRoot%\System32\tar.exe" set "TAR=%SystemRoot%\System32\tar.exe"
goto :eof
