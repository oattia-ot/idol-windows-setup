@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  Extract-All-Zips.bat
::  Extract every .zip in a folder to a target location.
::
::  Preferred tool: 7-Zip (7z.exe)
::  Fallback:       Windows tar.exe (built into Win10/11/Server)
::
::  Usage:
::    Extract-All-Zips.bat
::    Extract-All-Zips.bat "C:\KD-Setup\zip-folder"
::    Extract-All-Zips.bat "C:\KD-Setup\zip-folder" "C:\KnowledgeDiscovery\26.2"
::
::  Each archive is extracted to:  TARGET\<zip-base-name>\
::  If the ZIP contains a single top-level folder, that folder is
::  stripped so files land directly under TARGET\<zip-base-name>\
:: ============================================================

set "SCRIPT_DIR=%~dp0"
set "ZIP_FOLDER=%~1"
if "%ZIP_FOLDER%"=="" set "ZIP_FOLDER=C:\KD-Setup\zip-folder"

set "TARGET_FOLDER=%~2"
if "%TARGET_FOLDER%"=="" set "TARGET_FOLDER=C:\KnowledgeDiscovery\26.2"

if not exist "%ZIP_FOLDER%" (
    echo ERROR: Zip folder not found: %ZIP_FOLDER%
    echo.
    echo Usage: Extract-All-Zips.bat "source-zip-folder" "target-folder"
    exit /b 1
)

if not exist "%TARGET_FOLDER%" (
    echo Creating target folder: %TARGET_FOLDER%
    mkdir "%TARGET_FOLDER%" 2>nul
)

:: ------------------------------------------------------------
:: Locate 7-Zip
:: ------------------------------------------------------------
set "SEVENZ="
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

:: ------------------------------------------------------------
:: Locate tar.exe (Windows built-in)
:: ------------------------------------------------------------
set "TAR="
where tar.exe >nul 2>&1
if not errorlevel 1 set "TAR=tar.exe"
if exist "%SystemRoot%\System32\tar.exe" set "TAR=%SystemRoot%\System32\tar.exe"

if defined SEVENZ (
    set "ENGINE=7z"
    echo Using 7-Zip: %SEVENZ%
) else if defined TAR (
    set "ENGINE=tar"
    echo Using tar.exe: %TAR%
) else (
    echo Neither 7-Zip nor tar.exe found. Attempting auto-install...
    call "%SCRIPT_DIR%Ensure-ExtractTools.bat" /quiet
    if errorlevel 1 (
        echo ERROR: Could not install extract tools.
        echo Install 7-Zip from https://www.7-zip.org/
        exit /b 1
    )
    :: Re-detect after install
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
        echo Using 7-Zip: %SEVENZ%
    ) else if defined TAR (
        set "ENGINE=tar"
        echo Using tar.exe: %TAR%
    ) else (
        echo ERROR: Tools still missing after install attempt.
        exit /b 1
    )
)

echo.
echo Source  (ZIPs)  : %ZIP_FOLDER%
echo Target (extract): %TARGET_FOLDER%
echo Engine          : %ENGINE%
echo.

set "FAILED=0"
set "OKCOUNT=0"

for %%F in ("%ZIP_FOLDER%\*.zip") do (
    set "NAME=%%~nF"
    set "DEST=%TARGET_FOLDER%\!NAME!"
    set "TEMP_DIR=!DEST!\_tmp_extract"

    echo --------------------------------------------------
    echo Extracting: %%~nxF
    echo Destination: !DEST!

    if exist "!DEST!" rd /s /q "!DEST!" 2>nul
    mkdir "!DEST!" 2>nul
    mkdir "!TEMP_DIR!" 2>nul

    set "EXTRACT_OK=0"

    if /I "%ENGINE%"=="7z" (
        "%SEVENZ%" x "%%F" -o"!TEMP_DIR!" -y >nul
        if not errorlevel 1 set "EXTRACT_OK=1"
    ) else (
        "%TAR%" -xf "%%F" -C "!TEMP_DIR!"
        if not errorlevel 1 set "EXTRACT_OK=1"
    )

    if "!EXTRACT_OK!"=="0" (
        echo   [FAILED] extract failed for %%~nxF
        rd /s /q "!TEMP_DIR!" 2>nul
        set /a FAILED+=1
    ) else (
        :: Detect single top-level folder and strip it
        set "COUNT=0"
        set "INNER="
        for /f "delims=" %%I in ('dir /b /a "!TEMP_DIR!" 2^>nul') do (
            set /a COUNT+=1
            set "INNER=%%I"
        )

        if !COUNT! EQU 1 (
            if exist "!TEMP_DIR!\!INNER!\" (
                xcopy "!TEMP_DIR!\!INNER!\*" "!DEST!\" /E /H /Y /Q >nul
                if errorlevel 1 (
                    echo   [FAILED] xcopy while stripping root folder
                    rd /s /q "!TEMP_DIR!" 2>nul
                    set /a FAILED+=1
                ) else (
                    rd /s /q "!TEMP_DIR!" 2>nul
                    echo   [OK] %%~nxF  ^(root folder stripped: !INNER!^)
                    set /a OKCOUNT+=1
                )
            ) else (
                xcopy "!TEMP_DIR!\*" "!DEST!\" /E /H /Y /Q >nul
                rd /s /q "!TEMP_DIR!" 2>nul
                echo   [OK] %%~nxF
                set /a OKCOUNT+=1
            )
        ) else (
            xcopy "!TEMP_DIR!\*" "!DEST!\" /E /H /Y /Q >nul
            if errorlevel 1 (
                echo   [FAILED] xcopy
                rd /s /q "!TEMP_DIR!" 2>nul
                set /a FAILED+=1
            ) else (
                rd /s /q "!TEMP_DIR!" 2>nul
                echo   [OK] %%~nxF
                set /a OKCOUNT+=1
            )
        )
    )
)

echo.
echo --------------------------------------------------
echo Done. OK=%OKCOUNT%  Failed=%FAILED%
if %FAILED% GTR 0 (
    echo Finished with %FAILED% failure^(s^).
    exit /b 1
) else (
    echo All packages extracted successfully to:
    echo   %TARGET_FOLDER%
    exit /b 0
)
