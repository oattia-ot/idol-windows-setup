#requires -Version 5.1
<#
.SYNOPSIS
    Apply text replacements from tools/replacements.json to config files,
    then sync target ports into config/my-config.json (and default-config.json).

.DESCRIPTION
    Windows-native replacement for the Lua helper. No Lua required.

    1. Reads tools/replacements.json (or -ReplacementsPath).
    2. For each entry: replace "from" -> "to" in the listed file (literal match).
       Optional "section" property restricts the replacement to the body of a
       named [Section] (INI-style). If "from" is not found (or the section is
       missing), that individual replacement is skipped without error.
       Creates a .bak backup only when the file content actually changes.
    3. Collects target ports from the "to" values and writes them into
       config/my-config.json and config/default-config.json so the installer
       and configuration dashboard stay in sync.

.PARAMETER ReplacementsPath
    Path to the JSON file. Default: <scriptDir>/replacements.json

.PARAMETER SetupRoot
    Project root (folder that contains "config"). Default: parent of tools/

.PARAMETER DryRun
    Show what would change without writing files.

.EXAMPLE
    .\tools\Update-ConfigFiles.ps1
    .\tools\Update-ConfigFiles.ps1 -DryRun
    .\Install-KD.ps1 -Mode UpdateConfigFiles
#>
[CmdletBinding()]
param(
    [string]$ReplacementsPath = '',
    [string]$SetupRoot = '',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Write-Ok   { param([string]$m) Write-Host "  [OK]    $m" -ForegroundColor Green }
function Write-Skip { param([string]$m) Write-Host "  [SKIP]  $m" -ForegroundColor DarkGray }
function Write-Err  { param([string]$m) Write-Host "  [ERROR] $m" -ForegroundColor Red }
function Write-Info { param([string]$m) Write-Host "  $m" -ForegroundColor Cyan }

# Resolve paths
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $SetupRoot) {
    $SetupRoot = Split-Path -Parent $scriptDir   # tools/ -> project root
}
$SetupRoot = (Resolve-Path -LiteralPath $SetupRoot).Path

if (-not $ReplacementsPath) {
    $ReplacementsPath = Join-Path $scriptDir 'replacements.json'
}
if (-not (Test-Path -LiteralPath $ReplacementsPath)) {
    Write-Err "Replacements file not found: $ReplacementsPath"
    exit 1
}

Write-Host ''
Write-Host '  Update configuration files (ports, API key, etc.)' -ForegroundColor Cyan
Write-Host "  Setup root : $SetupRoot" -ForegroundColor DarkGray
Write-Host "  JSON       : $ReplacementsPath" -ForegroundColor DarkGray
if ($DryRun) { Write-Host '  Mode       : DRY-RUN (no writes)' -ForegroundColor Yellow }
Write-Host ''

# ---------- load JSON ----------
try {
    $raw = Get-Content -LiteralPath $ReplacementsPath -Raw -Encoding UTF8
    $entries = $raw | ConvertFrom-Json
} catch {
    Write-Err "Failed to parse JSON: $_"
    exit 1
}

if (-not $entries -or $entries.Count -eq 0) {
    Write-Err 'No entries in replacements JSON.'
    exit 1
}

Write-Info ("Loaded {0} file entries" -f $entries.Count)
Write-Host ''

# ---------- apply file replacements ----------
$okCount = 0
$changeCount = 0

