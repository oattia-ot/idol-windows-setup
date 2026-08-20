#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    IDOL SSL Certificate Generation for Windows Server (no Docker)

.DESCRIPTION
    Builds a full CA hierarchy (Root -> Intermediate -> Service certs) with
    proper Subject Alternative Names, Java keystores, Windows trust store
    installation, and deployment to IDOL toolkit locations.

    PREFERRED ON WINDOWS: use the Python generator instead (no OpenSSL CA
    index.txt / CRLF issues):

        python tools\generate_ssl.py --auto
        Generate-SSL.bat --auto

    See tools/generate_ssl.py (cryptography library). Keep this script only
    if you specifically need the OpenSSL-based flow.

.PARAMETER Auto
    Non-interactive mode: auto-generate passwords and use built-in subject defaults.

.PARAMETER Help
    Show usage information.

.NOTES
    Environment variables (optional):
      SSL_SERVICE_USER   Windows account that should own private keys (e.g. idol)
      SSL_SERVICE_GROUP  Windows group for private-key read access (e.g. idol)
      EXTRA_IP_SANS_ENV  Comma-separated IPs for -Auto mode
      IDOL_NET_HOST_IP   Default value shown in Extra IP SANs prompt

    Prerequisites:
      - openssl.exe  (auto-installed via winget or a portable ZIP if
                      missing; see -NoAutoInstall to disable this)
      - keytool.exe  (from a JDK, must be in PATH)
      - curl.exe     (optional, only used by health-check)
#>

[CmdletBinding()]
param(
    [Alias("a")]
    [switch]$Auto,

    [Alias("h")]
    [switch]$Help,

    # Skip the automatic OpenSSL install attempts (winget / portable ZIP
    # download) and go straight to the manual-install instructions. Use this
    # on servers with no outbound internet access.
    [switch]$NoAutoInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"

# ==============================================================================
# LOGGING HELPERS (ASCII-only)
# ==============================================================================
function Write-Info  { param([string]$Message) Write-Host "[INFO]  $Message" -ForegroundColor Green }
function Write-Warn  { param([string]$Message) Write-Host "[WARN]  $Message" -ForegroundColor Yellow }
function Write-Err   { param([string]$Message) Write-Host "[ERROR] $Message" -ForegroundColor Red }
function Write-Ok    { param([string]$Message) Write-Host "  [OK]  $Message" -ForegroundColor Green }
function Write-Fail  { param([string]$Message) Write-Host "  [FAIL] $Message" -ForegroundColor Red }
function Write-Skip  { param([string]$Message) Write-Host "  [SKIP] $Message" -ForegroundColor DarkYellow }

function Write-Section {
    param([string]$Title)
    $border = "=" * 65
    Write-Host ""
    Write-Host $border -ForegroundColor Cyan
    Write-Host ("  {0,-63}" -f $Title) -ForegroundColor Cyan
    Write-Host $border -ForegroundColor Cyan
    Write-Host ""
}

# ==============================================================================
# EXTERNAL TOOL INVOCATION (openssl / keytool)
# ==============================================================================
# With $ErrorActionPreference = "Stop" (set below), PowerShell can turn a
# native command's stderr output into a terminating error the moment the
# process exits non-zero - and it does this BEFORE a trailing "2>$null"
# on the call actually suppresses it, so the trap ends up showing only the
# first line the process happened to write (e.g. openssl's routine
# "Using configuration from ..." banner) instead of the real error that
# came after it. That made every failure here look identical and gave no
# way to diagnose what actually went wrong.
#
# The fix: always merge stderr into the output stream ourselves (2>&1) and
# capture it into a variable rather than redirecting it to $null directly.
# Assigning native output to a variable keeps PowerShell from promoting it
# to a terminating error, so we can inspect $LASTEXITCODE ourselves and
# report the FULL captured output when something really fails.
function Invoke-ExternalTool {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$ToolArgs,
        [string]$ErrorContext
    )
    if (-not $ErrorContext) { $ErrorContext = "$Exe $($ToolArgs[0])" }

    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $output = $null
    try {
        $output = & $Exe @ToolArgs 2>&1
    } finally {
        $ErrorActionPreference = $prevEap
    }

    if ($LASTEXITCODE -ne 0) {
        Write-Err "$ErrorContext failed (exit code $LASTEXITCODE)"
        if ($output) {
            Write-Host "  ---- $Exe output ----" -ForegroundColor Red
            ($output | Out-String) -split "`r?`n" | Where-Object { $_.Trim() } | ForEach-Object {
                Write-Host "  $_" -ForegroundColor Red
            }
            Write-Host "  ----------------------" -ForegroundColor Red
        }
        throw "$ErrorContext failed - see $Exe output above"
    }

    return $output
}

function Invoke-OpenSsl {
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$OpenSslArgs,
        [string]$ErrorContext
    )
    if (-not $ErrorContext) { $ErrorContext = "openssl $($OpenSslArgs[0])" }
    return Invoke-ExternalTool -Exe "openssl" -ToolArgs $OpenSslArgs -ErrorContext $ErrorContext
}

