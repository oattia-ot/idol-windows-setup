<#
.SYNOPSIS
    Knowledge Discovery 26.2 Windows Installer - PowerShell UI wrapper.

.DESCRIPTION
    Collects user selections (mode, paths, options) then calls the Python backend.

    PowerShell handles elevation and interactive menus.
    Python performs extract-check, configure, service install/uninstall, and logging.

    Package extraction is automatic - the Python backend calls Unzip-One.bat
    (native tar.exe) for each component as needed. Unzip-One.bat / Unzip-All.bat
    are still available if you want to extract packages manually beforehand.

.PARAMETER Mode
    Operation mode:
      Install    - Extract (if needed), configure, and install services
                   (interactive: asks whether to regenerate SSL certificates)
      Uninstall  - Stop services, remove services, delete component folders
                   (interactive: asks + confirms before deleting toolkit SSL folder)
      Repair     - Uninstall then Install (SSL prompts as for Uninstall + Install)
      Upgrade    - Re-run Install (existing services left in place if present;
                   interactive: asks whether to regenerate SSL certificates)
      Configure  - Re-apply .cfg / license settings only

.PARAMETER ConfigPath
    Path to JSON configuration file.
    Default: config\my-config.json if present, else config\default-config.json

.PARAMETER DryRun
    Show what would happen without changing disk or services.

.PARAMETER NonInteractive
    Skip prompts; use config file and environment variables only.

.PARAMETER Force
    Continue past non-fatal environment validation failures.

.PARAMETER Resume
    Skip stages already recorded as complete in kd-install-state.json.

.PARAMETER ExtractOnly
    Extract (if needed) and verify folders exist; do not configure or install services.

.PARAMETER GenerateConfig
    Run tools\generate_kd_config.py before continuing (Configure flow).

.PARAMETER Help
    Show this usage help and exit. Aliases: -h

.EXAMPLE
    .\Install-KD.ps1 -Help

.EXAMPLE
    .\Install-KD.ps1

.EXAMPLE
    .\Install-KD.ps1 -Mode Install -Force

.EXAMPLE
    .\Install-KD.ps1 -Mode Install -NonInteractive -ConfigPath .\config\my-config.json

.EXAMPLE
    .\Install-KD.ps1 -Mode Uninstall -NonInteractive

.EXAMPLE
    .\Install-KD.ps1 -Mode Configure -GenerateConfig

.NOTES
    Requires PowerShell 5.1+ and Administrator elevation (except -Help).
    Python 3.8+ must be installed (not the Microsoft Store stub).
#>

#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet('Install', 'Uninstall', 'Repair', 'Upgrade', 'Configure', 'InstallJavaServices', 'InstallNiFiService', 'SetNiFiCredentials', 'ManageServices', 'ShowBrowserUrls', 'SetupUIConfig', 'UpdateConfigFiles', 'GenerateSSL')]
    [string]$Mode,

    [string]$ConfigPath,

    [switch]$DryRun,
    [switch]$NonInteractive,
    [switch]$Force,
    [switch]$Resume,
    [switch]$ExtractOnly,
    [switch]$GenerateConfig,

    [Alias('h')]
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
$scriptRoot = $PSScriptRoot

