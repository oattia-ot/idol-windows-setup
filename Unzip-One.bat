@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  Unzip-One.bat
::  Native extraction of a SINGLE zip using Windows tar.exe
::  Strips the first-level folder that OpenText packages contain.
::
::  Usage:
::    Unzip-One.bat "C:\path\to\file.zip" "C:\path\to\destination"
::
::  Returns exit code 0 on success, 1 on failure.
::  Emits progress lines so long extracts do not appear stuck.
:: ============================================================

if "%~1"=="" (
    echo ERROR: Missing ZIP path
    echo Usage: Unzip-One.bat "zipfile.zip" "destination_folder"
    exit /b 1
)
if "%~2"=="" (
    echo ERROR: Missing destination path
    echo Usage: Unzip-One.bat "zipfile.zip" "destination_folder"
    exit /b 1
)

set "ZIPFILE=%~1"
set "DEST=%~2"
set "TEMP_DIR=%DEST%\_tmp_extract"

if not exist "%ZIPFILE%" (
    echo ERROR: ZIP not found: %ZIPFILE%
    exit /b 1
)

echo [Unzip-One] Source: %ZIPFILE%
echo [Unzip-One] Dest:   %DEST%
for %%A in ("%ZIPFILE%") do echo [Unzip-One] Size:   %%~zA bytes

if exist "%DEST%" (
    echo [Unzip-One] Removing existing destination...
    rd /s /q "%DEST%" 2>nul
)
mkdir "%DEST%" 2>nul
if errorlevel 1 (
    echo ERROR: Could not create destination: %DEST%
    exit /b 1
)
if exist "%TEMP_DIR%" rd /s /q "%TEMP_DIR%" 2>nul
mkdir "%TEMP_DIR%" 2>nul

set "TAR=%SystemRoot%\System32\tar.exe"
if not exist "%TAR%" set "TAR=tar.exe"

echo [Unzip-One] Extracting with tar (this can take a long time for multi-GB packages)...
"%TAR%" -xf "%ZIPFILE%" -C "%TEMP_DIR%"
if errorlevel 1 (
    echo ERROR: tar failed for %ZIPFILE%
    rd /s /q "%TEMP_DIR%" 2>nul
    exit /b 1
)
echo [Unzip-One] tar extract finished; stripping top-level folder if present...

set "COUNT=0"
set "INNER="
for /f "delims=" %%I in ('dir /b /a "%TEMP_DIR%" 2^>nul') do (
    set /a COUNT+=1
    set "INNER=%%I"
)

if !COUNT! EQU 1 (
    if exist "%TEMP_DIR%\!INNER!\" (
        echo [Unzip-One] Single root folder: !INNER! - moving contents up...
        where robocopy.exe >nul 2>&1
        if not errorlevel 1 (
            robocopy "%TEMP_DIR%\!INNER!" "%DEST%" /E /MOVE /NFL /NDL /NJH /NJS /nc /ns /np >nul
            set "RC=!errorlevel!"
            if !RC! GEQ 8 (
                echo ERROR: robocopy failed while stripping root folder ^(code !RC!^)
                rd /s /q "%TEMP_DIR%" 2>nul
                exit /b 1
            )
        ) else (
            xcopy "%TEMP_DIR%\!INNER!\*" "%DEST%\" /E /H /Y /Q >nul
            if errorlevel 1 (
                echo ERROR: xcopy failed while stripping root folder
                rd /s /q "%TEMP_DIR%" 2>nul
                exit /b 1
            )
        )
        rd /s /q "%TEMP_DIR%" 2>nul
        echo [Unzip-One] Done.
        exit /b 0
    )
)

echo [Unzip-One] No single root folder - moving all contents...
where robocopy.exe >nul 2>&1
if not errorlevel 1 (
    robocopy "%TEMP_DIR%" "%DEST%" /E /MOVE /NFL /NDL /NJH /NJS /nc /ns /np >nul
    set "RC=!errorlevel!"
    if !RC! GEQ 8 (
        echo ERROR: robocopy failed ^(code !RC!^)
        rd /s /q "%TEMP_DIR%" 2>nul
        exit /b 1
    )
) else (
    xcopy "%TEMP_DIR%\*" "%DEST%\" /E /H /Y /Q >nul
    if errorlevel 1 (
        echo ERROR: xcopy failed
        rd /s /q "%TEMP_DIR%" 2>nul
        exit /b 1
    )
)
rd /s /q "%TEMP_DIR%" 2>nul
echo [Unzip-One] Done.
exit /b 0