foreach ($entry in $entries) {
    $rel = $entry.file
    if (-not $rel) { continue }
    $path = Join-Path $SetupRoot ($rel -replace '/', '\')

    if (-not (Test-Path -LiteralPath $path)) {
        Write-Err "File not found: $path"
        continue
    }

    try {
        $content = [System.IO.File]::ReadAllText($path)
    } catch {
        Write-Err "Cannot read $path : $_"
        continue
    }

    $original = $content
    $total = 0
    $skippedReps = 0

    foreach ($rep in @($entry.replacements)) {
        $from = [string]$rep.from
        $to   = [string]$rep.to
        if ([string]::IsNullOrEmpty($from)) { continue }

        $section = $null
        if ($rep.PSObject.Properties.Name -contains 'section') {
            $section = [string]$rep.section
            if ([string]::IsNullOrWhiteSpace($section)) { $section = $null }
        }

        $n = 0

        if ($section) {
            # Section-scoped replace: only touch matches that fall inside [Section] ... next [
            # NOTE: [regex]::Match has no (input, pattern, startat) overload — the 3rd
            # static argument is RegexOptions. Use an instance Match(..., startat) instead.
            $secPattern = "(?m)^\s*\[$([regex]::Escape($section))\]\s*"
            $secMatch = [regex]::Match($content, $secPattern)
            if (-not $secMatch.Success) {
                # Section not present -> skip this replacement
                $skippedReps++
                continue
            }
            $secStart = $secMatch.Index
            $afterHeader = $secStart + $secMatch.Length
            $nextSecRx = [regex]::new('(?m)^\s*\[[^\]]+\]')
            $nextSec = $nextSecRx.Match($content, $afterHeader)
            $secEnd = if ($nextSec.Success) { $nextSec.Index } else { $content.Length }

            # Section range includes the [Header] line through (but not including) the next [
            $before = $content.Substring(0, $secStart)
            $secText = $content.Substring($secStart, $secEnd - $secStart)
            $after  = $content.Substring($secEnd)

            if ($secText.IndexOf($from) -lt 0) {
                # "from" not found inside this section -> skip
                $skippedReps++
                continue
            }

            $idx = 0
            while (($idx = $secText.IndexOf($from, $idx)) -ge 0) {
                $secText = $secText.Remove($idx, $from.Length).Insert($idx, $to)
                $idx += $to.Length
                $n++
            }
            $content = $before + $secText + $after
        }
        else {
            # Global literal replace (no section constraint)
            if ($content.IndexOf($from) -lt 0) {
                # "from" does not exist anywhere in the file -> skip this replacement
                $skippedReps++
                continue
            }
            $idx = 0
            while (($idx = $content.IndexOf($from, $idx)) -ge 0) {
                $content = $content.Remove($idx, $from.Length).Insert($idx, $to)
                $idx += $to.Length
                $n++
            }
        }
        $total += $n
    }

    if ($content -eq $original) {
        $skipMsg = if ($skippedReps -gt 0) { " (no changes; $skippedReps replacement(s) skipped - from not found)" } else { ' (no changes)' }
        Write-Skip "$rel$skipMsg"
        $okCount++
        continue
    }

    if ($DryRun) {
        $extra = if ($skippedReps -gt 0) { "  [$skippedReps skipped]" } else { '' }
        Write-Host ("  [DRY]   {0}  ({1} replacement(s)){2}" -f $rel, $total, $extra) -ForegroundColor Yellow
        $okCount++
        $changeCount++
        continue
    }

    $bak = $path + '.bak'
    try {
        if (Test-Path -LiteralPath $bak) { Remove-Item -LiteralPath $bak -Force }
        Rename-Item -LiteralPath $path -NewName ([IO.Path]::GetFileName($bak))
        [System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))
        $extra = if ($skippedReps -gt 0) { "  [$skippedReps skipped]" } else { '' }
        Write-Ok ("{0}  ({1} replacement(s)){2}" -f $rel, $total, $extra)
        $okCount++
        $changeCount++
    } catch {
        Write-Err "Cannot write $path : $_"
        if (Test-Path -LiteralPath $bak) {
            Rename-Item -LiteralPath $bak -NewName ([IO.Path]::GetFileName($path)) -ErrorAction SilentlyContinue
        }
    }
}

Write-Host ''
Write-Info ("File replacements finished. {0} / {1} entries OK  ({2} changed)." -f $okCount, $entries.Count, $changeCount)

# ---------- collect target ports from "to" values ----------
# Map: relative file + key name -> Ports key in my-config.json
$portMap = @{
    'config/cfg/content/content.cfg|Port'                        = 'Content'
    'config/cfg/category/category.cfg|Port'                      = 'Category'
    'config/cfg/community/community.cfg|Port'                    = 'Community'
    'config/cfg/agentstore/agentstore.cfg|Port'                  = 'Agentstore'
    'config/cfg/qmsagentstore/agentstore.cfg|Port'               = 'QMSAgentStore'
    'config/cfg/answerbank-agentstore/agentstore.cfg|Port'       = 'AnswerBankAgentStore'
    'config/cfg/conversation-agentstore/agentstore.cfg|Port'     = 'ConversationAgentStore'
    'config/cfg/qms/qms.cfg|Port'                                = 'QMS'
    'config/cfg/answerserver/answerserver.cfg|Port'              = 'AnswerServer'
    'config/cfg/statsserver/statsserver.cfg|Port'                = 'StatsServer'
    'config/cfg/view/view.cfg|Port'                              = 'View'
    'config/cfg/agentstore/idol.common.cfg|LicenseServerACIPort' = 'LicenseServer'
    'config/cfg/content/idol.common.cfg|LicenseServerACIPort'    = 'LicenseServer'
}

# Preferred values when a file has multiple Port= lines
$preferred = @{
    QMS      = '16000'
    Category = '9020'
    Community= '9030'
}

function Get-PortNumber([string]$s) {
    if ($s -match '=(\d+)\s*$') { return $Matches[1] }
    if ($s -match '(\d+)\s*$')  { return $Matches[1] }
    return $null
}

$ports = @{}
$licensePort = $null