# ---------------------------------------------------------------------------
# Help / usage (works without Administrator)
# ---------------------------------------------------------------------------
function Show-Usage {
    Write-Host ''
    Write-Host 'Knowledge Discovery 26.2 Windows Installer' -ForegroundColor Cyan
    Write-Host 'PowerShell UI  ->  Python backend' -ForegroundColor DarkCyan
    Write-Host ''
    Write-Host 'USAGE' -ForegroundColor Yellow
    Write-Host '  .\Install-KD.ps1 [options]'
    Write-Host '  .\Install-KD.ps1 -Help'
    Write-Host '  .\Install-KD.ps1 -h'
    Write-Host ''
    Write-Host 'OPTIONS' -ForegroundColor Yellow
    Write-Host '  -Mode <name>         Install | Uninstall | Repair | Upgrade | Configure |'
    Write-Host '                       InstallJavaServices | InstallNiFiService | SetNiFiCredentials |'
    Write-Host '                       ManageServices | ShowBrowserUrls | SetupUIConfig | UpdateConfigFiles | GenerateSSL'
    Write-Host '                       (if omitted, an interactive menu is shown)'
    Write-Host '  -ConfigPath <path>   JSON config file (default: config\my-config.json)'
    Write-Host '  -DryRun              Show actions without making changes'
    Write-Host '  -NonInteractive      No prompts; use config / env values only'
    Write-Host '  -Force               Continue past non-fatal validation failures'
    Write-Host '  -Resume              Skip stages already completed (state file)'
    Write-Host '  -ExtractOnly         Extract (if needed) and verify folders; no configure/services'
    Write-Host '  -GenerateConfig      Run Python config generator first'
    Write-Host '  -Help, -h            Show this help and exit'
    Write-Host ''
    Write-Host 'MODES' -ForegroundColor Yellow
    Write-Host '  Install              Configure components and install/start Windows services'
    Write-Host '                       (interactive: asks whether to regenerate SSL certificates)'
    Write-Host '  Uninstall            Stop services -> delete services -> delete folders'
    Write-Host '                       (interactive: asks + confirms before deleting toolkit SSL folder)'
    Write-Host '  Repair               Uninstall then Install (asks about SSL regenerate / SSL delete)'
    Write-Host '  Upgrade              Re-run Install (existing services left alone if present;'
    Write-Host '                       interactive: asks whether to regenerate SSL certificates)'
    Write-Host '  Configure            Re-apply license/host/port settings to existing install'
    Write-Host '  InstallJavaServices  Stop/remove existing KD-NiFi + KD-Find services, force re-extract NiFi and Find'
    Write-Host '                       (overwrites BasePath\NiFi / BasePath\Find), restore staged .nar connectors into'
    Write-Host '                       BasePath\NiFi\extensions, ensure Java 21 + NSSM, register + start both services'
    Write-Host '  InstallNiFiService   NiFi-only equivalent of InstallJavaServices (kept for backward compatibility)'
    Write-Host '  SetNiFiCredentials   Apply NiFi single-user UI username/password (nifi.cmd)'
    Write-Host '  ManageServices       Start/stop/restart/status/delete/create KD-* services'
    Write-Host '  ShowBrowserUrls      Print Admin/Status/Service-Log URLs for all components'
    Write-Host '                       (derived from Ports in the config file)'
    Write-Host '  SetupUIConfig        Prerequisite - setup configurations (menu 00; do this first)'
    Write-Host '                       Runs config\ui-config\Start-KD-Config-Dashboard.bat'
    Write-Host '  UpdateConfigFiles    Apply ports / API key replacements from tools\replacements.json'
    Write-Host '  GenerateSSL          Generate SSL certificates via tools\generate_ssl.py'
    Write-Host '                       (menu 11; also syncs Ports into config\my-config.json)'
    Write-Host ''
    Write-Host 'EXAMPLES' -ForegroundColor Yellow
    Write-Host '  .\Install-KD.ps1'
    Write-Host '  .\Install-KD.ps1 -Mode Install -Force'
    Write-Host '  .\Install-KD.ps1 -Mode Install -NonInteractive -ConfigPath .\config\my-config.json'
    Write-Host '  .\Install-KD.ps1 -Mode Uninstall -NonInteractive'
    Write-Host '  .\Install-KD.ps1 -Mode Configure -GenerateConfig'
    Write-Host '  .\Install-KD.ps1 -Mode InstallJavaServices -NonInteractive'
    Write-Host '  .\Install-KD.ps1 -Mode InstallNiFiService -NonInteractive'
    Write-Host '  .\Install-KD.ps1 -Mode SetNiFiCredentials -NonInteractive'
    Write-Host '  .\Install-KD.ps1 -Mode ManageServices'
    Write-Host '  .\Install-KD.ps1 -Mode ShowBrowserUrls'
    Write-Host '  .\Install-KD.ps1 -Mode SetupUIConfig'
    Write-Host '  .\Install-KD.ps1 -Mode UpdateConfigFiles'
    Write-Host '  .\Install-KD.ps1 -DryRun -Mode Install'
    Write-Host ''
    Write-Host 'EXTRACTION (automatic)' -ForegroundColor Yellow
    Write-Host '  Install-KD.ps1 / install_kd.py call Unzip-One.bat automatically for each'
    Write-Host '  component that is not already extracted. To extract manually instead:'
    Write-Host '    Unzip-All.bat "C:\KD-Setup\zip-folder" "C:\KnowledgeDiscovery\26.2"'
    Write-Host '    Unzip-One.bat "C:\path\to\Content_....zip" "C:\KnowledgeDiscovery\26.2\Content"'
    Write-Host ''
    Write-Host 'PYTHON CLI (optional direct call)' -ForegroundColor Yellow
    Write-Host '  python install_kd.py --help'
    Write-Host '  python install_kd.py --mode Install --force --non-interactive'
    Write-Host ''
    Write-Host 'ENVIRONMENT OVERRIDES' -ForegroundColor Yellow
    Write-Host '  KD_BASEPATH  KD_ZIPPATH  KD_LICENSEHOST  KD_LICENSEKEYPATH  KD_THISHOST'
    Write-Host ''
    Write-Host 'NOTES' -ForegroundColor Yellow
    Write-Host '  - Run from an elevated PowerShell session (Administrator), except -Help'
    Write-Host '  - Requires a real Python 3.8+ install (not the Windows Store stub)'
    Write-Host '  - See README.md and INSTALL.md for full documentation'
    Write-Host ''
}

