#Requires -Version 5.1
<#
.SYNOPSIS
    Register Apache NiFi as Windows service KD-NiFi via NSSM.

.DESCRIPTION
    Preferred helper used by the KD installer (and operators manually).

    - Service name defaults to KD-NiFi (DisplayName: OpenText KD Apache NiFi)
    - Application: %COMSPEC% with AppParameters "/c nifi.cmd run"
      (AppDirectory = NIFI_HOME\bin — matches OpenText NSSM layout)
    - Removes legacy service names KD-Apache-NiFi / ApacheNifiService
    - Sets AppEnvironmentExtra JAVA_HOME + NIFI_HOME when available
    - Rotated stdout/stderr under NiFi\logs\nssm-*.log

.PARAMETER NifiHome
    Path to the extracted NiFi tree (folder that contains bin\nifi.cmd).

.PARAMETER ServiceName
    Windows service name. Default: KD-NiFi

.PARAMETER NssmPath
    Full path to nssm.exe. If omitted, searched on PATH / common locations /
    toolkit nifi\nssm.exe, then installed via winget when possible.

.PARAMETER NonInteractive
    No prompts. Required by the KD installer.

.PARAMETER Start
    Start the service after registration.

.PARAMETER StartMode
    Auto (default) or Manual / Demand.

.EXAMPLE
    .\Setup-NiFi-Service.ps1 -NifiHome "C:\KnowledgeDiscovery\26.2\NiFi" -NonInteractive
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$NifiHome = "C:\KnowledgeDiscovery\26.2\NiFi",

    [Parameter(Mandatory = $false)]
    [string]$ServiceName = "KD-NiFi",

    [Parameter(Mandatory = $false)]
    [string]$NssmPath,

    [Parameter(Mandatory = $false)]
    [string]$Port = "8443",

    [switch]$NonInteractive,

    [switch]$Start,

    [ValidateSet("Auto", "Automatic", "Manual", "Demand")]
    [string]$StartMode = "Auto"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Info {
    param([string]$Message)
    Write-Host "[INFO]  $Message" -ForegroundColor Green
}
function Write-WarnMsg {
    param([string]$Message)
    Write-Host "[WARN]  $Message" -ForegroundColor Yellow
}
function Write-ErrMsg {
    param([string]$Message)
    Write-Host "[ERROR] $Message" -ForegroundColor Red
}

$LegacyNames = @("KD-Apache-NiFi", "ApacheNifiService")

# --- Resolve NiFi home / bin ---
$NifiHome = $NifiHome.TrimEnd('\', '/')
if (-not (Test-Path -LiteralPath $NifiHome)) {
    Write-ErrMsg "NifiHome not found: $NifiHome"
    exit 1
}
$BinDir  = Join-Path $NifiHome "bin"
$LogsDir = Join-Path $NifiHome "logs"
$NifiCmd = Join-Path $BinDir "nifi.cmd"

if (-not (Test-Path -LiteralPath $NifiCmd)) {
    Write-ErrMsg "nifi.cmd not found under $BinDir"
    exit 1
}
if (-not (Test-Path -LiteralPath $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
}

# AppDirectory is bin/, so relative nifi.cmd (matches NSSM editor: /c nifi.cmd run)
$AppParameters = "/c nifi.cmd run"
Write-Info "Launcher: nifi.cmd run (AppDirectory=$BinDir)"

$ComSpec = if ($env:COMSPEC) { $env:COMSPEC } else { "$env:SystemRoot\System32\cmd.exe" }
$DisplayName = "OpenText KD Apache NiFi"
$Description = "Apache NiFi Data Flow - OpenText Knowledge Discovery (NSSM)"
if ($StartMode -match '^(Auto|Automatic)$') {
    $StartType = "SERVICE_AUTO_START"
} else {
    $StartType = "SERVICE_DEMAND_START"
}

# --- Locate / install NSSM ---
function Find-Nssm {
    param([string]$Explicit)
    if ($Explicit -and (Test-Path -LiteralPath $Explicit)) {
        return (Resolve-Path -LiteralPath $Explicit).Path
    }
    $cmd = Get-Command nssm -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { return $cmd.Source }

    $scriptRoot = $PSScriptRoot
    if (-not $scriptRoot) {
        $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
    }
    $candidates = @(
        (Join-Path $scriptRoot "nssm.exe"),
        (Join-Path $scriptRoot "nssm\nssm.exe"),
        "${env:ProgramFiles}\nssm\nssm.exe",
        "${env:ProgramFiles(x86)}\nssm\nssm.exe",
        "$env:ProgramData\chocolatey\bin\nssm.exe"
    )
    foreach ($root in @(
        "$env:LOCALAPPDATA\Microsoft\WinGet\Packages",
        "$env:ProgramFiles\WindowsApps"
    )) {
        if (Test-Path $root) {
            $hit = Get-ChildItem -Path $root -Filter "nssm.exe" -Recurse -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($hit) { $candidates += $hit.FullName }
        }
    }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return $c }
    }
    return $null
}

