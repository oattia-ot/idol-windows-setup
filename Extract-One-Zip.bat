@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  Extract-One-Zip.bat
::  Extract a single ZIP to a destination folder.
::
::  Preferred tool: 7-Zip (7z.exe)
::  Fallback:       Windows tar.exe
::
::  Usage:
::    Extract-One-Zip.bat "C:\path\to\file.zip" "C:\path\to\destination"
::
::  If the ZIP has a single top-level folder, it is stripped
::  (same behavior as Unzip-One.bat / OpenText package layout).
:: ============================================================

if "%~1"=="" (
    echo ERROR: Missing ZIP path
    echo Usage: Extract-One-Zip.bat "zipfile.zip" "destination_folder"
    exit /b 1
)
if "%~2"=="" (
    echo ERROR: Missing destination path
    echo Usage: Extract-One-Zip.bat "zipfile.zip" "destination_folder"
    exit /b 1
)

set "ZIPFILE=%~1"
set "DEST=%~2"
set "TEMP_DIR=%DEST%\_tmp_extract"

if not exist "%ZIPFILE%" (
    echo ERROR: ZIP not found: %ZIPFILE%
    exit /b 1
)

:: Locate 7-Zip
set "SEVENZ="
if exist "C:\Program Files\7-Zip\7z.exe" set "SEVENZ=C:\Program Files\7-Zip\7z.exe"
if exist "C:\Program Files (x86)\7-Zip\7z.exe" set "SEVENZ=C:\Program Files (x86)\7-Zip\7z.exe"
if exist "%ProgramFiles%\7-Zip\7z.exe" set "SEVENZ=%ProgramFiles%\7-Zip\7z.exe"
where 7z.exe >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%A in ('where 7z.exe 2^>nul') do (
        if not defined SEVENZ set "SEVENZ=%%A"
    )
)

set "TAR="
where tar.exe >nul 2>&1
if not errorlevel 1 set "TAR=tar.exe"
if exist "%SystemRoot%\System32\tar.exe" set "TAR=%SystemRoot%\System32\tar.exe"

if defined SEVENZ (
    set "ENGINE=7z"
) else if defined TAR (
    set "ENGINE=tar"
) else (
    echo Neither 7-Zip nor tar.exe found. Attempting auto-install...
    set "SCRIPT_DIR=%~dp0"
    call "%SCRIPT_DIR%Ensure-ExtractTools.bat" /quiet
    if errorlevel 1 (
        echo ERROR: Could not install extract tools.
        echo Install 7-Zip from https://www.7-zip.org/
        exit /b 1
    )
    set "SEVENZ="
    set "TAR="
    if exist "C:\Program Files\7-Zip\7z.exe" set "SEVENZ=C:\Program Files\7-Zip\7z.exe"
    if exist "C:\Program Files (x86)\7-Zip\7z.exe" set "SEVENZ=C:\Program Files (x86)\7-Zip\7z.exe"
    if exist "%ProgramFiles%\7-Zip\7z.exe" set "SEVENZ=%ProgramFiles%\7-Zip\7z.exe"
    where 7z.exe >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%A in ('where 7z.exe 2^>nul') do (
            if not defined SEVENZ set "SEVENZ=%%A"
        )
    )
    where tar.exe >nul 2>&1
    if not errorlevel 1 set "TAR=tar.exe"
    if exist "%SystemRoot%\System32\tar.exe" set "TAR=%SystemRoot%\System32\tar.exe"

    if defined SEVENZ (
        set "ENGINE=7z"
    ) else if defined TAR (
        set "ENGINE=tar"
    ) else (
        echo ERROR: Tools still missing after install attempt.
        exit /b 1
    )
)

if exist "%DEST%" rd /s /q "%DEST%" 2>nul
mkdir "%DEST%" 2>nul
if errorlevel 1 (
    echo ERROR: Could not create destination: %DEST%
    exit /b 1
)
mkdir "%TEMP_DIR%" 2>nul

echo Extracting with %ENGINE%: %ZIPFILE%
echo Destination: %DEST%

if /I "%ENGINE%"=="7z" (
    "%SEVENZ%" x "%ZIPFILE%" -o"%TEMP_DIR%" -y
    if errorlevel 1 (
        echo ERROR: 7z failed for %ZIPFILE%
        rd /s /q "%TEMP_DIR%" 2>nul
        exit /b 1
    )
) else (
    "%TAR%" -xf "%ZIPFILE%" -C "%TEMP_DIR%"
    if errorlevel 1 (
        echo ERROR: tar failed for %ZIPFILE%
        rd /s /q "%TEMP_DIR%" 2>nul
        exit /b 1
    )
)

:: Strip single top-level folder if present
set "COUNT=0"
set "INNER="
for /f "delims=" %%I in ('dir /b /a "%TEMP_DIR%" 2^>nul') do (
    set /a COUNT+=1
    set "INNER=%%I"
)

if !COUNT! EQU 1 (
    if exist "%TEMP_DIR%\!INNER!\" (
        xcopy "%TEMP_DIR%\!INNER!\*" "%DEST%\" /E /H /Y /Q >nul
        if errorlevel 1 (
            echo ERROR: xcopy failed while stripping root folder
            rd /s /q "%TEMP_DIR%" 2>nul
            exit /b 1
        )
        rd /s /q "%TEMP_DIR%" 2>nul
        echo OK - root folder stripped: !INNER!
        exit /b 0
    )
)

xcopy "%TEMP_DIR%\*" "%DEST%\" /E /H /Y /Q >nul
if errorlevel 1 (
    echo ERROR: xcopy failed
    rd /s /q "%TEMP_DIR%" 2>nul
    exit /b 1
)
rd /s /q "%TEMP_DIR%" 2>nul
echo OK
exit /b 0