# Accept -? via remaining args style if someone passes it positionally is limited;
# also support common --help from users coming from Python habits.
if ($Help -or ($args -contains '--help') -or ($args -contains '-?')) {
    Show-Usage
    exit 0
}

# ---------------------------------------------------------------------------
# Administrator check (after help, so -Help works without elevation)
# ---------------------------------------------------------------------------
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'ERROR: This script must be run as Administrator (except -Help).' -ForegroundColor Red
    Write-Host 'Right-click PowerShell -> Run as administrator, then re-run.' -ForegroundColor Yellow
    Write-Host 'For usage:  .\Install-KD.ps1 -Help' -ForegroundColor DarkGray
    exit 1
}

# ---------------------------------------------------------------------------
# Helpers (PowerShell 5.1 compatible - no ?. null-conditional operator)
# ---------------------------------------------------------------------------
function Write-Title {
    param([string]$Text)
    Write-Host $Text -ForegroundColor Cyan
}
function Write-Ok {
    param([string]$Text)
    Write-Host "  [OK] $Text" -ForegroundColor Green
}
function Write-Warn2 {
    param([string]$Text)
    Write-Host "  [WARN] $Text" -ForegroundColor Yellow
}
function Write-Fail {
    param([string]$Text)
    Write-Host "  [FAIL] $Text" -ForegroundColor Red
}

function Test-PythonWorks {
    param([Parameter(Mandatory)][string]$ExePath)
    # Skip the Microsoft Store app-execution-alias stub
    if ($ExePath -match '(?i)[\\/]WindowsApps[\\/]') {
        return $false
    }
    if (-not (Test-Path -LiteralPath $ExePath)) {
        return $false
    }
    try {
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $out = & $ExePath -c "import sys; print(sys.version_info[0])" 2>$null
        $ErrorActionPreference = $prevEap
        if ($LASTEXITCODE -ne 0) { return $false }
        if ($out -match '^[23]$') { return $true }
        return $false
    }
    catch {
        return $false
    }
}

function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[string]

    foreach ($name in @('python', 'python3', 'py')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -ne $cmd -and $cmd.Source) {
            $candidates.Add($cmd.Source) | Out-Null
        }
    }

    $extra = @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python39\python.exe",
        "$env:ProgramFiles\Python313\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "$env:ProgramFiles\Python311\python.exe",
        "$env:ProgramFiles\Python310\python.exe",
        "C:\Python313\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe",
        "C:\Python310\python.exe"
    )
    foreach ($p in $extra) {
        if ($p -and (Test-Path -LiteralPath $p)) {
            $candidates.Add($p) | Out-Null
        }
    }

    foreach ($c in $candidates) {
        if (Test-PythonWorks -ExePath $c) {
            return $c
        }
    }
    return $null
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
Clear-Host
Write-Host ''
Write-Host '  ========================================================' -ForegroundColor Cyan
Write-Host '   Knowledge Discovery 26.2  -  Windows Installer' -ForegroundColor Cyan
Write-Host '   PowerShell UI  ->  Python backend' -ForegroundColor DarkCyan
Write-Host '  ========================================================' -ForegroundColor Cyan
Write-Host ''

# ---------------------------------------------------------------------------
# 1. Locate Python
# ---------------------------------------------------------------------------
Write-Title '[1/4] Locating Python...'
$python = Find-Python
if (-not $python) {
    Write-Fail 'Python 3.8+ not found (or only the Microsoft Store stub was found).'
    Write-Host ''
    Write-Host 'The Windows "App execution aliases" python.exe is NOT a real install.' -ForegroundColor Yellow
    Write-Host 'Install a real Python, then re-run this script:' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '  Option A (recommended):' -ForegroundColor Cyan
    Write-Host '    winget install Python.Python.3.12' -ForegroundColor White
    Write-Host ''
    Write-Host '  Option B:' -ForegroundColor Cyan
    Write-Host '    Download from https://www.python.org/downloads/' -ForegroundColor White
    Write-Host '    IMPORTANT: check "Add python.exe to PATH" during setup' -ForegroundColor White
    Write-Host ''
    Write-Host '  After install, close and reopen this elevated PowerShell window.' -ForegroundColor DarkGray
    Write-Host ''
    Write-Host '  Optional: disable the Store stub under' -ForegroundColor DarkGray
    Write-Host '    Settings > Apps > Advanced app settings > App execution aliases' -ForegroundColor DarkGray
    Write-Host '    and turn OFF the python.exe / python3.exe toggles.' -ForegroundColor DarkGray
    Write-Host ''
    Write-Host '  For script options:  .\Install-KD.ps1 -Help' -ForegroundColor DarkGray
    exit 1
}
Write-Ok "Found: $python"
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$pyVer = & $python -c "import sys; print('{0}.{1}'.format(sys.version_info.major, sys.version_info.minor))" 2>$null
$ErrorActionPreference = $prevEap
Write-Host "         Version: $pyVer" -ForegroundColor DarkGray