# ==============================================================================
# PATHS & CONSTANTS
# ==============================================================================
$ScriptDir   = $PSScriptRoot
if (-not $ScriptDir) { $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$RepoRoot    = (Resolve-Path (Join-Path $ScriptDir "..\..") -ErrorAction SilentlyContinue)
if (-not $RepoRoot) { $RepoRoot = $ScriptDir }
$RepoRoot    = $RepoRoot.Path
$ScriptName  = $MyInvocation.MyCommand.Name

$DaysRoot         = 7300   # 20 years
$DaysIntermediate = 3650   # 10 years
$DaysCert         = 825    # ~2 years

$SslRoot     = Join-Path $ScriptDir "ssl"
$CaDir       = Join-Path $SslRoot "intermediate"
$CertsDir    = Join-Path $CaDir "certs"
$PrivateDir  = Join-Path $CaDir "private"
$NifiDir     = Join-Path $CaDir "nifi"
$LogDir      = Join-Path $ScriptDir "logs"

$DeployBasicIdol     = Join-Path $RepoRoot "idol-containers-toolkit\basic-idol\ssl"
$DeployDataAdmin     = Join-Path $RepoRoot "idol-containers-toolkit\data-admin\ssl"
$DeployRichMedia     = Join-Path $RepoRoot "idol-containers-toolkit\rich-media\ssl"
$DeployLicenseServer = Join-Path $RepoRoot "idol-licenseserver\ssl"

$SslPasswordsEnv = Join-Path $RepoRoot "env\.idol-ssl-passwords.ps1"

$ExternalHostname = "idol-docker-host"

$SslServiceUser  = $env:SSL_SERVICE_USER
$SslServiceGroup = $env:SSL_SERVICE_GROUP
$ExtraIpSansEnv  = $env:EXTRA_IP_SANS_ENV

$Services = @(
    "idol-docker-host",
    "obsidian",
    "idol-agentstore",
    "idol-category",
    "idol-categorisation-agentstore",
    "idol-community",
    "idol-content",
    "idol-find",
    "idol-httpd-reverse-proxy",
    "idol-licenseserver",
    "idol-nifi",
    "idol-view",
    "idol-dataadmin",
    "idol-dataadmin-community",
    "idol-dataadmin-viewserver",
    "idol-dataadmin-statsserver",
    "idol-dataadmin-find",
    "idol-qms",
    "idol-qms-agentstore",
    "idol-answerserver",
    "idol-passageextractor-agentstore",
    "idol-passageextractor-content",
    "idol-factbank-postgres",
    "idol-answerbank-agentstore",
    "idol-mediaserver",
    "idol-mmap-playlistserver",
    "idol-mmap-app"
)

$HealthChecks = @(
    @{ Name = "Community";    Port = 9030  },
    @{ Name = "ViewServer";   Port = 9080  },
    @{ Name = "Content";      Port = 9100  },
    @{ Name = "AnswerServer"; Port = 12000 },
    @{ Name = "QMS";          Port = 16000 },
    @{ Name = "StatsServer";  Port = 19870 },
    @{ Name = "Agentstore";   Port = 20050 }
)

# ==============================================================================
# RUNTIME STATE
# ==============================================================================
$script:AutoMode        = $Auto.IsPresent
$script:SkipGeneration  = $false
$script:LogFile         = $null
$script:KeystorePass    = $null
$script:TruststorePass  = $null
$script:HealthFailCount = 0
$script:CertFailures    = 0
$script:CertStatus      = @{}

$script:CertCountry = "US"
$script:CertState   = "State"
$script:CertCity    = "City"
$script:CertOrg     = "Organization"
$script:CertOu      = "IT"

$script:ExtraDnsSans = @()
$script:ExtraIpSans  = @()

# ==============================================================================
# AUTO-DETECTED HOSTNAME + FQDN (lower + UPPER)
# ==============================================================================
$DetectedHostname = $env:COMPUTERNAME
try {
    $DetectedFqdn = [System.Net.Dns]::GetHostEntry($DetectedHostname).HostName
} catch {
    $DetectedFqdn = ""
}

$HostnameLower = $DetectedHostname.ToLowerInvariant()
$HostnameUpper = $DetectedHostname.ToUpperInvariant()

$FqdnLower = ""
$FqdnUpper = ""
if ($DetectedFqdn -and ($DetectedFqdn -ne $DetectedHostname)) {
    $FqdnLower = $DetectedFqdn.ToLowerInvariant()
    $FqdnUpper = $DetectedFqdn.ToUpperInvariant()
}

# ==============================================================================
# LOGGING SETUP
# ==============================================================================
function Setup-Logging {
    if (-not (Test-Path $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    }
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $script:LogFile = Join-Path $LogDir "generate-ssl-$timestamp.log"
    Start-Transcript -Path $script:LogFile -Append -Force | Out-Null
    Write-Info "Log: $script:LogFile"
}

function Stop-Logging {
    try { Stop-Transcript | Out-Null } catch {}
}

# ==============================================================================
# ERROR HANDLING
# ==============================================================================
trap {
    Write-Err "Failed at line $($_.InvocationInfo.ScriptLineNumber) : $($_.Exception.Message)"
    Write-Err "Full trace:  powershell -File $ScriptName  (run with -Verbose for more detail)"
    Stop-Logging
    exit 1
}

# ==============================================================================
# USAGE
# ==============================================================================
function Show-Usage {
    Write-Host @"
======================================================================
  IDOL SSL Certificate Generator (Windows Server - no Docker)
======================================================================

  Usage:  .\$ScriptName [OPTIONS]

  Options:
    -Auto, -a         Non-interactive mode (auto-generate passwords + defaults)
    -NoAutoInstall    Don't attempt to auto-install OpenSSL if missing
                      (use on servers with no outbound internet access)
    -Help, -h         Show this help message

  Key Paths:
    Repo root:     $RepoRoot
    SSL output:    $SslRoot
    Password file: $SslPasswordsEnv

  Environment Variables:
    SSL_SERVICE_USER     Windows account for private-key ownership
    SSL_SERVICE_GROUP    Group for private-key read access
    EXTRA_IP_SANS_ENV    Extra IPs for -Auto mode (CSV)
    IDOL_NET_HOST_IP     Default IP shown in Extra IP SANs prompt

"@ -ForegroundColor Cyan
    exit 0
}

# ==============================================================================
# PREREQUISITES
# ==============================================================================
function Find-OpenSsl {
    # 1. Already in PATH?
    $cmd = Get-Command openssl -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    # 2. Explicit override
    if ($env:OPENSSL_PATH -and (Test-Path $env:OPENSSL_PATH)) {
        return $env:OPENSSL_PATH
    }

    # 3. Common install locations on Windows
    $candidates = @(
        "${env:ProgramFiles}\OpenSSL-Win64\bin\openssl.exe",
        "${env:ProgramFiles}\OpenSSL-Win32\bin\openssl.exe",
        "${env:ProgramFiles(x86)}\OpenSSL-Win32\bin\openssl.exe",
        "${env:ProgramFiles}\OpenSSL\bin\openssl.exe",
        "C:\OpenSSL-Win64\bin\openssl.exe",
        "C:\OpenSSL-Win32\bin\openssl.exe",
        "C:\OpenSSL\bin\openssl.exe",
        "${env:ProgramFiles}\Git\usr\bin\openssl.exe",
        "${env:ProgramFiles}\Git\mingw64\bin\openssl.exe",
        "${env:LOCALAPPDATA}\Programs\Git\usr\bin\openssl.exe",
        "C:\Strawberry\c\bin\openssl.exe",
        # FireDaemon OpenSSL (winget id FireDaemon.OpenSSL / MSI installer) -
        # this is the most common modern distribution and was previously
        # missing from this list, so an existing winget/MSI install went
        # undetected and the script incorrectly reported it as missing.
        "${env:ProgramFiles}\FireDaemon OpenSSL 4\bin\openssl.exe",
        "${env:ProgramFiles}\FireDaemon OpenSSL 3\bin\openssl.exe",
        "${env:ProgramFiles}\FireDaemon OpenSSL 1\bin\openssl.exe",
        "${env:ProgramFiles(x86)}\FireDaemon OpenSSL 4\bin\openssl.exe",
        "${env:ProgramFiles(x86)}\FireDaemon OpenSSL 3\bin\openssl.exe",
        # Our own portable fallback download (see Install-OpenSslPortable)
        "$ScriptDir\tools\openssl\bin\openssl.exe"
    )

    foreach ($path in $candidates) {
        if ($path -and (Test-Path $path)) {
            return $path
        }
    }

    # 4. Last resort: search Program Files one level deep for any
    #    "OpenSSL*"/"FireDaemon*" folder containing bin\openssl.exe
    #    (covers version-numbered folder names we didn't enumerate above).
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $root -or -not (Test-Path $root)) { continue }
        $hit = Get-ChildItem -Path $root -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match 'OpenSSL|FireDaemon' } |
            ForEach-Object { Join-Path $_.FullName "bin\openssl.exe" } |
            Where-Object { Test-Path $_ } |
            Select-Object -First 1
        if ($hit) { return $hit }
    }

    return $null
}

# ==============================================================================
# OPENSSL AUTO-INSTALL (fallback when not already present)
# ==============================================================================
function Install-OpenSslViaWinget {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return $false }

    Write-Info "winget found - attempting automatic OpenSSL install (FireDaemon.OpenSSL)..."
    try {
        $proc = Start-Process -FilePath "winget" `
            -ArgumentList @(
                "install", "--id", "FireDaemon.OpenSSL", "-e",
                "--silent", "--accept-package-agreements", "--accept-source-agreements"
            ) `
            -NoNewWindow -PassThru -Wait -ErrorAction Stop

        if ($proc.ExitCode -eq 0) {
            Write-Ok "winget reported success"
            return $true
        } else {
            Write-Warn "winget exited with code $($proc.ExitCode)"
            return $false
        }
    } catch {
        Write-Warn "winget install failed: $($_.Exception.Message)"
        return $false
    }
}

