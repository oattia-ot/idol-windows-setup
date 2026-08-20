@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  Unzip-All.bat
::  Bulk-extract every .zip in a folder using native tar.exe
::  Calls Unzip-One.bat for each file (strips first-level folder).
::
::  Usage:
::    Unzip-All.bat
::    Unzip-All.bat "C:\KD-Setup\zip-folder"
::    Unzip-All.bat "C:\KD-Setup\zip-folder" "C:\KnowledgeDiscovery\26.2"
:: ============================================================

set "SCRIPT_DIR=%~dp0"
set "ZIP_FOLDER=%~1"
if "%ZIP_FOLDER%"=="" set "ZIP_FOLDER=C:\KD-Setup\zip-folder"

set "TARGET_FOLDER=%~2"
if "%TARGET_FOLDER%"=="" set "TARGET_FOLDER=C:\KnowledgeDiscovery\26.2"

if not exist "%ZIP_FOLDER%" (
    echo ERROR: Zip folder not found: %ZIP_FOLDER%
    pause
    exit /b 1
)

if not exist "%TARGET_FOLDER%" (
    echo Creating target folder: %TARGET_FOLDER%
    mkdir "%TARGET_FOLDER%"
)

echo.
echo Source  (ZIPs)  : %ZIP_FOLDER%
echo Target (extract): %TARGET_FOLDER%
echo.

set "FAILED=0"
for %%F in ("%ZIP_FOLDER%\*.zip") do (
    set "NAME=%%~nF"
    echo --------------------------------------------------
    echo Extracting: %%~nxF
    echo Destination: %TARGET_FOLDER%\!NAME!
    call "%SCRIPT_DIR%Unzip-One.bat" "%%F" "%TARGET_FOLDER%\!NAME!"
    if errorlevel 1 (
        echo   [FAILED] %%~nxF
        set /a FAILED+=1
    ) else (
        echo   [OK] %%~nxF
    )
)

echo.
if %FAILED% GTR 0 (
    echo Finished with %FAILED% failure(s).
) else (
    echo Done. All packages extracted successfully.
)
pause