function Install-NssmViaWinget {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return $false }
    Write-Info "Installing NSSM via winget (NSSM.NSSM)..."
    try {
        $p = Start-Process -FilePath "winget" -ArgumentList @(
            "install", "--id", "NSSM.NSSM", "-e",
            "--silent", "--accept-package-agreements", "--accept-source-agreements"
        ) -NoNewWindow -PassThru -Wait
        return ($p.ExitCode -eq 0)
    } catch {
        Write-WarnMsg "winget install failed: $($_.Exception.Message)"
        return $false
    }
}

$nssm = Find-Nssm -Explicit $NssmPath
if (-not $nssm) {
    if (Install-NssmViaWinget) {
        Start-Sleep -Seconds 2
        $nssm = Find-Nssm -Explicit $NssmPath
    }
}
if (-not $nssm) {
    Write-ErrMsg "nssm.exe not found. Install with: winget install --id NSSM.NSSM -e"
    Write-ErrMsg "Or place nssm.exe next to this script under nifi\"
    exit 1
}
Write-Info "Using NSSM: $nssm"
try {
    & $nssm version | Out-Host
} catch {
    # ignore version print failures
}

# --- Stop / remove existing services (current + legacy) ---
function Test-ServicePresent {
    param([string]$Name)
    & sc.exe query $Name 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Remove-ServiceIfPresent {
    param([string]$Name)

    if (-not (Test-ServicePresent -Name $Name)) {
        Write-Info "Service $Name not present - nothing to remove."
        return $true
    }

    Write-Info "Stopping/removing existing service $Name ..."

    # Best-effort stop. Suppress NSSM/PowerShell native stderr here because a
    # missing/already-stopped service is not an installation error.
    & $nssm stop $Name 2>$null | Out-Null
    & sc.exe stop $Name 2>$null | Out-Null

    # Give SCM time to transition from STOP_PENDING to STOPPED.
    for ($i = 0; $i -lt 10; $i++) {
        $query = (& sc.exe query $Name 2>$null | Out-String)
        if ($query -notmatch 'STOP_PENDING') { break }
        Start-Sleep -Milliseconds 500
    }

    # Prefer NSSM removal, but do not emit its native exception/error stream.
    $nssmOutput = & $nssm remove $Name confirm 2>&1 | Out-String
    $nssmExit = $LASTEXITCODE

    if ($nssmExit -eq 0 -and -not (Test-ServicePresent -Name $Name)) {
        Write-Info "[OK] $Name removed via NSSM."
        return $true
    }

    # NSSM can fail when SCM has a stale/partially deleted service. Use the
    # Windows Service Control Manager directly as the reliable fallback.
    Write-Info "NSSM could not remove $Name cleanly; using Windows service deletion."
    & sc.exe delete $Name 2>$null | Out-Null

    # A service can remain visible briefly after 'sc delete' (or be marked for
    # deletion until its last handle closes). Wait for SCM to report it gone.
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Test-ServicePresent -Name $Name)) {
            Write-Info "[OK] $Name removed."
            return $true
        }
        Start-Sleep -Milliseconds 500
    }

    Write-WarnMsg "$Name still exists after removal attempt; Windows may have it marked for deletion. A reboot may be required before reinstalling this service."
    return $false
}

