<#
.SYNOPSIS
    Start / stop / restart / delete / uninstall / create KD Windows services.

.DESCRIPTION
    Thin elevated wrapper around manage_kd_services.py.

    Target selection (first match wins):
      1. -Components <list>   - explicit component or service names
      2. -AllDeployed         - every Windows service named KD-* on this machine
      3. config Components    - from config\my-config.json
      4. (fallback)           - all deployed KD-* services if Components is empty

    Service naming: each component "X" maps to Windows service "KD-X"
    (e.g. Content -> KD-Content, NiFi -> KD-NiFi). You may also pass full
    service names (KD-Content) via -Components.

.PARAMETER Action
    status | start | stop | restart | delete | uninstall | create

.PARAMETER ConfigPath
    Path to JSON config. Default: config\my-config.json if present,
    else config\default-config.json

.PARAMETER Components
    Optional comma-separated override (e.g. "Content,Community,NiFi"
    or "KD-Content,KD-NiFi")

.PARAMETER AllDeployed
    Operate on every KD-* Windows service registered on this machine,
    regardless of the Components list in config.

.PARAMETER DryRun
    Show what would happen without changing services.

.PARAMETER Force
    Skip confirmation for delete / uninstall.

.PARAMETER NonInteractive
    No prompts (implies Force for destructive actions).

.EXAMPLE
    .\Manage-KDServices.ps1 status

.EXAMPLE
    .\Manage-KDServices.ps1 start -AllDeployed

.EXAMPLE
    .\Manage-KDServices.ps1 start -Components Content,NiFi -NonInteractive

.EXAMPLE
    .\Manage-KDServices.ps1 stop

.EXAMPLE
    .\Manage-KDServices.ps1 delete -Components Content,Community -Force

.EXAMPLE
    .\Manage-KDServices.ps1 uninstall -ConfigPath .\config\my-config.json -Force

.NOTES
    Requires Administrator for start/stop/restart/delete/uninstall/create.
    create registers services (native -install for KD components; NSSM for NiFi).
    status works without elevation.
#>

#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('status', 'start', 'stop', 'restart', 'delete', 'uninstall', 'create')]
    [string]$Action = 'status',

    [string]$ConfigPath,

    [string]$Components,

    [Alias('a')]
    [switch]$AllDeployed,

    [switch]$DryRun,
    [switch]$Force,
    [switch]$NonInteractive,

    [Alias('h')]
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
$scriptRoot = $PSScriptRoot

function Show-Usage {
    Write-Host ''
    Write-Host 'KD Service Manager' -ForegroundColor Cyan
    Write-Host '  Start / stop / status any deployed KD-* Windows service' -ForegroundColor DarkCyan
    Write-Host ''
    Write-Host 'USAGE' -ForegroundColor Yellow
    Write-Host '  .\Manage-KDServices.ps1 <action> [options]'
    Write-Host ''
    Write-Host 'ACTIONS' -ForegroundColor Yellow
    Write-Host '  status      Show Running / Stopped / Missing for each service'
    Write-Host '  start       Start services (LicenseServer first; NiFi waits for Flow Controller)'
    Write-Host '  stop        Stop services (LicenseServer last)'
    Write-Host '  restart     stop then start'
    Write-Host '  delete      Stop + sc delete (service registration only; files kept)'
    Write-Host '  uninstall   Binary -remove (if found) + sc delete (folders still kept)'
    Write-Host '  create      Register services (native -install; NiFi via NSSM) for config Components'
    Write-Host '              For full folder cleanup use: .\Install-KD.ps1 -Mode Uninstall'
    Write-Host ''
    Write-Host 'OPTIONS' -ForegroundColor Yellow
    Write-Host '  -ConfigPath <path>     JSON config (default: config\my-config.json)'
    Write-Host '  -Components <list>     Override, e.g. Content,Community,NiFi  (or KD-Content,...)'
    Write-Host '  -AllDeployed, -a       All registered KD-* services on this machine'
    Write-Host '  -DryRun                Print only'
    Write-Host '  -Force                 Skip YES confirmation on delete/uninstall'
    Write-Host '  -NonInteractive        No prompts'
    Write-Host '  -Help, -h              This help'
    Write-Host ''
    Write-Host 'EXAMPLES' -ForegroundColor Yellow
    Write-Host '  .\Manage-KDServices.ps1 status'
    Write-Host '  .\Manage-KDServices.ps1 start -AllDeployed'
    Write-Host '  .\Manage-KDServices.ps1 start -Components Content,NiFi -NonInteractive'
    Write-Host '  .\Manage-KDServices.ps1 stop'
    Write-Host '  .\Manage-KDServices.ps1 delete -Force'
    Write-Host '  .\Manage-KDServices.ps1 create -NonInteractive'
    Write-Host '  .\Manage-KDServices.ps1 uninstall -Components NiFi -Force'
    Write-Host ''
}

if ($Help) {
    Show-Usage
    exit 0
}

# Elevation for mutating actions
$needsAdmin = $Action -in @('start', 'stop', 'restart', 'delete', 'uninstall', 'create')
if ($needsAdmin) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host "ERROR: Action '$Action' requires Administrator." -ForegroundColor Red
        Write-Host 'Right-click PowerShell -> Run as administrator, then re-run.' -ForegroundColor Yellow
        exit 1
    }
}

function Test-PythonWorks {
    param([Parameter(Mandatory)][string]$ExePath)
    if ($ExePath -match '(?i)[\\/]WindowsApps[\\/]') { return $false }
    if (-not (Test-Path -LiteralPath $ExePath)) { return $false }
    try {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $out = & $ExePath -c "import sys; print(sys.version_info[0])" 2>$null
        $ErrorActionPreference = $prev
        if ($LASTEXITCODE -ne 0) { return $false }
        return ($out -match '^[23]$')
    } catch {
        return $false
    }
}

function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($name in @('python', 'python3', 'py')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -ne $cmd -and $cmd.Source) { $candidates.Add($cmd.Source) | Out-Null }
    }
    $extra = @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "$env:ProgramFiles\Python311\python.exe"
    )
    foreach ($p in $extra) {
        if ($p -and (Test-Path -LiteralPath $p)) { $candidates.Add($p) | Out-Null }
    }
    foreach ($c in $candidates) {
        if (Test-PythonWorks -ExePath $c) { return $c }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host 'ERROR: Python 3.8+ not found.' -ForegroundColor Red
    exit 1
}

$pyScript = Join-Path $scriptRoot 'manage_kd_services.py'
if (-not (Test-Path -LiteralPath $pyScript)) {
    Write-Host "ERROR: Missing $pyScript" -ForegroundColor Red
    exit 1
}

$pyArgs = @($pyScript, $Action)
if ($ConfigPath) { $pyArgs += @('--config', $ConfigPath) }
if ($Components) { $pyArgs += @('--components', $Components) }
if ($AllDeployed) { $pyArgs += '--all-deployed' }
if ($DryRun) { $pyArgs += '--dry-run' }
if ($Force) { $pyArgs += '--force' }
if ($NonInteractive) { $pyArgs += '--non-interactive' }

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'

& $python @pyArgs
exit $LASTEXITCODE
