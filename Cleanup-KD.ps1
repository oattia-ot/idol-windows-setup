<#
.SYNOPSIS
    Completely cleans up a previous Knowledge Discovery installation.
    Thin wrapper that calls the Python backend in Uninstall mode, then
    optionally removes the BasePath folder itself.
#>

#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$BasePath = "C:\KnowledgeDiscovery\26.2",
    [string]$ConfigPath,
    [switch]$DryRun
)

$scriptRoot = $PSScriptRoot
$installScript = Join-Path $scriptRoot "Install-KD.ps1"

Write-Host "=== KD Cleanup Utility (Python backend) ===" -ForegroundColor Cyan
Write-Host "This will STOP and DELETE all KD-* services"
Write-Host "and then DELETE component folders under: $BasePath`n"

if (-not $DryRun) {
    Write-Host -ForegroundColor Yellow -NoNewline "Are you sure? Type YES to continue "
    $confirm = Read-Host
    if ($confirm -ne 'YES') {
        Write-Host "Aborted." -ForegroundColor Yellow
        exit 0
    }
}

$params = @{
    Mode           = 'Uninstall'
    NonInteractive = $true
}
if ($ConfigPath) { $params['ConfigPath'] = $ConfigPath }
if ($DryRun)     { $params['DryRun']     = $true }

& $installScript @params

# Extra: remove the entire BasePath if still present (state file, logs, etc.)
if ((Test-Path $BasePath) -and -not $DryRun) {
    Write-Host "`nRemoving remaining BasePath folder: $BasePath ..." -NoNewline
    try {
        Remove-Item -Path $BasePath -Recurse -Force -ErrorAction Stop
        Write-Host " DELETED" -ForegroundColor Green
    } catch {
        Write-Host " FAILED: $_" -ForegroundColor Red
    }
}

Write-Host "`nCleanup complete." -ForegroundColor Cyan