foreach ($entry in $entries) {
    $file = [string]$entry.file
    foreach ($rep in @($entry.replacements)) {
        $to = [string]$rep.to
        if ($to -notmatch '^([\w]+)\s*=') { continue }
        $key = $Matches[1]
        $num = Get-PortNumber $to
        if (-not $num) { continue }

        $mapKey = "$file|$key"
        $portsKey = $portMap[$mapKey]
        if ($portsKey) {
            if (-not $ports.ContainsKey($portsKey)) {
                $ports[$portsKey] = $num
            } elseif ($preferred.ContainsKey($portsKey) -and $num -eq $preferred[$portsKey]) {
                $ports[$portsKey] = $num
            }
        }
        if ($key -eq 'LicenseServerACIPort') {
            $licensePort = $num
            $ports['LicenseServer'] = $num
        }
    }
}

# Fill sensible defaults for any missing keys
$defaults = @{
    Content                 = '9100'
    Category                = '9020'
    Community               = '9030'
    Agentstore              = '9050'
    QMSAgentStore           = '9150'
    AnswerBankAgentStore    = '9450'
    ConversationAgentStore  = '9550'
    QMS                     = '16000'
    AnswerServer            = '12000'
    StatsServer             = '19870'
    View                    = '9080'
    LicenseServer           = '20000'
    NiFi                    = '8443'
    Find                    = '8080'
}
foreach ($k in $defaults.Keys) {
    if (-not $ports.ContainsKey($k)) { $ports[$k] = $defaults[$k] }
}
if (-not $licensePort) { $licensePort = $ports['LicenseServer'] }

Write-Host ''
Write-Info 'Target Ports:'
$ports.GetEnumerator() | Sort-Object Name | ForEach-Object {
    Write-Host ("    {0,-26} = {1}" -f $_.Key, $_.Value) -ForegroundColor DarkGray
}
Write-Host ("    {0,-26} = {1}" -f 'LicensePort (top-level)', $licensePort) -ForegroundColor DarkGray

# ---------- update my-config.json / default-config.json ----------
function Update-ConfigJson {
    param([string]$ConfigPath, [hashtable]$PortsTable, [string]$LicPort)

    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        Write-Skip "$(Split-Path -Leaf $ConfigPath)  (file not found)"
        return $false
    }

    try {
        $cfg = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Write-Err "Cannot parse $ConfigPath : $_"
        return $false
    }

    $changed = $false
    if (-not $cfg.Ports) {
        $cfg | Add-Member -NotePropertyName Ports -NotePropertyValue ([pscustomobject]@{}) -Force
    }

    # Convert Ports to a mutable hashtable-like object
    $portsObj = @{}
    if ($cfg.Ports) {
        $cfg.Ports.PSObject.Properties | ForEach-Object { $portsObj[$_.Name] = [string]$_.Value }
    }
    foreach ($k in $PortsTable.Keys) {
        $newVal = [string]$PortsTable[$k]
        if (-not $portsObj.ContainsKey($k) -or $portsObj[$k] -ne $newVal) {
            $portsObj[$k] = $newVal
            $changed = $true
        }
    }
    # Rebuild Ports as ordered object for stable JSON
    $newPorts = [ordered]@{}
    foreach ($k in ($portsObj.Keys | Sort-Object)) {
        $newPorts[$k] = $portsObj[$k]
    }
    $cfg.Ports = [pscustomobject]$newPorts

    if ($LicPort -and [string]$cfg.LicensePort -ne [string]$LicPort) {
        $cfg.LicensePort = [string]$LicPort
        $changed = $true
    }

    if (-not $changed) {
        Write-Skip "$(Split-Path -Leaf $ConfigPath)  (ports already match targets)"
        return $false
    }

    if ($DryRun) {
        Write-Host ("  [DRY]   Would update ports in {0}" -f (Split-Path -Leaf $ConfigPath)) -ForegroundColor Yellow
        return $true
    }

    $bak = $ConfigPath + '.bak'
    try {
        if (Test-Path -LiteralPath $bak) { Remove-Item -LiteralPath $bak -Force }
        Copy-Item -LiteralPath $ConfigPath -Destination $bak -Force
        $json = $cfg | ConvertTo-Json -Depth 20
        [System.IO.File]::WriteAllText($ConfigPath, $json, [System.Text.UTF8Encoding]::new($false))
        Write-Ok "Updated ports in $(Split-Path -Leaf $ConfigPath)"
        return $true
    } catch {
        Write-Err "Failed to write $ConfigPath : $_"
        return $false
    }
}

Write-Host ''
Write-Info 'Updating config JSON files with target ports ...'
$n = 0
foreach ($name in @('my-config.json', 'default-config.json')) {
    $p = Join-Path $SetupRoot "config\$name"
    if (Update-ConfigJson -ConfigPath $p -PortsTable $ports -LicPort $licensePort) {
        $n++
    }
}

Write-Host ''
Write-Host ("  Done. {0} config file(s) updated with target ports." -f $n) -ForegroundColor Green
if (-not $DryRun) {
    Write-Host '  Backups created as *.bak next to every modified file.' -ForegroundColor DarkGray
}
Write-Host ''
exit 0
