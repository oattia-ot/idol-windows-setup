@echo off
REM ==============================================================================
REM  Setup.bat - bootstrap launcher for Initialize-Environment.ps1
REM
REM  Why this exists: on machines where the PowerShell execution policy is
REM  "AllSigned" or "Restricted", PowerShell refuses to even LOAD an unsigned
REM  .ps1 file - including Initialize-Environment.ps1, whose whole job is to
REM  relax that policy for you. That's a chicken-and-egg problem: the script
REM  meant to fix the policy can't run because of the policy.
REM
REM  Batch files aren't subject to PowerShell's script execution policy, so
REM  this wrapper launches PowerShell with "-ExecutionPolicy Bypass" for just
REM  this one process (it does NOT change your machine's saved policy - that
REM  part is still handled by Initialize-Environment.ps1 itself, as documented
REM  in INSTALL.md).
REM
REM  Right-click this file and "Run as administrator" (Initialize-Environment.ps1
REM  requires elevation).
REM ==============================================================================

setlocal
set "SCRIPT_DIR=%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%Initialize-Environment.ps1" %*
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
    echo.
    echo ============================================================
    echo  Initialize-Environment.ps1 exited with code %EXITCODE%.
    echo.
    echo  If PowerShell reported something like:
    echo    "...cannot be loaded. ... is not digitally signed."
    echo  even through this wrapper, your organization is enforcing
    echo  the execution policy at the MachinePolicy scope via Group
    echo  Policy, and "-ExecutionPolicy Bypass" is being overridden.
    echo  Run this in an elevated PowerShell prompt to check:
    echo    Get-ExecutionPolicy -List
    echo  and ask your admin for an exception, or have the scripts
    echo  code-signed, if MachinePolicy or UserPolicy shows a value
    echo  other than "Undefined".
    echo ============================================================
)

pause
exit /b %EXITCODE%