# ---------------------------------------------------------------------------
# 2. Optional: ensure rich is available (nice console)
# ---------------------------------------------------------------------------
Write-Title '[2/4] Checking optional Python packages...'
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$hasRich = & $python -c "import rich; print('yes')" 2>$null
$ErrorActionPreference = $prevEap
if ($hasRich -ne 'yes') {
    Write-Warn2 'Package "rich" not installed - console will be plain. Installing...'
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $python -m pip install --quiet rich colorama 2>$null
    $pipExit = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    if ($pipExit -eq 0) {
        Write-Ok 'rich + colorama installed'
    }
    else {
        Write-Warn2 'Could not install rich (offline?). Continuing with plain output.'
    }
}
else {
    Write-Ok 'rich is available'
}

# ---------------------------------------------------------------------------
# 3. Interactive mode selection (if Mode not supplied)
# ---------------------------------------------------------------------------
if (-not $Mode) {
    Write-Title '[3/4] Select operation mode'
    Write-Host ''
    Write-Host '  Required for a new deployment:' -ForegroundColor DarkYellow
    Write-Host '  00) Prerequisite - setup configurations - open web UI to edit Ports/Components/BrowserUrls (do this first)' -ForegroundColor Red
    Write-Host '  01) Install                 - extract check, configure, register and start services (asks to regenerate SSL)' -ForegroundColor Red
    Write-Host '                              BasePath uses major.minor from ZIP names (e.g. Content_26.3.1_WINDOWS -> C:\KnowledgeDiscovery\26.3)' -ForegroundColor DarkGray
    Write-Host ''
    Write-Host '  Other modes:' -ForegroundColor Cyan
    Write-Host '  02) Configure               - re-apply config to an existing install' -ForegroundColor Blue
    Write-Host '  03) Upgrade                 - re-run install (existing services left alone; asks to regenerate SSL)' -ForegroundColor Blue
    Write-Host '  04) Repair                  - uninstall then reinstall (asks to regenerate SSL)' -ForegroundColor Blue
    Write-Host '  05) Uninstall               - stop services, delete services, delete folders (asks before deleting SSL)' -ForegroundColor Blue
    Write-Host '  06) Extract-Only            - extract (if needed) and verify folders (no config / services)' -ForegroundColor Blue
    Write-Host '  07) Install Java Services (NiFi|Find) - stop/remove existing services, force re-extract (overwrite), register + start' -ForegroundColor Blue
    Write-Host '  08) Set NiFi UI credentials - apply username/password via nifi.cmd (NiFi already extracted)' -ForegroundColor Blue
    Write-Host '  09) Manage Windows services - start/stop/restart/status/delete/create' -ForegroundColor Blue
    Write-Host '  10) Generate Browser URLs   - print Admin/Status/Log URLs for all components from config Ports' -ForegroundColor Blue
    Write-Host '  11) Update config files     - apply ports / API key from tools\replacements.json (syncs my-config.json)' -ForegroundColor Blue
    Write-Host '  12) Generate SSL certificates - run tools\generate_ssl.py (interactive SSL CA + service certs)' -ForegroundColor Blue
    Write-Host '   0) Exit' -ForegroundColor Red
    Write-Host '   ?) Help' -ForegroundColor Yellow
    Write-Host ''
    Write-Host -ForegroundColor Yellow -NoNewline "Enter choice [00] "
    $choice = Read-Host
    if ([string]::IsNullOrWhiteSpace($choice)) { $choice = '00' }
    # Exit must be checked before zero-padding (otherwise "0" becomes "00")
    if ($choice -eq '0') { exit 0 }
    # Accept both "1" and "01" style input for 1-9
    if ($choice -match '^[1-9]$') { $choice = '0' + $choice }

    switch ($choice) {
        '00' { $Mode = 'SetupUIConfig' }
        '01' { $Mode = 'Install' }
        '02' { $Mode = 'Configure' }
        '03' { $Mode = 'Upgrade' }
        '04' { $Mode = 'Repair' }
        '05' { $Mode = 'Uninstall' }
        '06' { $Mode = 'Install'; $ExtractOnly = $true }
        '07' { $Mode = 'InstallJavaServices' }
        '08' { $Mode = 'SetNiFiCredentials' }
        '09' { $Mode = 'ManageServices' }
        '10' { $Mode = 'ShowBrowserUrls' }
        '11' { $Mode = 'UpdateConfigFiles' }
        '12' { $Mode = 'GenerateSSL' }
        { $_ -in @('?', 'h', 'H', 'help', 'Help') } {
            Show-Usage
            exit 0
        }
        default {
            Write-Fail "Invalid choice: $choice"
            Write-Host 'Run .\Install-KD.ps1 -Help for options.' -ForegroundColor DarkGray
            exit 1
        }
    }
    $modeLabel = $Mode
    if ($ExtractOnly) { $modeLabel = "$Mode (ExtractOnly)" }
    Write-Ok "Mode = $modeLabel"
}
else {
    Write-Title "[3/4] Mode already set: $Mode"
}

