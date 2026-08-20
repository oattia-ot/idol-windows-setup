@echo off
setlocal
REM Clean unnecessary toolkit files (ssl\, __pycache__, optional BasePath logs/state)
REM Usage:
REM   Clean-Unnecessary.bat
REM   Clean-Unnecessary.bat -BasePath "C:\KnowledgeDiscovery\26.2"
REM   Clean-Unnecessary.bat -DryRun
REM   Clean-Unnecessary.bat -Force

cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Clean-Unnecessary.ps1" %*
exit /b %ERRORLEVEL%
