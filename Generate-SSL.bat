@echo off
REM ==============================================================================
REM  Generate-SSL.bat - bootstrap launcher for tools\generate_ssl.py
REM
REM  Preferred SSL generator for Windows: uses Python cryptography (no OpenSSL
REM  CA database / index.txt). Avoids the common PowerShell+OpenSSL failure:
REM    "Problem with index file: ...\index.txt (could not load/parse file)"
REM
REM  Usage:
REM    Generate-SSL.bat
REM    Generate-SSL.bat --auto
REM    Generate-SSL.bat --auto --kd-services
REM    Generate-SSL.bat --auto --services content,community,nifi
REM
REM  Legacy: Generate-SSL.ps1 still exists but requires a working OpenSSL CA
REM  setup; prefer this wrapper on Windows.
REM ==============================================================================

setlocal
set "SCRIPT_DIR=%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    where py >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python not found. Install Python 3.8+ and re-run.
        echo         winget install Python.Python.3.12
        pause
        exit /b 1
    )
    set "PYTHON=py"
) else (
    set "PYTHON=python"
)

REM Ensure cryptography is available
%PYTHON% -c "import cryptography" 2>nul
if errorlevel 1 (
    echo [INFO] Installing cryptography...
    %PYTHON% -m pip install --quiet "cryptography>=42.0.0"
    if errorlevel 1 (
        echo [ERROR] Could not install cryptography. Run: pip install cryptography
        pause
        exit /b 1
    )
)

%PYTHON% "%SCRIPT_DIR%tools\generate_ssl.py" %*
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
    echo.
    echo generate_ssl.py exited with code %EXITCODE%.
)
pause
exit /b %EXITCODE%