function Install-OpenSslPortable {
    # Downloads the FireDaemon OpenSSL 3.5 LTS "no installer" ZIP distribution
    # and unpacks just openssl.exe + its DLLs into a local, portable folder
    # under the script directory. Requires no admin install, no Chocolatey,
    # and no package manager - only outbound HTTPS access.
    $destRoot = Join-Path $ScriptDir "tools\openssl"
    if (Test-Path (Join-Path $destRoot "bin\openssl.exe")) {
        return (Join-Path $destRoot "bin\openssl.exe")
    }

    $zipPageUrl = "https://www.firedaemon.com/download-firedaemon-openssl-3-5-zip"
    $tmpDir     = Join-Path $env:TEMP "kd-openssl-portable"
    $zipPath    = Join-Path $tmpDir "openssl.zip"

    try {
        Write-Info "Attempting portable OpenSSL download (no installer, no admin tooling required)..."
        New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null

        if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
            & curl.exe -L --fail --silent --show-error --max-time 60 -o $zipPath $zipPageUrl
            if ($LASTEXITCODE -ne 0) { throw "curl exited with code $LASTEXITCODE" }
        } else {
            Invoke-WebRequest -Uri $zipPageUrl -OutFile $zipPath -UseBasicParsing -TimeoutSec 60 -MaximumRedirection 5
        }

        if (-not (Test-Path $zipPath) -or (Get-Item $zipPath).Length -lt 1000) {
            throw "downloaded file is missing or too small - likely blocked by a proxy/firewall"
        }

        $extractDir = Join-Path $tmpDir "extracted"
        Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force

        # The FireDaemon ZIP contains an x64\ (and x86\) subfolder with the
        # binaries - grab openssl.exe from the x64 build; DLLs live in the
        # same folder and are required alongside it.
        $exeHit = Get-ChildItem -Path $extractDir -Recurse -Filter "openssl.exe" -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\' } |
            Select-Object -First 1
        if (-not $exeHit) {
            $exeHit = Get-ChildItem -Path $extractDir -Recurse -Filter "openssl.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        }
        if (-not $exeHit) { throw "openssl.exe not found inside downloaded archive" }

        $srcBin = $exeHit.DirectoryName
        New-Item -ItemType Directory -Path (Join-Path $destRoot "bin") -Force | Out-Null
        Copy-Item -Path (Join-Path $srcBin "*") -Destination (Join-Path $destRoot "bin") -Recurse -Force

        # Some distributions keep default OpenSSL config one level up in ssl\
        $sslCnfSrc = Join-Path (Split-Path $srcBin -Parent) "ssl"
        if (Test-Path $sslCnfSrc) {
            Copy-Item -Path $sslCnfSrc -Destination (Join-Path $destRoot "ssl") -Recurse -Force -ErrorAction SilentlyContinue
        }

        $finalExe = Join-Path $destRoot "bin\openssl.exe"
        if (Test-Path $finalExe) {
            Write-Ok "Portable OpenSSL installed to $destRoot"
            return $finalExe
        }
        throw "copy step did not produce $finalExe"
    } catch {
        Write-Warn "Portable OpenSSL download failed: $($_.Exception.Message)"
        return $null
    } finally {
        Remove-Item -Path $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Test-Prerequisites {
    Write-Section "Checking Prerequisites"
    $missing = @()

    # --- OpenSSL (auto-detect, then auto-install if missing) ---
    $opensslExe = Find-OpenSsl

    if (-not $opensslExe -and -not $NoAutoInstall.IsPresent) {
        Write-Warn "openssl not found - attempting to install it automatically"

        if (Install-OpenSslViaWinget) {
            $opensslExe = Find-OpenSsl
        }

        if (-not $opensslExe) {
            $portableExe = Install-OpenSslPortable
            if ($portableExe) { $opensslExe = $portableExe }
        }
    }

    if ($opensslExe) {
        $opensslDir = Split-Path $opensslExe -Parent
        if ($env:Path -notlike "*$opensslDir*") {
            $env:Path = "$opensslDir;$env:Path"
        }
        # Also set a script-scoped full path so we can call it reliably
        $script:OpenSslExe = $opensslExe
        Write-Ok "openssl  ($opensslExe)"
    } else {
        Write-Fail "openssl - not found in PATH or common locations, and automatic install failed"
        $missing += "openssl"
    }

    # --- keytool ---
    if (Get-Command keytool -ErrorAction SilentlyContinue) {
        Write-Ok "keytool"
    } else {
        Write-Fail "keytool - not found in PATH"
        $missing += "keytool"
    }

    # --- curl (optional) ---
    if (Get-Command curl -ErrorAction SilentlyContinue) {
        Write-Ok "curl (optional - available)"
    } else {
        Write-Warn "curl not found - health-check will be skipped"
    }

    if ($missing.Count -gt 0) {
        Write-Err "Missing required tools: $($missing -join ', ')"

        if ($missing -contains "openssl") {
            Write-Host ""
            Write-Host "  This script already tried to install OpenSSL automatically" -ForegroundColor Yellow
            Write-Host "  (via winget, then a portable no-installer ZIP download) and" -ForegroundColor Yellow
            Write-Host "  that did not succeed - most likely this server has no" -ForegroundColor Yellow
            Write-Host "  outbound internet access, or winget isn't installed." -ForegroundColor Yellow
            Write-Host ""
            Write-Host "  How to install OpenSSL on Windows Server manually:" -ForegroundColor Yellow
            Write-Host "  ---------------------------------------------------" -ForegroundColor Yellow
            Write-Host "  Option A - winget (built into Windows Server 2022+/2025):"
            Write-Host "    winget install --id FireDaemon.OpenSSL -e"
            Write-Host ""
            Write-Host "  Option B - Portable ZIP, no installer/admin rights needed:"
            Write-Host "    1. On a machine with internet access, download the ZIP from:"
            Write-Host "       https://www.firedaemon.com/download-firedaemon-openssl-3-5-zip"
            Write-Host "    2. Copy the ZIP to this server and extract it"
            Write-Host "    3. Copy the x64\ folder's contents into:"
            Write-Host "       $ScriptDir\tools\openssl\bin\"
            Write-Host "       (so that $ScriptDir\tools\openssl\bin\openssl.exe exists)"
            Write-Host "    4. Re-run this script (it auto-detects that folder)"
            Write-Host ""
            Write-Host "  Option C - Full EXE installer:"
            Write-Host "    1. Download from https://www.firedaemon.com/download-firedaemon-openssl"
            Write-Host "       (or https://slproweb.com/products/Win32OpenSSL.html)"
            Write-Host "    2. Run the installer (default path is fine)"
            Write-Host "    3. Re-run this script (it auto-detects common install paths)"
            Write-Host ""
            Write-Host "  Option D - If Git for Windows is installed:"
            Write-Host "    The script will auto-detect openssl from Git\usr\bin"
            Write-Host ""
            Write-Host "  Option E - Set an explicit path before running:"
            Write-Host "    `$env:OPENSSL_PATH = 'C:\Program Files\OpenSSL-Win64\bin\openssl.exe'"
            Write-Host "    .\Generate-SSL.ps1"
            Write-Host ""
            Write-Host "  Note: 'choco install openssl' only works if Chocolatey is" -ForegroundColor DarkGray
            Write-Host "  already installed on this machine - it is not bundled with" -ForegroundColor DarkGray
            Write-Host "  Windows, so skip that option unless you've set up choco yourself." -ForegroundColor DarkGray
            Write-Host ""
        }
        exit 1
    }
}

# ==============================================================================
# CERTIFICATE SUBJECT INFORMATION
# ==============================================================================
function Collect-SubjectInfo {
    Write-Section "Certificate Subject Information"

    if ($script:AutoMode) {
        if ($ExtraIpSansEnv) {
            $script:ExtraIpSans = $ExtraIpSansEnv.Split(",") |
                ForEach-Object { $_.Trim() } |
                Where-Object { $_ }
        }

        Write-Info "Auto mode - using default subject values."
        Write-Host ""
        Write-Host "  Subject DN (all certs):" -ForegroundColor White
        Write-Host "    C  = $($script:CertCountry)"
        Write-Host "    ST = $($script:CertState)"
        Write-Host "    L  = $($script:CertCity)"
        Write-Host "    O  = $($script:CertOrg)"
        Write-Host "    OU = $($script:CertOu)"
        Write-Host "    CN = (per-service name)"
        Write-Host ""
        Write-Host "  SANs per cert:" -ForegroundColor White
        Write-Host "    DNS.1 = (service-name)"
        Write-Host "    DNS.2 = $ExternalHostname"
        Write-Host "    DNS.3 = localhost"
        Write-Host "    IP.1  = 127.0.0.1"
        Write-Host "    + auto-injected (always, deduplicated):" -ForegroundColor Cyan
        Write-Host "        DNS.x = $HostnameLower   (hostname lower)"
        Write-Host "        DNS.y = $HostnameUpper   (hostname UPPER)"
        if ($FqdnLower) {
            Write-Host "        DNS.x = $FqdnLower   (FQDN lower)"
            Write-Host "        DNS.y = $FqdnUpper   (FQDN UPPER)"
        }
        if ($script:ExtraIpSans.Count -gt 0) {
            $i = 2
            foreach ($ip in $script:ExtraIpSans) {
                Write-Host "    IP.$i = $ip"
                $i++
            }
        }
        return
    }

    # Interactive mode
    Write-Host "  These values are embedded in every generated certificate."
    Write-Host "  Press Enter to accept the default shown in [brackets]."
    Write-Host ""

    Write-Host "  Subject Distinguished Name" -ForegroundColor Yellow

    Write-Host -ForegroundColor Yellow -NoNewline "  Country Name (2-letter ISO code)    [$($script:CertCountry)] "
    $in = Read-Host
    if ($in) { $script:CertCountry = $in.ToUpperInvariant() }
    while ($script:CertCountry -notmatch '^[A-Za-z]{2}$') {
        Write-Warn "Country must be exactly 2 letters (e.g. US, GB, IL)."
        Write-Host -ForegroundColor Yellow -NoNewline "  Country Name (2-letter ISO code)    [US] "
        $in = Read-Host
        $script:CertCountry = if ($in) { $in.ToUpperInvariant() } else { "US" }
    }

    Write-Host -ForegroundColor Yellow -NoNewline "  State / Province Name               [$($script:CertState)] "
    $in = Read-Host
    if ($in) { $script:CertState = $in }

    Write-Host -ForegroundColor Yellow -NoNewline "  Locality / City                     [$($script:CertCity)] "
    $in = Read-Host
    if ($in) { $script:CertCity = $in }

    Write-Host -ForegroundColor Yellow -NoNewline "  Organization Name                   [$($script:CertOrg)] "
    $in = Read-Host
    if ($in) { $script:CertOrg = $in }

    Write-Host -ForegroundColor Yellow -NoNewline "  Organizational Unit                 [$($script:CertOu)] "
    $in = Read-Host
    if ($in) { $script:CertOu = $in }

    Write-Host ""
    Write-Host "  Common Name" -ForegroundColor White
    Write-Host "    CN is set automatically per service" -ForegroundColor Cyan
    Write-Host ""

    Write-Host "  Subject Alternative Names (SANs)" -ForegroundColor White
    Write-Host ""
    Write-Host "  Added to every cert automatically:"
    Write-Host "    DNS.1 = (service-name)  (internal hostname)" -ForegroundColor Cyan
    Write-Host "    DNS.2 = $ExternalHostname" -ForegroundColor Cyan
    Write-Host "    DNS.3 = localhost" -ForegroundColor Cyan
    Write-Host "    IP.1  = 127.0.0.1" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Automatically added to every cert (no need to type anything):" -ForegroundColor Green
    Write-Host "    DNS.x = $HostnameLower   (hostname lower)"
    Write-Host "    DNS.y = $HostnameUpper   (hostname UPPER)"
    if ($FqdnLower) {
        Write-Host "    DNS.x = $FqdnLower   (FQDN lower)"
        Write-Host "    DNS.y = $FqdnUpper   (FQDN UPPER)"
    }
    Write-Host ""

    Write-Host "  Additional DNS SANs (comma-separated, or Enter to skip)." -ForegroundColor Cyan
    Write-Host "  Example: myserver.example.com, *.internal.corp" -ForegroundColor Yellow
    Write-Host -ForegroundColor Yellow -NoNewline "  Extra DNS SANs [] "
    $sanInput = Read-Host
    $script:ExtraDnsSans = @()
    if ($sanInput) {
        $script:ExtraDnsSans = $sanInput.Split(",") |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ }
    }

    $ipDefault = if ($env:IDOL_NET_HOST_IP) { $env:IDOL_NET_HOST_IP } elseif ($ExtraIpSansEnv) { $ExtraIpSansEnv } else { "" }
    Write-Host "  Additional IP SANs (comma-separated, or Enter to skip)." -ForegroundColor Cyan
    Write-Host "  WARNING: If using a remote hyperscaler (e.g. Azure) environment, enter the remote IP address here." -ForegroundColor Red
    Write-Host -ForegroundColor Yellow -NoNewline "  Extra IP SANs [$(if ($ipDefault) { $ipDefault } else { 'none' })] "
    $ipInput = Read-Host
    if (-not $ipInput -and $ipDefault) { $ipInput = $ipDefault }

    $script:ExtraIpSans = @()
    if ($ipInput) {
        $script:ExtraIpSans = $ipInput.Split(",") |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ }
    }

    # Review
    $border = "-" * 55
    Write-Host ""
    Write-Host "  $border" -ForegroundColor Cyan
    Write-Host "  Review certificate settings" -ForegroundColor White
    Write-Host "  $border" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Subject DN:" -ForegroundColor White
    Write-Host "    C  = $($script:CertCountry)"
    Write-Host "    ST = $($script:CertState)"
    Write-Host "    L  = $($script:CertCity)"
    Write-Host "    O  = $($script:CertOrg)"
    Write-Host "    OU = $($script:CertOu)"
    Write-Host "    CN = (per-service name)"
    Write-Host ""
    Write-Host "  SANs per cert:" -ForegroundColor White
    Write-Host "    DNS.1 = (service-name)"
    Write-Host "    DNS.2 = $ExternalHostname"
    Write-Host "    DNS.3 = localhost"
    Write-Host "    IP.1  = 127.0.0.1"
    Write-Host "    + auto-injected (if not duplicate):" -ForegroundColor Cyan
    Write-Host "        DNS.x = $HostnameLower   (hostname lower)"
    Write-Host "        DNS.y = $HostnameUpper   (hostname UPPER)"
    if ($FqdnLower) {
        Write-Host "        DNS.x = $FqdnLower   (FQDN lower)"
        Write-Host "        DNS.y = $FqdnUpper   (FQDN UPPER)"
    }

    if ($script:ExtraDnsSans.Count -gt 0) {
        $di = 4
        foreach ($san in $script:ExtraDnsSans) {
            Write-Host "    DNS.$di = $san"
            $di++
        }
    }
    if ($script:ExtraIpSans.Count -gt 0) {
        $ii = 2
        foreach ($ip in $script:ExtraIpSans) {
            Write-Host "    IP.$ii = $ip"
            $ii++
        }
    }

    Write-Host ""
    Write-Host "  Validity:" -ForegroundColor White
    Write-Host "    Root CA:        $DaysRoot days  (~$([math]::Round($DaysRoot/365)) years)"
    Write-Host "    Intermediate:   $DaysIntermediate days  (~$([math]::Round($DaysIntermediate/365)) years)"
    Write-Host "    Service certs:  $DaysCert days   (~$([math]::Round($DaysCert/365)) years)"
    Write-Host ""
    Write-Host "  $border" -ForegroundColor Cyan
    Write-Host -ForegroundColor Yellow -NoNewline "  Confirm and continue? [Enter / Ctrl+C to abort] "
    $null = Read-Host
}