# Optional config generation
if ($GenerateConfig -or ($Mode -eq 'Configure' -and -not $NonInteractive)) {
    $gen = Join-Path $scriptRoot 'tools\generate_kd_config.py'
    if (Test-Path -LiteralPath $gen) {
        $runGen = $false
        if ($GenerateConfig) {
            $runGen = $true
        }
        else {
            Write-Host -ForegroundColor Yellow -NoNewline "Generate / edit config with the Python generator first? (Y/N) [N] "
            $a = Read-Host
            if ($a -eq 'Y' -or $a -eq 'y') { $runGen = $true }
        }
        if ($runGen) {
            Write-Host 'Launching config generator...' -ForegroundColor Yellow
            & $python $gen
        }
    }
}

# Resolve config path
if (-not $ConfigPath) {
    $my = Join-Path $scriptRoot 'config\my-config.json'
    $def = Join-Path $scriptRoot 'config\default-config.json'
    if (Test-Path -LiteralPath $my) {
        $ConfigPath = $my
    }
    elseif (Test-Path -LiteralPath $def) {
        $ConfigPath = $def
    }
    else {
        Write-Fail 'No configuration file found.'
        exit 1
    }
}
Write-Host "Using configuration: $ConfigPath" -ForegroundColor DarkCyan

# ---------------------------------------------------------------------------
# 3a. SetupUIConfig: pre-install web UI for Ports / Components / BrowserUrls
# ---------------------------------------------------------------------------
# Recommended before menu 01 (Install). Launches the local dashboard server
# which writes Export JSON to config\my-config.json.
if ($Mode -eq 'SetupUIConfig') {
    Write-Title '00) Prerequisite - setup configurations'
    Write-Host ''
    Write-Host '  This opens the KD Configuration Dashboard in your browser.' -ForegroundColor Cyan
    Write-Host '  1. Edit Ports, Components, and path endpoints as needed.' -ForegroundColor DarkGray
    Write-Host '  2. Click Export JSON to save config\my-config.json.' -ForegroundColor DarkGray
    Write-Host '  3. Close the server window (Ctrl+C), then run menu 01 Install.' -ForegroundColor DarkGray
    Write-Host ''

    $uiBat = Join-Path $scriptRoot 'config\ui-config\Start-KD-Config-Dashboard.bat'
    if (-not (Test-Path -LiteralPath $uiBat)) {
        Write-Fail "UI config launcher not found: $uiBat"
        Write-Host 'Expected files under config\ui-config\ (Start-KD-Config-Dashboard.bat, kd-config-server.py, kd-config-dashboard.html)' -ForegroundColor DarkGray
        exit 1
    }

    # Ensure export lands in this setup's config\my-config.json
    $env:KD_SETUP_ROOT = $scriptRoot

    # Open Windows Firewall so external clients can reach the dashboard (port 5000)
    $dashPort = 5000
    $tcpName = "Temp-KD-$dashPort"
    $icmpName = "Temp-ICMP"
    Write-Host "  Opening Windows Firewall for dashboard port $dashPort (DisplayName=$tcpName)..." -ForegroundColor DarkCyan
    try {
        if (-not (Get-NetFirewallRule -DisplayName $tcpName -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName $tcpName -Direction Inbound `
                -Protocol TCP -LocalPort $dashPort -Action Allow -Profile Any | Out-Null
            Write-Host "  Firewall rule created: $tcpName (TCP $dashPort inbound Allow)" -ForegroundColor Green
        } else {
            Write-Host "  Firewall rule already present: $tcpName (TCP $dashPort)" -ForegroundColor DarkGray
        }
        if (-not (Get-NetFirewallRule -DisplayName $icmpName -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName $icmpName -Direction Inbound `
                -Protocol ICMPv4 -Action Allow -Profile Any | Out-Null
            Write-Host "  Firewall rule created: $icmpName (ICMPv4 inbound Allow)" -ForegroundColor Green
        } else {
            Write-Host "  Firewall rule already present: $icmpName" -ForegroundColor DarkGray
        }
    } catch {
        Write-Warn2 "Could not open firewall for port $dashPort : $($_.Exception.Message)"
    }

    # Hint public IP for external clients (best-effort)
    try {
        $pubIp = Invoke-RestMethod -Uri "http://ifconfig.me/ip" -TimeoutSec 5 -ErrorAction SilentlyContinue
        if ($pubIp) {
            Write-Host "  Public IP (for external clients): $pubIp" -ForegroundColor Cyan
            Write-Host "  External URL: http://${pubIp}:5000/kd-config-dashboard.html" -ForegroundColor Cyan
            Write-Host "  From a client: Test-NetConnection $pubIp -Port 5000" -ForegroundColor DarkGray
        }
    } catch {
        # offline / blocked - ignore
    }

    Write-Host "  Launching: $uiBat" -ForegroundColor DarkCyan
    Write-Host "  Setup root: $scriptRoot" -ForegroundColor DarkCyan
    Write-Host "  Export to : $(Join-Path $scriptRoot 'config\my-config.json')" -ForegroundColor DarkCyan
    Write-Host "  Local URL : http://127.0.0.1:5000/kd-config-dashboard.html" -ForegroundColor DarkCyan
    Write-Host ''

    # Run in a new console so the user can stop the server with Ctrl+C there
    # while still seeing this parent script's guidance when it returns.
    # Use cmd /c so the batch exit code is returned reliably.
    $p = Start-Process -FilePath 'cmd.exe' `
        -ArgumentList @('/c', "`"$uiBat`"") `
        -WorkingDirectory (Split-Path -Parent $uiBat) `
        -PassThru -Wait
    $code = if ($null -ne $p -and $null -ne $p.ExitCode) { [int]$p.ExitCode } else { 0 }

    Write-Host ''
    # Exit 0  = clean stop (Ctrl+C handled by Python / Exit button)
    # Exit 255 / -1 = console window closed with X (Windows terminates the
    #                 process without a normal exit) — treat as success.
    # Other non-zero = real failure (missing Python, bind error, etc.)
    $normalClose = ($code -eq 0 -or $code -eq 255 -or $code -eq -1)
    if ($normalClose) {
        Write-Ok 'UI Config dashboard closed.'
        if ($code -ne 0) {
            Write-Host "  (console closed; process exit code $code is normal)" -ForegroundColor DarkGray
        }
    }
    else {
        Write-Warn2 "UI Config dashboard exited with code $code"
        Write-Host '  Check the dashboard window for Python / bind errors.' -ForegroundColor DarkGray
    }
    Write-Host ''
    Write-Host '  Next: run .\Install-KD.ps1 again and choose 01) Install' -ForegroundColor Yellow
    Write-Host '        (or: .\Install-KD.ps1 -Mode Install)' -ForegroundColor DarkGray
    Write-Host ''
    # Always exit 0 after a normal close so the parent menu is not treated as failed
    exit $(if ($normalClose) { 0 } else { $code })
}

# ---------------------------------------------------------------------------
# 3a2. UpdateConfigFiles: apply ports / API key from tools/replacements.json
# ---------------------------------------------------------------------------
# Menu option 11. PowerShell-native (no Lua required). Reads tools/replacements.json,
# updates listed config/cfg files and the API key placeholders, then syncs target
# ports into config/my-config.json (and default-config.json).
if ($Mode -eq 'UpdateConfigFiles') {
    Write-Title '11) Update configuration files (ports, API key, etc.)'
    Write-Host ''
    Write-Host '  Applies replacements from tools\replacements.json' -ForegroundColor Cyan
    Write-Host '  - YOUR_AI_LLM_PROVIDER_KEY  ->  real API key (edit the JSON first)' -ForegroundColor DarkGray
    Write-Host '  - Port lines in config\cfg\**\*.cfg' -ForegroundColor DarkGray
    Write-Host '  - Syncs target Ports into config\my-config.json' -ForegroundColor DarkGray
    Write-Host ''

    $updater = Join-Path $scriptRoot 'tools\Update-ConfigFiles.ps1'
    if (-not (Test-Path -LiteralPath $updater)) {
        Write-Fail "Updater not found: $updater"
        Write-Host 'Expected tools\Update-ConfigFiles.ps1 and tools\replacements.json' -ForegroundColor DarkGray
        exit 1
    }

    $jsonPath = Join-Path $scriptRoot 'tools\replacements.json'
    if (-not (Test-Path -LiteralPath $jsonPath)) {
        Write-Fail "Replacements JSON not found: $jsonPath"
        exit 1
    }

    if (-not $NonInteractive) {
        Write-Host '  Edit tools\replacements.json before continuing if you need different' -ForegroundColor Yellow
        Write-Host '  API key or port values. Current targets are applied as-is.' -ForegroundColor Yellow
        Write-Host ''
        Write-Host -ForegroundColor Yellow -NoNewline "  Proceed with update? (Y/N) [Y] "
        $a = Read-Host
        if ($a -eq 'N' -or $a -eq 'n') {
            Write-Host '  Cancelled.' -ForegroundColor DarkGray
            exit 0
        }
    }

    $argsList = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', $updater,
        '-SetupRoot', $scriptRoot,
        '-ReplacementsPath', $jsonPath
    )
    if ($DryRun) { $argsList += '-DryRun' }

    Write-Host "  Running: tools\Update-ConfigFiles.ps1" -ForegroundColor DarkCyan
    Write-Host ''
    $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $argsList -WorkingDirectory $scriptRoot -NoNewWindow -PassThru -Wait
    $code = if ($null -ne $p) { $p.ExitCode } else { 1 }

    Write-Host ''
    if ($code -eq 0) {
        Write-Ok 'Configuration files updated.'
        Write-Host '  Tip: re-open menu 00 (SetupUIConfig) or 10 (Browser URLs) to verify ports.' -ForegroundColor DarkGray
    }
    else {
        Write-Fail "Update-ConfigFiles exited with code $code"
    }
    exit $(if ($null -eq $code) { 1 } else { $code })
}

# ---------------------------------------------------------------------------
# 3a3. GenerateSSL: run tools\generate_ssl.py
# ---------------------------------------------------------------------------
# Menu option 12. Launches the preferred Python SSL generator (cryptography-based).
# Passes through any remaining args; interactive by default.
if ($Mode -eq 'GenerateSSL') {
    Write-Title '12) Generate SSL certificates'
    Write-Host ''
    Write-Host '  Runs: python tools\generate_ssl.py' -ForegroundColor Cyan
    Write-Host '  Preferred on Windows (no OpenSSL CA index.txt issues).' -ForegroundColor DarkGray
    Write-Host '  Tip: Generate-SSL.bat is an equivalent double-click launcher.' -ForegroundColor DarkGray
    Write-Host ''

    $sslScript = Join-Path $scriptRoot 'tools\generate_ssl.py'
    if (-not (Test-Path -LiteralPath $sslScript)) {
        Write-Fail "SSL generator not found: $sslScript"
        exit 1
    }

    # Locate python
    $py = $null
    foreach ($c in @('python', 'py')) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { $py = $cmd.Source; break }
    }
    if (-not $py) {
        Write-Fail 'Python not found in PATH. Install Python 3.8+ and re-run.'
        Write-Host '  winget install Python.Python.3.12' -ForegroundColor DarkGray
        exit 1
    }

    # Ensure cryptography
    & $py -c "import cryptography" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host '  Installing cryptography...' -ForegroundColor Yellow
        & $py -m pip install --quiet "cryptography>=42.0.0"
        if ($LASTEXITCODE -ne 0) {
            Write-Fail 'Could not install cryptography. Run: pip install cryptography'
            exit 1
        }
    }

    Write-Host "  Running: $py tools\generate_ssl.py" -ForegroundColor DarkCyan
    Write-Host ''
    Push-Location $scriptRoot
    try {
        & $py $sslScript @args
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    Write-Host ''
    if ($code -eq 0) {
        Write-Ok 'SSL certificate generation finished.'
    } else {
        Write-Fail "generate_ssl.py exited with code $code"
    }
    exit $(if ($null -eq $code) { 1 } else { $code })
}

# ---------------------------------------------------------------------------
# 3b. ManageServices: start / stop all deployed KD-* services (no Python install path)
# ---------------------------------------------------------------------------
# IMPORTANT: call manage_kd_services.py directly (not via Manage-KDServices.ps1
# array splat). PowerShell 5.1 treats strings like '-AllDeployed' in an array
# splat as *positional* values when the callee is another .ps1, which produces:
#   A positional parameter cannot be found that accepts argument '-AllDeployed'
# and attributes the error to Install-KD.ps1. Hashtable splat or a fresh
# powershell.exe -File process would also work; Python is the same backend the
# .bat wrappers and Manage-KDServices.ps1 ultimately run, so go straight there.
if ($Mode -eq 'ManageServices') {
    Write-Title 'Manage Windows services'
    Write-Host ''
    Write-Host '  Operate on every deployed KD-* Windows service on this machine.'
    Write-Host ''

    $svcAction = $null
    if ($NonInteractive) {
        # Non-interactive ManageServices defaults to status (safe). Use the
        # dedicated Start/Stop bat files or Manage-KDServices.ps1 for automation.
        $svcAction = 'status'
        Write-Host '  NonInteractive: showing status of all deployed KD-* services.' -ForegroundColor DarkCyan
    } else {
        Write-Host '  1) Status only'
        Write-Host '  2) Start all'
        Write-Host '  3) Stop all'
        Write-Host '  4) Restart all'
        Write-Host '  5) Delete all services   (stop + remove service registration; files kept)'
        Write-Host '  6) Create all services   (native -install; NiFi via NSSM - from config Components)'
        Write-Host '  0) Cancel'
        Write-Host ''
        Write-Host -ForegroundColor Yellow -NoNewline "Enter choice [1] "
        $svcChoice = Read-Host
        if ([string]::IsNullOrWhiteSpace($svcChoice)) { $svcChoice = '1' }

        switch ($svcChoice) {
            '1' { $svcAction = 'status' }
            '2' { $svcAction = 'start' }
            '3' { $svcAction = 'stop' }
            '4' { $svcAction = 'restart' }
            '5' { $svcAction = 'delete' }
            '6' { $svcAction = 'create' }
            '0' {
                Write-Host 'Cancelled.' -ForegroundColor Yellow
                exit 0
            }
            default {
                Write-Fail "Invalid choice: $svcChoice"
                exit 1
            }
        }
    }

    $pyManage = Join-Path $scriptRoot 'manage_kd_services.py'
    if (-not (Test-Path -LiteralPath $pyManage)) {
        Write-Fail "Missing $pyManage"
        exit 1
    }

    # $python was resolved in step [1/4] above
    # delete / start / stop / restart / status: operate on every deployed KD-* service
    # create: register from config Components via NSSM (folders must already exist)
    $useAllDeployed = $svcAction -ne 'create'
    $actionLabel = $svcAction
    if ($useAllDeployed) {
        Write-Host ""
        Write-Host "Running: manage_kd_services.py $svcAction --all-deployed --non-interactive" -ForegroundColor Cyan
        Write-Host ""
    } else {
        Write-Host ""
        Write-Host "Running: manage_kd_services.py create --non-interactive  (native -install / NiFi NSSM; config Components)" -ForegroundColor Cyan
        Write-Host ""
    }

    $pyArgs = @(
        $pyManage,
        $svcAction,
        '--non-interactive'
    )
    if ($useAllDeployed) { $pyArgs += '--all-deployed' }
    if ($DryRun) { $pyArgs += '--dry-run' }
    if ($ConfigPath) { $pyArgs += @('--config', $ConfigPath) }

    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUNBUFFERED = '1'
    & $python @pyArgs
    exit $LASTEXITCODE
}

# ---------------------------------------------------------------------------
# 3c. SetNiFiCredentials: optional interactive username/password prompt
# ---------------------------------------------------------------------------
$script:NifiUsernameOverride = $null
$script:NifiPasswordOverride = $null

if ($Mode -eq 'SetNiFiCredentials' -and -not $NonInteractive) {
    Write-Title 'NiFi UI credentials'
    Write-Host ''
    Write-Host '  Applies nifi.cmd set-single-user-credentials on BasePath\NiFi.'
    Write-Host '  Defaults come from config NiFi.Username / NiFi.Password when present.'
    Write-Host ''

    # Try to read defaults from config JSON (best-effort, no strict parse)
    $cfgUser = 'admin'
    $cfgPass = ''
    try {
        if ($ConfigPath -and (Test-Path -LiteralPath $ConfigPath)) {
            $raw = Get-Content -LiteralPath $ConfigPath -Raw -ErrorAction Stop
            if ($raw -match '"Username"\s*:\s*"([^"]*)"') { $cfgUser = $Matches[1] }
            if ($raw -match '"Password"\s*:\s*"([^"]*)"') { $cfgPass = $Matches[1] }
        }
    } catch {}

    Write-Host -ForegroundColor Yellow -NoNewline "  NiFi UI username [$cfgUser] "
    $uIn = Read-Host
    if ([string]::IsNullOrWhiteSpace($uIn)) { $uIn = $cfgUser }
    $script:NifiUsernameOverride = $uIn

    $passPrompt = if ($cfgPass) { '******** (config default; Enter to keep)' } else { '(required)' }
    Write-Host -ForegroundColor Yellow -NoNewline "  NiFi UI password $passPrompt "
    $pIn = Read-Host
    if ([string]::IsNullOrWhiteSpace($pIn)) {
        if ($cfgPass) {
            $script:NifiPasswordOverride = $cfgPass
        } else {
            Write-Fail 'Password is required.'
            exit 1
        }
    } else {
        $script:NifiPasswordOverride = $pIn
    }
    Write-Ok "Will set credentials for user '$($script:NifiUsernameOverride)'"
}

# ---------------------------------------------------------------------------
# 4. Build argument list and call Python
# ---------------------------------------------------------------------------
Write-Title '[4/4] Launching Python backend...'
Write-Host ''

$pyArgs = @(
    (Join-Path $scriptRoot 'install_kd.py'),
    '--mode', $Mode,
    '--config', $ConfigPath
)
if ($DryRun)         { $pyArgs += '--dry-run' }
if ($NonInteractive) { $pyArgs += '--non-interactive' }
if ($Force)          { $pyArgs += '--force' }
if ($Resume)         { $pyArgs += '--resume' }
if ($ExtractOnly)    { $pyArgs += '--extract-only' }
if ($script:NifiUsernameOverride) {
    $pyArgs += @('--nifi-username', $script:NifiUsernameOverride)
}
if ($script:NifiPasswordOverride) {
    $pyArgs += @('--nifi-password', $script:NifiPasswordOverride)
}

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'

& $python @pyArgs
$exitCode = $LASTEXITCODE

Write-Host ''
if ($exitCode -eq 0) {
    Write-Host '=== PowerShell wrapper finished successfully ===' -ForegroundColor Green
}
else {
    Write-Host "=== PowerShell wrapper finished with exit code $exitCode ===" -ForegroundColor Red
    Write-Host 'For options:  .\Install-KD.ps1 -Help' -ForegroundColor DarkGray
}
exit $exitCode