$allNames = @($ServiceName) + $LegacyNames
foreach ($name in $allNames) {
    Remove-ServiceIfPresent -Name $name
}

# --- Install + configure ---
Write-Info "Installing service $ServiceName ..."
& $nssm install $ServiceName $ComSpec
if ($LASTEXITCODE -ne 0) {
    Write-WarnMsg "nssm install exit $LASTEXITCODE (continuing if service exists)"
}

& $nssm set $ServiceName Application $ComSpec | Out-Null
& $nssm set $ServiceName AppDirectory $BinDir | Out-Null
& $nssm set $ServiceName AppParameters $AppParameters | Out-Null
& $nssm set $ServiceName DisplayName $DisplayName | Out-Null
& $nssm set $ServiceName Description $Description | Out-Null
& $nssm set $ServiceName Start $StartType | Out-Null
& $nssm set $ServiceName AppStdout (Join-Path $LogsDir "nssm-stdout.log") | Out-Null
& $nssm set $ServiceName AppStderr (Join-Path $LogsDir "nssm-stderr.log") | Out-Null
& $nssm set $ServiceName AppRotateFiles "1" | Out-Null
& $nssm set $ServiceName AppRotateBytes "10485760" | Out-Null
& $nssm set $ServiceName AppStopMethodSkip "0" | Out-Null
& $nssm set $ServiceName AppStopMethodConsole "20000" | Out-Null
& $nssm set $ServiceName AppKillProcessTree "1" | Out-Null
& $nssm set $ServiceName AppRestartDelay "10000" | Out-Null
& $nssm set $ServiceName ObjectName "LocalSystem" | Out-Null
& $nssm set $ServiceName Type "SERVICE_WIN32_OWN_PROCESS" | Out-Null

# Environment for the service process
$javaHome = $env:JAVA_HOME
if (-not $javaHome) { $javaHome = $env:JDK_HOME }
if (-not $javaHome) {
    try {
        $reg = Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" -Name JAVA_HOME -ErrorAction SilentlyContinue
        if ($reg -and $reg.JAVA_HOME) { $javaHome = [string]$reg.JAVA_HOME }
    } catch {
        # ignore
    }
}

$envExtra = "NIFI_HOME=$NifiHome"
if ($javaHome) {
    $envExtra = "JAVA_HOME=$javaHome;$envExtra"
    Write-Info "JAVA_HOME=$javaHome"
} else {
    Write-WarnMsg "JAVA_HOME not set - service may fail if Java is not on system PATH"
}
Write-Info "NIFI_HOME=$NifiHome"
& $nssm set $ServiceName AppEnvironmentExtra $envExtra | Out-Null