# ==============================================================================
# PASSWORD MANAGEMENT
# ==============================================================================
function New-RandomPassword {
    $bytes = New-Object byte[] 48
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $b64 = [Convert]::ToBase64String($bytes)
    return ($b64 -replace '[/+=]', '').Substring(0, [Math]::Min(43, $b64.Length))
}

function Get-PasswordInteractive {
    param([string]$Label)
    while ($true) {
        Write-Host -ForegroundColor Yellow -NoNewline "  Enter $Label "
        $p1 = Read-Host -AsSecureString
        Write-Host -ForegroundColor Yellow -NoNewline "  Confirm $Label "
        $p2 = Read-Host -AsSecureString
        $bstr1 = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($p1)
        $bstr2 = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($p2)
        try {
            $plain1 = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr1)
            $plain2 = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr2)
            if ($plain1 -eq $plain2) { return $plain1 }
            Write-Warn "Passwords do not match - try again."
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr1)
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr2)
        }
    }
}

function Setup-Passwords {
    Write-Section "KeyStore / TrustStore Passwords"

    if ($script:AutoMode) {
        $script:KeystorePass   = New-RandomPassword
        $script:TruststorePass = New-RandomPassword
        Write-Ok "Auto-generated KeyStore password:   $($script:KeystorePass)"
        Write-Ok "Auto-generated TrustStore password: $($script:TruststorePass)"
    } else {
        Write-Host "  Generate random passwords?" -ForegroundColor Yellow
        Write-Host "    1) Yes - auto-generate  (recommended)" -ForegroundColor Cyan
        Write-Host "    2) No  - enter manually" -ForegroundColor Cyan
        Write-Host -ForegroundColor Yellow -NoNewline "  Choice [1/2] (default: 1) "
        $choice = Read-Host
        if (-not $choice) { $choice = "1" }

        if ($choice -eq "1") {
            $script:KeystorePass   = New-RandomPassword
            $script:TruststorePass = New-RandomPassword
            Write-Ok "KeyStore password:   $($script:KeystorePass)"
            Write-Ok "TrustStore password: $($script:TruststorePass)"
        } else {
            $script:KeystorePass   = Get-PasswordInteractive "KeyStore password"
            $script:TruststorePass = Get-PasswordInteractive "TrustStore password"
        }
    }

    # Persist for other scripts / future runs
    $envDir = Split-Path $SslPasswordsEnv -Parent
    if (-not (Test-Path $envDir)) {
        New-Item -ItemType Directory -Path $envDir -Force | Out-Null
    }

    $content = @"
# Auto-generated by Generate-SSL.ps1 - do not commit to source control
`$env:IDOL_CERT_KEYSTORE_PASS   = '$($script:KeystorePass)'
`$env:IDOL_CERT_TRUSTSTORE_PASS = '$($script:TruststorePass)'
"@
    Set-Content -Path $SslPasswordsEnv -Value $content -Encoding UTF8 -Force

    # Restrict ACL on the password file
    try {
        $acl = Get-Acl $SslPasswordsEnv
        $acl.SetAccessRuleProtection($true, $false)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            [System.Security.Principal.WindowsIdentity]::GetCurrent().Name,
            "FullControl", "Allow")
        $acl.SetAccessRule($rule)
        Set-Acl -Path $SslPasswordsEnv -AclObject $acl
    } catch {
        Write-Warn "Could not restrict ACL on password file: $($_.Exception.Message)"
    }

    Write-Ok "Credentials saved: $SslPasswordsEnv"
}

