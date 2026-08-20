<#
.SYNOPSIS
    One-time environment prep for the KD Python Installer toolkit.
#>

#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [ValidateSet('Process', 'CurrentUser')]
    [string]$ExecutionPolicyScope = 'Process',
    [switch]$SkipExecutionPolicy
)

function Write-Step($Text) { Write-Host $Text -ForegroundColor Cyan }
function Write-Ok($Text)   { Write-Host "  [OK] $Text" -ForegroundColor Green }
function Write-Warn2($Text){ Write-Host "  [WARN] $Text" -ForegroundColor Yellow }
function Write-Fail($Text) { Write-Host "  [FAIL] $Text" -ForegroundColor Red }

$root = $PSScriptRoot
$overallOk = $true

Clear-Host
Write-Host 'KD Installer (Python) - Environment Preparation' -ForegroundColor Cyan
Write-Host "Toolkit root: $root`n"

# 1. Elevation
Write-Step '[1/5] Checking Administrator privileges...'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Ok 'Running elevated'
} else {
    Write-Fail 'Not running as Administrator.'
    $overallOk = $false
}

# 2. Python
Write-Step '[2/5] Checking Python...'
$python = $null
foreach ($c in @('python', 'python3', 'py')) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source; break }
}
if ($python) {
    $ver = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>$null
    Write-Ok "Python $ver ($python)"
} else {
    Write-Fail 'Python 3.8+ not found. Install from https://www.python.org or winget install Python.Python.3.12'
    $overallOk = $false
}

# 3. Unblock files
Write-Step '[3/5] Unblocking files (removing Mark-of-the-Web)...'
try {
    $files = Get-ChildItem -Path $root -Recurse -File -ErrorAction Stop
    $blocked = 0
    foreach ($f in $files) {
        $zone = Get-Item -Path $f.FullName -Stream Zone.Identifier -ErrorAction SilentlyContinue
        if ($zone) { $blocked++ }
        Unblock-File -Path $f.FullName -ErrorAction SilentlyContinue
    }
    Write-Ok "Processed $($files.Count) files ($blocked had a zone-identifier flag)"
} catch {
    Write-Fail "Failed while unblocking: $_"
    $overallOk = $false
}

# 4. Execution policy (for the .ps1 wrappers)
Write-Step '[4/5] Checking / setting execution policy...'
if ($SkipExecutionPolicy) {
    Write-Warn2 "Skipped. Current process policy: $(Get-ExecutionPolicy -Scope Process)"
} else {
    try {
        Set-ExecutionPolicy -Scope $ExecutionPolicyScope -ExecutionPolicy RemoteSigned -Force -ErrorAction Stop
        Write-Ok "Execution policy set to RemoteSigned for scope '$ExecutionPolicyScope'"
    } catch {
        Write-Fail "Could not set execution policy: $_"
        Write-Warn2 'Fallback: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force'
        $overallOk = $false
    }
}

# 5. Verify layout
Write-Step '[5/5] Verifying toolkit files...'
$expected = @(
    'Install-KD.ps1',
    'install_kd.py',
    'config\default-config.json',
	'Generate-SSL.ps1',
    'kd\config.py',
    'kd\installer.py',
    'kd\service_manager.py',
    'Unzip-One.bat'
)
$missing = @()
foreach ($rel in $expected) {
    if (-not (Test-Path (Join-Path $root $rel))) { $missing += $rel }
}
if ($missing.Count -eq 0) {
    Write-Ok "All $($expected.Count) expected files present"
} else {
    Write-Fail "Missing: $($missing -join ', ')"
    $overallOk = $false
}

# Offer config copy
Write-Host ''
$defaultConfig = Join-Path $root 'config\default-config.json'
$workingConfig = Join-Path $root 'config\my-config.json'
if ((Test-Path $defaultConfig) -and -not (Test-Path $workingConfig)) {
    Write-Host -ForegroundColor Yellow -NoNewline "Create config\my-config.json from the template? (Y/N) [Y] "
    $resp = Read-Host
    if ([string]::IsNullOrWhiteSpace($resp) -or $resp.ToUpper() -eq 'Y') {
        Copy-Item -Path $defaultConfig -Destination $workingConfig
        Write-Ok "Created $workingConfig"
    }
}

Write-Host ''
if ($overallOk) {
    Write-Host '=== Environment ready ===' -ForegroundColor Green
    Write-Host 'Next steps:' -ForegroundColor Cyan
    Write-Host '  1. Edit config\my-config.json (or run: python tools\generate_kd_config.py)'
    Write-Host '  2. .\Install-KD.ps1 -DryRun'
    Write-Host '  3. .\Install-KD.ps1 -Mode Install -NonInteractive'
} else {
    Write-Host '=== Environment NOT fully ready - see FAIL items above ===' -ForegroundColor Red
    exit 1
}