# Permanent machine env (only if missing)
function Set-PermanentEnvIfMissing {
    param([string]$Name, [string]$Value)
    $existing = [Environment]::GetEnvironmentVariable($Name, "Machine")
    if ($existing) {
        Write-Info "Permanent $Name already set - leaving as-is"
        return
    }
    try {
        [Environment]::SetEnvironmentVariable($Name, $Value, "Machine")
        Write-Info "Set permanent ${Name}=$Value"
    } catch {
        # ${Name} required: bare $Name: is parsed as a drive-qualified variable
        # (InvalidVariableReferenceWithDrive on Windows PowerShell 5.1).
        Write-WarnMsg "Could not set permanent ${Name}: $($_.Exception.Message)"
    }
}
Set-PermanentEnvIfMissing -Name "NIFI_HOME" -Value $NifiHome
if ($javaHome) {
    Set-PermanentEnvIfMissing -Name "JAVA_HOME" -Value $javaHome
    try {
        $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
        $binPath = Join-Path $javaHome "bin"
        if ($machinePath -and ($machinePath -notlike "*$binPath*")) {
            [Environment]::SetEnvironmentVariable("Path", "$binPath;$machinePath", "Machine")
            Write-Info "Prepended $binPath to machine PATH"
        }
    } catch {
        Write-WarnMsg "Could not update machine PATH: $($_.Exception.Message)"
    }
}

# Verify
$null = & sc.exe query $ServiceName 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-ErrMsg "Service $ServiceName was not registered (sc query failed)"
    exit 1
}
Write-Info "Service $ServiceName registered successfully."
Write-Info "  Application : $ComSpec"
Write-Info "  AppParameters: $AppParameters"
Write-Info "  AppDirectory : $BinDir"

# Open Windows Firewall so external clients can reach NiFi (and allow ICMP)
function Open-KdFirewallPort {
    param(
        [string]$TcpPort,
        [string]$DisplayName
    )
    try {
        if (-not (Get-NetFirewallRule -DisplayName $DisplayName -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName $DisplayName -Direction Inbound `
                -Protocol TCP -LocalPort $TcpPort -Action Allow -Profile Any | Out-Null
            Write-Info "Firewall rule created: $DisplayName (TCP $TcpPort inbound Allow)"
        } else {
            Write-Info "Firewall rule already present: $DisplayName (TCP $TcpPort)"
        }
    } catch {
        Write-WarnMsg "Could not open firewall for TCP $TcpPort : $($_.Exception.Message)"
    }
}
function Open-KdFirewallIcmp {
    $icmpName = "Temp-ICMP"
    try {
        if (-not (Get-NetFirewallRule -DisplayName $icmpName -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName $icmpName -Direction Inbound `
                -Protocol ICMPv4 -Action Allow -Profile Any | Out-Null
            Write-Info "Firewall rule created: $icmpName (ICMPv4 inbound Allow)"
        } else {
            Write-Info "Firewall rule already present: $icmpName"
        }
    } catch {
        Write-WarnMsg "Could not open ICMP firewall rule: $($_.Exception.Message)"
    }
}

if ($Port -and $Port.Trim()) {
    $portTrim = $Port.Trim()
    $portOk = $false
    $portInt = 0
    if ($portTrim -match '^\d+$') {
        $portInt = [int]$portTrim
        if ($portInt -ge 1 -and $portInt -le 65535) {
            $portOk = $true
        }
    }
    if (-not $portOk) {
        Write-WarnMsg "Invalid port for firewall open: '$Port' (must be integer 1-65535) — skipping firewall rule creation"
    } else {
        $tcpName = "Temp-KD-$portInt"
        Write-Info "Opening Windows Firewall for NiFi port $portInt (DisplayName=$tcpName)..."
        Open-KdFirewallPort -TcpPort $portInt -DisplayName $tcpName
        Open-KdFirewallIcmp
    }
}

if ($Start) {
    Write-Info "Starting $ServiceName ..."
    & $nssm start $ServiceName
    if ($LASTEXITCODE -eq 0) {
        Write-Info "Start requested."
    } else {
        Write-WarnMsg "nssm start exit $LASTEXITCODE"
    }
} elseif (-not $NonInteractive) {
    Write-Host -ForegroundColor Yellow -NoNewline "Start the service now? (Y/N) "
    $ans = Read-Host
    if ($ans -match '^[Yy]') {
        & $nssm start $ServiceName
    }
}

Write-Host ""
Write-Info "Done. Manage with: sc start $ServiceName  |  nssm status $ServiceName"
exit 0