# ==============================================================================
# CONFIRM BEFORE WIPING EXISTING SSL
# ==============================================================================
function Confirm-Regeneration {
    if (-not (Test-Path $SslRoot)) { return }

    if ($script:AutoMode) {
        Write-Warn "Existing SSL directory found - auto mode: regenerating."
        Remove-Item -Path $SslRoot -Recurse -Force -ErrorAction SilentlyContinue
        return
    }

    Write-Warn "Existing SSL directory found: $SslRoot"
    Write-Host ""
    Write-Host "  What would you like to do?" -ForegroundColor White
    Write-Host "    s) Skip generation - redeploy + fix permissions on existing certs  (fast)" -ForegroundColor Cyan
    Write-Host "    r) Regenerate       - delete everything and create new certs" -ForegroundColor Cyan
    Write-Host "    a) Abort" -ForegroundColor Cyan
    Write-Host ""

    while ($true) {
        Write-Host -ForegroundColor Yellow -NoNewline "  Choice [s/r/a] "
        $answer = (Read-Host).ToLowerInvariant()
        switch ($answer) {
            { $_ -in @("s", "skip") } {
                $script:SkipGeneration = $true
                Write-Info "Skipping generation - existing certs will be redeployed."
                return
            }
            { $_ -in @("r", "regen", "regenerate") } {
                Write-Warn "Deleting $SslRoot ..."
                Remove-Item -Path $SslRoot -Recurse -Force
                Write-Ok "Deleted. Fresh certificates will be generated."
                return
            }
            { $_ -in @("a", "abort") } {
                Write-Info "Aborted."
                exit 0
            }
            default {
                Write-Warn "Enter 's' to skip, 'r' to regenerate, or 'a' to abort."
            }
        }
    }
}

# ==============================================================================
# CA DIRECTORY & CONFIG INITIALISATION
# ==============================================================================
function Initialize-CaDirectory {
    Write-Section "Initialising CA Directory Structure"

    New-Item -ItemType Directory -Path $CertsDir, $PrivateDir, $NifiDir, (Join-Path $CaDir "issued") -Force | Out-Null

    # Restrict private directory
    try {
        $acl = Get-Acl $PrivateDir
        $acl.SetAccessRuleProtection($true, $false)
        $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($currentUser, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")
        $acl.SetAccessRule($rule)
        Set-Acl -Path $PrivateDir -AclObject $acl
    } catch {
        Write-Warn "Could not restrict private directory ACL: $($_.Exception.Message)"
    }

    # index / serial / crlnumber
    Set-Content -Path (Join-Path $CaDir "index.txt") -Value "" -Encoding ASCII -Force
    Set-Content -Path (Join-Path $CaDir "serial")    -Value "1000" -Encoding ASCII -Force
    Set-Content -Path (Join-Path $CaDir "crlnumber") -Value "1000" -Encoding ASCII -Force

    Write-Ok "Directories created under $CaDir"

    # Use forward slashes for OpenSSL configs (required on Windows too)
    $caDirFwd = $CaDir -replace '\\', '/'

    # -- Root CA config --
    $rootCnf = @"
[ ca ]
default_ca = CA_default

[ CA_default ]
dir               = $caDirFwd
certs             = `$dir/certs
new_certs_dir     = `$dir/issued
database          = `$dir/index.txt
serial            = `$dir/serial
RANDFILE          = `$dir/private/.rand
private_key       = `$dir/private/ca.key.pem
certificate       = `$dir/certs/ca.cert.pem
crlnumber         = `$dir/crlnumber
default_crl_days  = 30
default_md        = sha256
name_opt          = ca_default
cert_opt          = ca_default
default_days      = 375
preserve          = no
policy            = policy_loose

[ policy_loose ]
countryName             = optional
stateOrProvinceName     = optional
localityName            = optional
organizationName        = optional
organizationalUnitName  = optional
commonName              = supplied
emailAddress            = optional

[ req ]
default_bits        = 4096
distinguished_name  = req_distinguished_name
string_mask         = utf8only
default_md          = sha256
x509_extensions     = v3_ca

[ req_distinguished_name ]
countryName             = Country Name (2 letter code)
stateOrProvinceName     = State or Province Name
localityName            = Locality Name
0.organizationName      = Organization Name
organizationalUnitName  = Organizational Unit Name
commonName              = Common Name

[ v3_ca ]
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid:always,issuer
basicConstraints       = critical, CA:true
keyUsage               = critical, digitalSignature, cRLSign, keyCertSign

[ v3_intermediate_ca ]
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid:always,issuer
basicConstraints       = critical, CA:true, pathlen:0
keyUsage               = critical, digitalSignature, cRLSign, keyCertSign

[ crl_ext ]
authorityKeyIdentifier = keyid:always
"@
    Set-Content -Path (Join-Path $CaDir "openssl-root-ca.cnf") -Value $rootCnf -Encoding ASCII -Force

    # -- Intermediate CA config --
    $intCnf = @"
[ ca ]
default_ca = CA_default

[ CA_default ]
dir               = $caDirFwd
certs             = `$dir/certs
new_certs_dir     = `$dir/issued
database          = `$dir/index.txt
serial            = `$dir/serial
RANDFILE          = `$dir/private/.rand
private_key       = `$dir/private/intermediate.key.pem
certificate       = `$dir/certs/intermediate.cert.pem
crlnumber         = `$dir/crlnumber
default_crl_days  = 30
default_md        = sha256
name_opt          = ca_default
cert_opt          = ca_default
default_days      = 375
preserve          = no
policy            = policy_loose
copy_extensions   = copy

[ policy_loose ]
countryName             = optional
stateOrProvinceName     = optional
localityName            = optional
organizationName        = optional
organizationalUnitName  = optional
commonName              = supplied
emailAddress            = optional

[ req ]
default_bits        = 2048
distinguished_name  = req_distinguished_name
string_mask         = utf8only
default_md          = sha256

[ req_distinguished_name ]
countryName             = Country Name (2 letter code)
stateOrProvinceName     = State or Province Name
localityName            = Locality Name
0.organizationName      = Organization Name
organizationalUnitName  = Organizational Unit Name
commonName              = Common Name

[ server_cert ]
basicConstraints       = CA:FALSE
nsCertType             = server
nsComment              = "OpenSSL Generated Server Certificate"
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid:always
keyUsage               = critical, digitalSignature, keyEncipherment
extendedKeyUsage       = serverAuth

[ crl_ext ]
authorityKeyIdentifier = keyid:always
"@
    Set-Content -Path (Join-Path $CaDir "openssl-ca.cnf") -Value $intCnf -Encoding ASCII -Force

    Write-Ok "OpenSSL CA configs written"
}

# ==============================================================================
# HELPER: Write per-service CNF with SANs
# ==============================================================================
function Write-ServiceCnf {
    param([string]$Service)

    $cnfFile = Join-Path $env:TEMP ("idol-ssl-{0}.cnf" -f [Guid]::NewGuid().ToString("N").Substring(0,8))

    $altLines = New-Object System.Collections.Generic.List[string]
    [void]$altLines.Add("DNS.1 = $Service")
    [void]$altLines.Add("DNS.2 = $ExternalHostname")
    [void]$altLines.Add("DNS.3 = localhost")
    [void]$altLines.Add("IP.1  = 127.0.0.1")

    $script:nextDnsIdx = 4

    $addDnsSan = {
        param([string]$Val)
        foreach ($existing in $altLines) {
            if ($existing -match "=\s*$([regex]::Escape($Val))$") { return }
        }
        [void]$altLines.Add("DNS.$($script:nextDnsIdx) = $Val")
        $script:nextDnsIdx++
    }

    if ($HostnameLower) { & $addDnsSan $HostnameLower }
    if ($HostnameUpper -and ($HostnameUpper -ne $HostnameLower)) { & $addDnsSan $HostnameUpper }
    if ($FqdnLower) { & $addDnsSan $FqdnLower }
    if ($FqdnUpper -and ($FqdnUpper -ne $FqdnLower)) { & $addDnsSan $FqdnUpper }

    foreach ($san in $script:ExtraDnsSans) {
        if ($san) { & $addDnsSan $san }
    }

    $altBlock = ($altLines -join "`n") + "`n"

    $ipIdx = 2
    foreach ($ip in $script:ExtraIpSans) {
        if ($ip) {
            $altBlock += "IP.$ipIdx = $ip`n"
            $ipIdx++
        }
    }

    $svcCnf = @"
[ req ]
default_bits        = 2048
distinguished_name  = req_distinguished_name
string_mask         = utf8only
default_md          = sha256
req_extensions      = v3_req

[ req_distinguished_name ]
countryName             = Country Name (2 letter code)
stateOrProvinceName     = State or Province Name
localityName            = Locality Name
0.organizationName      = Organization Name
organizationalUnitName  = Organizational Unit Name
commonName              = Common Name

[ v3_req ]
basicConstraints     = CA:FALSE
keyUsage             = critical, digitalSignature, keyEncipherment
extendedKeyUsage     = serverAuth
subjectAltName       = @alt_names

[ alt_names ]
$altBlock
[ server_cert ]
basicConstraints       = CA:FALSE
nsCertType             = server
nsComment              = "OpenSSL Generated Server Certificate"
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid:always
keyUsage               = critical, digitalSignature, keyEncipherment
extendedKeyUsage       = serverAuth
subjectAltName         = @alt_names
"@
    Set-Content -Path $cnfFile -Value $svcCnf -Encoding ASCII -Force
    return $cnfFile
}

# ==============================================================================
# HELPER: Build PKCS12 + JKS
# ==============================================================================
function Build-Keystore {
    param([string]$Service)

    $cert  = Join-Path $CertsDir "$Service.cert.pem"
    $key   = Join-Path $CertsDir "$Service.key.pem"
    $chain = Join-Path $CertsDir "ca-chain.cert.pem"
    $p12   = Join-Path $CertsDir "$Service.pkcs12"
    $jks   = Join-Path $CertsDir "$Service.jks"

    Invoke-OpenSsl -ErrorContext "PKCS12 export for $Service" -OpenSslArgs @(
        "pkcs12", "-export",
        "-in", $cert, "-inkey", $key, "-certfile", $chain,
        "-out", $p12, "-name", $Service,
        "-passout", "pass:$($script:KeystorePass)"
    ) | Out-Null

    Invoke-ExternalTool -Exe "keytool" -ErrorContext "Import $Service keystore to JKS" -ToolArgs @(
        "-importkeystore",
        "-srckeystore", $p12, "-srcstoretype", "PKCS12", "-srcstorepass", $script:KeystorePass,
        "-destkeystore", $jks, "-deststoretype", "JKS", "-deststorepass", $script:KeystorePass,
        "-destkeypass", $script:KeystorePass, "-noprompt"
    ) | Out-Null

    $aliasExists = (Invoke-ExternalTool -Exe "keytool" -ErrorContext "List $Service JKS aliases" -ToolArgs @(
        "-list", "-keystore", $jks, "-storepass", $script:KeystorePass
    ) | Select-String "ca-chain").Count
    if ($aliasExists -eq 0) {
        Invoke-ExternalTool -Exe "keytool" -ErrorContext "Import ca-chain into $Service JKS" -ToolArgs @(
            "-importcert",
            "-file", $chain, "-alias", "ca-chain",
            "-keystore", $jks, "-storepass", $script:KeystorePass, "-noprompt"
        ) | Out-Null
    }

    Write-Ok "$Service.pkcs12  +  $Service.jks"
}

# ==============================================================================
# STEP 1: Root CA
# ==============================================================================
function Step-RootCa {
    Write-Section "Step 1 - Root CA"

    $subj = "/C=$($script:CertCountry)/ST=$($script:CertState)/L=$($script:CertCity)/O=$($script:CertOrg)/OU=$($script:CertOu)/CN=IDOL Root CA"
    $key  = Join-Path $PrivateDir "ca.key.pem"
    $cert = Join-Path $CertsDir "ca.cert.pem"
    $cnf  = Join-Path $CaDir "openssl-root-ca.cnf"

    Invoke-OpenSsl -ErrorContext "Root CA key generation" -OpenSslArgs @("genrsa", "-out", $key, "4096") | Out-Null
    Restrict-PrivateKey -Path $key

    Invoke-OpenSsl -ErrorContext "Root CA certificate creation" -OpenSslArgs @(
        "req", "-config", $cnf,
        "-key", $key,
        "-new", "-x509", "-days", $DaysRoot, "-sha256", "-extensions", "v3_ca",
        "-out", $cert,
        "-subj", $subj
    ) | Out-Null

    Write-Ok "Root CA cert: $cert"
    $sub = Invoke-OpenSsl -ErrorContext "Reading root CA subject" -OpenSslArgs @("x509", "-in", $cert, "-noout", "-subject")
    $dat = Invoke-OpenSsl -ErrorContext "Reading root CA dates"   -OpenSslArgs @("x509", "-in", $cert, "-noout", "-dates")
    Write-Host "  $sub" -ForegroundColor Cyan
    Write-Host "  $dat" -ForegroundColor Cyan
}

# ==============================================================================
# STEP 2: Intermediate CA
# ==============================================================================
function Step-IntermediateCa {
    Write-Section "Step 2 - Intermediate CA"

    $subj = "/C=$($script:CertCountry)/ST=$($script:CertState)/L=$($script:CertCity)/O=$($script:CertOrg)/OU=$($script:CertOu)/CN=IDOL Intermediate CA"
    $key  = Join-Path $PrivateDir "intermediate.key.pem"
    $csr  = Join-Path $CertsDir "intermediate.csr.pem"
    $cert = Join-Path $CertsDir "intermediate.cert.pem"
    $chain = Join-Path $CertsDir "ca-chain.cert.pem"
    $rootCnf = Join-Path $CaDir "openssl-root-ca.cnf"
    $rootCert = Join-Path $CertsDir "ca.cert.pem"

    Invoke-OpenSsl -ErrorContext "Intermediate CA key generation" -OpenSslArgs @("genrsa", "-out", $key, "4096") | Out-Null
    Restrict-PrivateKey -Path $key

    Invoke-OpenSsl -ErrorContext "Intermediate CA CSR creation" -OpenSslArgs @(
        "req", "-config", $rootCnf, "-new", "-sha256",
        "-key", $key,
        "-out", $csr,
        "-subj", $subj
    ) | Out-Null

    Invoke-OpenSsl -ErrorContext "Intermediate CA signing (root -> intermediate)" -OpenSslArgs @(
        "ca", "-config", $rootCnf,
        "-extensions", "v3_intermediate_ca",
        "-days", $DaysIntermediate, "-notext", "-md", "sha256", "-batch",
        "-in", $csr,
        "-out", $cert
    ) | Out-Null

    Get-Content $cert, $rootCert | Set-Content $chain -Encoding ASCII

    $null = & openssl verify -CAfile $rootCert $cert 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "Intermediate CA verified against Root CA"
    } else {
        Write-Err "Intermediate CA verification failed"
        exit 1
    }

    $p12 = Join-Path $CertsDir "intermediate.pkcs12"
    Invoke-OpenSsl -ErrorContext "Intermediate CA PKCS12 export" -OpenSslArgs @(
        "pkcs12", "-export",
        "-in", $cert, "-inkey", $key, "-certfile", $rootCert,
        "-out", $p12, "-name", "intermediate-ca",
        "-passout", "pass:$($script:KeystorePass)"
    ) | Out-Null

    Write-Ok "Intermediate cert + PKCS12 created"
    $sub = Invoke-OpenSsl -ErrorContext "Reading intermediate CA subject" -OpenSslArgs @("x509", "-in", $cert, "-noout", "-subject")
    $dat = Invoke-OpenSsl -ErrorContext "Reading intermediate CA dates"   -OpenSslArgs @("x509", "-in", $cert, "-noout", "-dates")
    Write-Host "  $sub" -ForegroundColor Cyan
    Write-Host "  $dat" -ForegroundColor Cyan

    Start-Sleep -Seconds 2
}

# ==============================================================================
# STEP 3: Service certificates
# ==============================================================================
function Step-ServiceCerts {
    Write-Section "Step 3 - Service Certificates  ($($Services.Count) services)"
    Write-Host "  Default SANs: (service), $ExternalHostname, localhost, 127.0.0.1" -ForegroundColor Cyan
    Write-Host "  Auto-injected (always):  $HostnameLower + $HostnameUpper (hostname)" -ForegroundColor Green
    if ($FqdnLower) {
        Write-Host "                           + $FqdnLower + $FqdnUpper (FQDN)" -ForegroundColor Green
    }
    if ($script:ExtraDnsSans.Count -gt 0) {
        Write-Host "  Extra DNS SANs (user): $($script:ExtraDnsSans -join ', ')" -ForegroundColor Cyan
    }
    if ($script:ExtraIpSans.Count -gt 0) {
        Write-Host "  Extra IP  SANs: $($script:ExtraIpSans -join ', ')" -ForegroundColor Cyan
    }
    Write-Host ""

    $intCnf = Join-Path $CaDir "openssl-ca.cnf"
    $chain  = Join-Path $CertsDir "ca-chain.cert.pem"
    $intCert = Join-Path $CertsDir "intermediate.cert.pem"
    $rootCert = Join-Path $CertsDir "ca.cert.pem"

    foreach ($service in $Services) {
        $cnf  = Write-ServiceCnf -Service $service
        $key  = Join-Path $CertsDir "$service.key.pem"
        $csr  = Join-Path $CertsDir "$service.csr.pem"
        $cert = Join-Path $CertsDir "$service.cert.pem"
        $full = Join-Path $CertsDir "$service-fullchain.cert.pem"
        $subj = "/C=$($script:CertCountry)/ST=$($script:CertState)/L=$($script:CertCity)/O=$($script:CertOrg)/OU=$($script:CertOu)/CN=$service"

        Invoke-OpenSsl -ErrorContext "$service key generation" -OpenSslArgs @("genrsa", "-out", $key, "2048") | Out-Null
        Restrict-PrivateKey -Path $key

        Invoke-OpenSsl -ErrorContext "$service CSR creation" -OpenSslArgs @(
            "req", "-config", $cnf,
            "-key", $key, "-new", "-sha256", "-out", $csr, "-subj", $subj
        ) | Out-Null

        $startdate = (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmssZ")
        Invoke-OpenSsl -ErrorContext "$service certificate signing" -OpenSslArgs @(
            "ca", "-config", $intCnf,
            "-extfile", $cnf,
            "-extensions", "server_cert",
            "-startdate", $startdate,
            "-days", $DaysCert, "-notext", "-md", "sha256", "-batch",
            "-in", $csr, "-out", $cert
        ) | Out-Null

        Get-Content $cert, $intCert, $rootCert | Set-Content $full -Encoding ASCII

        Remove-Item $cnf -Force -ErrorAction SilentlyContinue

        $null = & openssl verify -CAfile $chain $cert 2>&1
        if ($LASTEXITCODE -eq 0) {
            $script:CertStatus[$service] = "PASS"
            $sans = (Invoke-OpenSsl -ErrorContext "$service SAN inspection" -OpenSslArgs @("x509", "-in", $cert, "-noout", "-text") |
                Select-String -Pattern "DNS:|IP Address:" |
                ForEach-Object { $_.Line.Trim() }) -join " "
            Write-Ok $service
            Write-Host "     Subject: $subj" -ForegroundColor Cyan
            Write-Host "     SANs:    $sans" -ForegroundColor Cyan
        } else {
            $script:CertStatus[$service] = "FAIL"
            Write-Fail "$service - chain verification FAILED"
            $script:CertFailures++
        }
    }
}

# ==============================================================================
# STEP 4: NiFi PKCS12 KeyStore & TrustStore (files only - no Docker)
# ==============================================================================
function Step-NifiKeystores {
    Write-Section "Step 4 - NiFi PKCS12 KeyStore and TrustStore"

    $nifiCert = Join-Path $CertsDir "idol-nifi.cert.pem"
    $nifiKey  = Join-Path $CertsDir "idol-nifi.key.pem"
    $chain    = Join-Path $CertsDir "ca-chain.cert.pem"
    $ks       = Join-Path $NifiDir "keystore.p12"
    $ts       = Join-Path $NifiDir "truststore.p12"

    if (-not (Test-Path $nifiCert) -or -not (Test-Path $nifiKey)) {
        Write-Warn "idol-nifi cert/key not found - skipping NiFi keystores"
        return
    }

    Invoke-OpenSsl -ErrorContext "NiFi keystore.p12 export" -OpenSslArgs @(
        "pkcs12", "-export",
        "-in", $nifiCert, "-inkey", $nifiKey, "-certfile", $chain,
        "-out", $ks, "-name", "nifi-key",
        "-passout", "pass:$($script:KeystorePass)"
    ) | Out-Null

    Restrict-PrivateKey -Path $ks
    Write-Ok "keystore.p12  -> $ks"

    Invoke-OpenSsl -ErrorContext "NiFi truststore.p12 export" -OpenSslArgs @(
        "pkcs12", "-export",
        "-nokeys",
        "-in", $chain,
        "-out", $ts,
        "-passout", "pass:$($script:TruststorePass)"
    ) | Out-Null

    Write-Ok "truststore.p12 -> $ts"

    $info = Invoke-OpenSsl -ErrorContext "NiFi keystore.p12 verification" -OpenSslArgs @(
        "pkcs12", "-in", $ks, "-passin", "pass:$($script:KeystorePass)", "-nokeys", "-info"
    ) | Select-String "subject"
    if ($info) {
        Write-Ok "keystore.p12 verified - contains certificate"
    } else {
        Write-Warn "keystore.p12 verification inconclusive - check manually"
    }
}

# ==============================================================================
# STEP 5-6: IDOL Find + DataAdmin + MMAP
# ==============================================================================
function Step-FindKeystores {
    Write-Section "Step 5-6 - IDOL Find KeyStores"
    Build-Keystore -Service "idol-find"
}

function Step-DataadminKeystores {
    Write-Section "Step 6b - IDOL DataAdmin KeyStores"
    Build-Keystore -Service "idol-dataadmin"
}

function Step-MmapKeystore {
    Write-Section "Step 6c - MMAP KeyStore (self-signed)"
    $p12 = Join-Path $CertsDir "idol-mmap-app.pkcs12"
    $dname = "CN=mmap, OU=$($script:CertOu), O=$($script:CertOrg), L=$($script:CertCity), ST=$($script:CertState), C=$($script:CertCountry)"

    Invoke-ExternalTool -Exe "keytool" -ErrorContext "MMAP keystore generation" -ToolArgs @(
        "-genkeypair", "-alias", "mmap", "-keyalg", "RSA", "-keysize", "2048",
        "-storetype", "PKCS12", "-keystore", $p12, "-validity", "365",
        "-storepass", $script:KeystorePass, "-keypass", $script:KeystorePass,
        "-dname", $dname
    ) | Out-Null

    Write-Ok "mmap.pkcs12 created (self-signed, validity 365 days)"
}

# ==============================================================================
# STEP 7: Cleanup
# ==============================================================================
function Step-Cleanup {
    Write-Section "Step 7 - Organising CA Database Files"
    $issued = Join-Path $CaDir "issued"
    Get-ChildItem (Join-Path $CaDir "index.txt*") -ErrorAction SilentlyContinue | Move-Item -Destination $issued -Force
    Get-ChildItem (Join-Path $CaDir "serial*")    -ErrorAction SilentlyContinue | Move-Item -Destination $issued -Force
    Get-ChildItem (Join-Path $CaDir "crlnumber*") -ErrorAction SilentlyContinue | Move-Item -Destination $issued -Force
    Write-Ok "CA database archived to $issued"
}

# ==============================================================================
# STEP 8: Deploy
# ==============================================================================
function Step-Deploy {
    Write-Section "Step 8 - Deploying SSL Artefacts"
    $targets = @($DeployBasicIdol, $DeployDataAdmin, $DeployRichMedia, $DeployLicenseServer)
    foreach ($target in $targets) {
        if (Test-Path $target) {
            Remove-Item -Path $target -Recurse -Force -ErrorAction SilentlyContinue
        }
        $parent = Split-Path $target -Parent
        if (-not (Test-Path $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        Copy-Item -Path $SslRoot -Destination $target -Recurse -Force
        Write-Ok "-> $target"
    }
}

# ==============================================================================
# HELPER: Restrict private key ACL
# ==============================================================================
function Restrict-PrivateKey {
    param([string]$Path)
    try {
        $acl = Get-Acl $Path
        $acl.SetAccessRuleProtection($true, $false)
        $identity = if ($SslServiceUser) { $SslServiceUser } else { [System.Security.Principal.WindowsIdentity]::GetCurrent().Name }
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($identity, "FullControl", "Allow")
        $acl.SetAccessRule($rule)
        if ($SslServiceGroup) {
            $groupRule = New-Object System.Security.AccessControl.FileSystemAccessRule($SslServiceGroup, "Read", "Allow")
            $acl.AddAccessRule($groupRule)
        }
        Set-Acl -Path $Path -AclObject $acl
    } catch {
        Write-Warn "Could not restrict ACL on $Path : $($_.Exception.Message)"
    }
}

# ==============================================================================
# STEP 8b: Fix permissions (Windows ACLs)
# ==============================================================================
function Step-FixPermissions {
    Write-Section "Step 8b - Fixing SSL File Permissions (Windows ACLs)"

    $allDirs = @($SslRoot)
    foreach ($t in @($DeployBasicIdol, $DeployDataAdmin, $DeployRichMedia, $DeployLicenseServer)) {
        if (Test-Path $t) { $allDirs += $t }
    }

    foreach ($dir in $allDirs) {
        Get-ChildItem -Path $dir -Recurse -Filter "*.key.pem" -ErrorAction SilentlyContinue | ForEach-Object {
            Restrict-PrivateKey -Path $_.FullName
        }
        Write-Ok "${dir}: *.key.pem -> restricted ACL"

        Get-ChildItem -Path $dir -Recurse -Include "*.cert.pem","*-fullchain.cert.pem","*.pkcs12","*.p12","*.jks","*.crt" -ErrorAction SilentlyContinue | ForEach-Object {
            try {
                $acl = Get-Acl $_.FullName
                $acl.SetAccessRuleProtection($false, $true)
                Set-Acl -Path $_.FullName -AclObject $acl -ErrorAction SilentlyContinue
            } catch {}
        }
        Write-Ok "${dir}: certs / keystores -> standard ACL"
    }

    if (-not $SslServiceUser -and -not $SslServiceGroup) {
        Write-Warn "SSL_SERVICE_USER / SSL_SERVICE_GROUP not set - private keys owned by current user only."
        Write-Warn "Re-run with: `$env:SSL_SERVICE_USER='idol'; `$env:SSL_SERVICE_GROUP='idol'; .\Generate-SSL.ps1"
    }
}

# ==============================================================================
# STEP 9: Export Root CA
# ==============================================================================
function Step-ExportRootCa {
    Write-Section "Step 9 - Exporting Root CA for Client Distribution"
    $src = Join-Path $CertsDir "ca.cert.pem"
    $dst = Join-Path $SslRoot "idol-root-ca.crt"
    Copy-Item $src $dst -Force
    Write-Ok "Root CA (distribute to clients): $dst"
}

# ==============================================================================
# STEP 10: Install into Windows Trusted Root store
# ==============================================================================
function Step-InstallTrustStore {
    Write-Section "Step 10 - Installing CAs into Windows Trusted Root Store"

    $rootPem  = Join-Path $CertsDir "ca.cert.pem"
    $interPem = Join-Path $CertsDir "intermediate.cert.pem"
    $rootCer  = Join-Path $env:TEMP "idol-root-ca.cer"
    $interCer = Join-Path $env:TEMP "idol-intermediate-ca.cer"

    # Convert PEM -> DER for Import-Certificate
    Invoke-OpenSsl -ErrorContext "Root CA PEM->DER conversion" -OpenSslArgs @("x509", "-in", $rootPem,  "-outform", "DER", "-out", $rootCer)  | Out-Null
    Invoke-OpenSsl -ErrorContext "Intermediate CA PEM->DER conversion" -OpenSslArgs @("x509", "-in", $interPem, "-outform", "DER", "-out", $interCer) | Out-Null

    try {
        Import-Certificate -FilePath $rootCer  -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
        Write-Ok "Root CA         -> Cert:\LocalMachine\Root"
    } catch {
        Write-Err "Failed to import Root CA: $($_.Exception.Message)"
    }

    try {
        Import-Certificate -FilePath $interCer -CertStoreLocation Cert:\LocalMachine\CA | Out-Null
        Write-Ok "Intermediate CA -> Cert:\LocalMachine\CA"
    } catch {
        Write-Err "Failed to import Intermediate CA: $($_.Exception.Message)"
    }

    # Also try certutil for broader compatibility
    & certutil -addstore -f "Root" $rootCer  2>$null | Out-Null
    & certutil -addstore -f "CA"   $interCer 2>$null | Out-Null

    Remove-Item $rootCer, $interCer -Force -ErrorAction SilentlyContinue

    Write-Ok "Windows certificate store updated (LocalMachine\Root + CA)"
}

# ==============================================================================
# HEALTH CHECK (optional)
# ==============================================================================
function Invoke-HealthCheck {
    if (-not (Get-Command curl -ErrorAction SilentlyContinue) -and -not (Get-Command Invoke-WebRequest -ErrorAction SilentlyContinue)) {
        Write-Warn "No HTTP client available - skipping health check"
        return
    }

    $script:HealthFailCount = 0
    $pass = 0

    foreach ($entry in $HealthChecks) {
        $url = "https://${ExternalHostname}:$($entry.Port)/a=GetStatus"
        try {
            $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
            if ($resp.Content -match "<response>SUCCESS</response>") {
                Write-Ok "$($entry.Name) (port $($entry.Port))"
                $pass++
            } else {
                Write-Fail "$($entry.Name) (port $($entry.Port))"
                $script:HealthFailCount++
            }
        } catch {
            Write-Fail "$($entry.Name) (port $($entry.Port))"
            $script:HealthFailCount++
        }
    }

    Write-Host ""
    if ($script:HealthFailCount -eq 0) {
        Write-Ok "All $pass/$($HealthChecks.Count) services responding over verified HTTPS."
    } else {
        Write-Warn "$pass/$($HealthChecks.Count) passed, $($script:HealthFailCount) failed."
    }
}

# ==============================================================================
# SUMMARY
# ==============================================================================
function Show-Summary {
    Write-Section "Summary Report"

    Write-Host "  Service Certificates ($($Services.Count) total):" -ForegroundColor White
    foreach ($service in $Services) {
        $status = $script:CertStatus[$service]
        if ($status -eq "PASS") {
            Write-Ok $service
        } else {
            Write-Fail $service
        }
    }

    Write-Host ""
    Write-Host "  Keystore Files:" -ForegroundColor White
    $ksFiles = @(
        (Join-Path $CertsDir "intermediate.pkcs12"),
        (Join-Path $CertsDir "idol-find.pkcs12"),
        (Join-Path $CertsDir "idol-find.jks"),
        (Join-Path $CertsDir "idol-dataadmin.pkcs12"),
        (Join-Path $CertsDir "idol-dataadmin.jks"),
        (Join-Path $NifiDir  "keystore.p12"),
        (Join-Path $NifiDir  "truststore.p12")
    )
    foreach ($ks in $ksFiles) {
        if (Test-Path $ks) {
            Write-Ok (Split-Path $ks -Leaf)
        } else {
            Write-Fail "$(Split-Path $ks -Leaf) - MISSING"
        }
    }

    Write-Host ""
    if ($script:CertFailures -eq 0) {
        Write-Host "  [SUCCESS] All $($Services.Count) certificates verified successfully." -ForegroundColor Green
    } else {
        Write-Host "  [ERROR] $($script:CertFailures) of $($Services.Count) certificate(s) FAILED." -ForegroundColor Red
    }

    $border = "=" * 65
    Write-Host ""
    Write-Host $border -ForegroundColor Cyan
    Write-Host "  Credentials and Key Files" -ForegroundColor White
    Write-Host $border -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  KeyStore password:    $($script:KeystorePass)" -ForegroundColor Yellow
    Write-Host "  TrustStore password:  $($script:TruststorePass)" -ForegroundColor Yellow
    Write-Host "  Saved to:             $SslPasswordsEnv" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Root CA (distribute):  $(Join-Path $SslRoot 'idol-root-ca.crt')" -ForegroundColor DarkYellow
    Write-Host "  CA Chain:              $(Join-Path $CertsDir 'ca-chain.cert.pem')" -ForegroundColor DarkYellow
    Write-Host "  Full log:              $script:LogFile" -ForegroundColor DarkYellow
    Write-Host ""
    Write-Host "  Expiry dates:" -ForegroundColor White
    Write-Host "    Root CA:        ~$((Get-Date).AddDays($DaysRoot).ToString('yyyy-MM-dd'))"
    Write-Host "    Intermediate:   ~$((Get-Date).AddDays($DaysIntermediate).ToString('yyyy-MM-dd'))"
    Write-Host "    Service certs:  ~$((Get-Date).AddDays($DaysCert).ToString('yyyy-MM-dd'))"
    Write-Host ""
    if ($script:ExtraIpSans.Count -gt 0) {
        Write-Host "  Extra IP SANs embedded in all certs:" -ForegroundColor White
        foreach ($ip in $script:ExtraIpSans) {
            Write-Host "    $ip" -ForegroundColor Cyan
        }
        Write-Host ""
    }
    Write-Host "  WARNING: Update passwords and DN values for production deployments." -ForegroundColor Red
    Write-Host ""
    Write-Host $border -ForegroundColor Cyan
    Write-Host ""
}

# ==============================================================================
# MAIN
# ==============================================================================
function Main {
    if ($Help) { Show-Usage }

    Setup-Logging

    Write-Section "IDOL SSL Certificate Generation (Windows Server)"
    Write-Info "Repo root:   $RepoRoot"
    Write-Info "Script dir:  $ScriptDir"
    Write-Info "SSL output:  $SslRoot"
    if ($script:AutoMode) { Write-Info "Mode: non-interactive (-Auto)" }

    Test-Prerequisites
    Confirm-Regeneration

    if (-not $script:SkipGeneration) {
        Collect-SubjectInfo
        Setup-Passwords

        Initialize-CaDirectory
        Step-RootCa
        Step-IntermediateCa
        Step-ServiceCerts
        Step-NifiKeystores
        Step-FindKeystores
        Step-DataadminKeystores
        Step-MmapKeystore
        Step-Cleanup
    } else {
        Write-Info "Loading existing passwords from $SslPasswordsEnv"
        if (Test-Path $SslPasswordsEnv) {
            . $SslPasswordsEnv
            $script:KeystorePass   = $env:IDOL_CERT_KEYSTORE_PASS
            $script:TruststorePass = $env:IDOL_CERT_TRUSTSTORE_PASS
        } else {
            Write-Warn "Password file not found - keystore operations may fail."
        }
    }

    Step-Deploy
    Step-FixPermissions
    Step-ExportRootCa
    Step-InstallTrustStore

    Show-Summary
    Write-Section "Complete"
}

try {
    Main
} finally {
    Stop-Logging
}
