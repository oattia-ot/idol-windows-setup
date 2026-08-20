<#
.SYNOPSIS
    Remove unnecessary files, folders, and optionally Windows services defined
    in config\cleanup.json.

.DESCRIPTION
    Reads config\cleanup.json (or -CleanupConfig) and deletes every listed target.

    Scope is controlled by Options in the JSON (and can be overridden by switches):
      DeleteToolkitTargets   - ssl\, caches, password sidecars under the toolkit
      DeleteBasePathTargets  - logs, state files under BasePath
      DeleteWindowsServices  - stop + sc delete listed KD-* services (default false)
      DeleteEntireBasePath   - remove the whole BasePath tree (default false; destructive)

.PARAMETER CleanupConfig
    Path to cleanup.json. Default: <toolkit>\config\cleanup.json

.PARAMETER BasePath
    Override BasePath from the JSON (e.g. C:\KnowledgeDiscovery\26.2).

.PARAMETER DeleteServices
    Also stop and delete Windows services listed in cleanup.json
    (overrides Options.DeleteWindowsServices = true).

.PARAMETER DeleteBasePath
    Also delete the entire BasePath folder after other cleanup
    (overrides Options.DeleteEntireBasePath = true).

.PARAMETER DryRun
    Preview only; do not delete.

.PARAMETER Force
    Skip interactive confirmation.

.EXAMPLE
    .\Clean-Unnecessary.ps1

.EXAMPLE
    .\Clean-Unnecessary.ps1 -BasePath "C:\KnowledgeDiscovery\26.2" -DeleteServices -Force

.EXAMPLE
    .\Clean-Unnecessary.ps1 -DryRun
#>

#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$CleanupConfig,
    [string]$BasePath,
    [switch]$DeleteServices,
    [switch]$DeleteBasePath,
    [switch]$DryRun,
    [switch]$Force
)

$ErrorActionPreference = "Continue"
$toolkitRoot = $PSScriptRoot
$deleted = [System.Collections.Generic.List[string]]::new()
$failed  = [System.Collections.Generic.List[string]]::new()
$skipped = [System.Collections.Generic.List[string]]::new()

function Write-Banner {
    param([string]$Text)
    Write-Host ""
    Write-Host ("=" * 60) -ForegroundColor Cyan
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host ("=" * 60) -ForegroundColor Cyan
}

function Remove-Target {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$Label = ""
    )
    # Always show the real path (e.g. C:\KD-Setup\idol-windows-setup\ssl)
    $full = $Path
    try {
        if (Test-Path -LiteralPath $Path) {
            $full = (Resolve-Path -LiteralPath $Path).Path
        }
    } catch { }

    $tag = if ($Label) { "$Label  ($full)" } else { $full }

    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Host "  [skip] Not found: $tag" -ForegroundColor Gray
        $skipped.Add($Path) | Out-Null
        return
    }

    if ($DryRun) {
        Write-Host "  [DryRun] Would remove: $tag" -ForegroundColor Yellow
        $skipped.Add("DRYRUN:$Path") | Out-Null
        return
    }

    try {
        cmd.exe /c "attrib -R -S -H `"$full\*`" /S /D" 2>$null | Out-Null
        Get-ChildItem -LiteralPath $full -Recurse -Force -ErrorAction SilentlyContinue |
            ForEach-Object { try { $_.Attributes = 'Normal' } catch { } }

        Remove-Item -LiteralPath $full -Recurse -Force -ErrorAction Stop

        if (Test-Path -LiteralPath $full) {
            cmd.exe /c "rmdir /s /q `"$full`"" 2>$null | Out-Null
        }

        if (Test-Path -LiteralPath $full) {
            Write-Host "  [FAIL] Still present: $tag" -ForegroundColor Red
            Write-Host "         Locked file? Close Explorer windows under ssl\ and re-run as Administrator." -ForegroundColor Yellow
            $failed.Add($full) | Out-Null
        } else {
            Write-Host "  [OK] Removed: $tag" -ForegroundColor Green
            $deleted.Add($full) | Out-Null
        }
    } catch {
        cmd.exe /c "rmdir /s /q `"$full`"" 2>$null | Out-Null
        if (-not (Test-Path -LiteralPath $full)) {
            Write-Host "  [OK] Removed (cmd fallback): $tag" -ForegroundColor Green
            $deleted.Add($full) | Out-Null
        } else {
            Write-Host "  [FAIL] $tag - $($_.Exception.Message)" -ForegroundColor Red
            $failed.Add("$full :: $($_.Exception.Message)") | Out-Null
        }
    }
}

function Remove-ServiceTarget {
    param(
        [Parameter(Mandatory = $true)][string]$ServiceName,
        [int]$TimeoutSeconds = 45
    )
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $svc) {
        Write-Host "  [skip] Service not present: $ServiceName" -ForegroundColor Gray
        $skipped.Add("SVC:$ServiceName") | Out-Null
        return
    }

    if ($DryRun) {
        Write-Host "  [DryRun] Would stop+delete service: $ServiceName (Status=$($svc.Status))" -ForegroundColor Yellow
        $skipped.Add("DRYRUN:SVC:$ServiceName") | Out-Null
        return
    }

    try {
        if ($svc.Status -ne 'Stopped') {
            Write-Host "  Stopping $ServiceName ..." -NoNewline
            Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
            $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
            while ((Get-Date) -lt $deadline) {
                $s = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
                if (-not $s -or $s.Status -eq 'Stopped') { break }
                Start-Sleep -Milliseconds 500
            }
            $s = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
            if ($s -and $s.Status -ne 'Stopped') {
                $p = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue
                if ($p -and $p.ProcessId -gt 0) {
                    taskkill /F /PID $p.ProcessId 2>$null | Out-Null
                    Start-Sleep -Seconds 1
                }
            }
            Write-Host " stopped" -ForegroundColor Green
        }

        $null = & sc.exe delete $ServiceName 2>&1
        Start-Sleep -Milliseconds 500
        if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
            Write-Host "  [FAIL] Could not delete service $ServiceName" -ForegroundColor Red
            $failed.Add("SVC:$ServiceName") | Out-Null
        } else {
            Write-Host "  [OK] Deleted service: $ServiceName" -ForegroundColor Green
            $deleted.Add("SVC:$ServiceName") | Out-Null
        }
    } catch {
        Write-Host "  [FAIL] Service $ServiceName - $($_.Exception.Message)" -ForegroundColor Red
        $failed.Add("SVC:$ServiceName :: $($_.Exception.Message)") | Out-Null
    }
}

# -------------------------------------------------------------------------
# Load cleanup.json
# -------------------------------------------------------------------------
if (-not $CleanupConfig) {
    $CleanupConfig = Join-Path $toolkitRoot "config\cleanup.json"
}
if (-not (Test-Path -LiteralPath $CleanupConfig)) {
    Write-Host "FATAL: cleanup config not found: $CleanupConfig" -ForegroundColor Red
    Write-Host "Expected config\cleanup.json under the toolkit folder." -ForegroundColor Red
    exit 1
}

try {
    $cfg = Get-Content -LiteralPath $CleanupConfig -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    Write-Host "FATAL: Failed to parse cleanup.json: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

$bp = if ($BasePath) { $BasePath } elseif ($cfg.BasePath) { [string]$cfg.BasePath } else { $null }
$opt = $cfg.Options
if (-not $opt) { $opt = [pscustomobject]@{} }

$doToolkit  = if ($null -ne $opt.DeleteToolkitTargets)  { [bool]$opt.DeleteToolkitTargets }  else { $true }
$doBasePath = if ($null -ne $opt.DeleteBasePathTargets) { [bool]$opt.DeleteBasePathTargets } else { $true }
$doServices = if ($DeleteServices) { $true } elseif ($null -ne $opt.DeleteWindowsServices) { [bool]$opt.DeleteWindowsServices } else { $false }
$doWipeBase = if ($DeleteBasePath) { $true } elseif ($null -ne $opt.DeleteEntireBasePath) { [bool]$opt.DeleteEntireBasePath } else { $false }
$svcTimeout = if ($opt.ServiceStopTimeoutSeconds) { [int]$opt.ServiceStopTimeoutSeconds } else { 45 }

Write-Banner "KD Clean Unnecessary (from cleanup.json)"
Write-Host "  Config   : $CleanupConfig"
Write-Host "  Toolkit  : $toolkitRoot"
if ($bp) { Write-Host "  BasePath : $bp" }
Write-Host "  Toolkit targets  : $doToolkit"
Write-Host "  BasePath targets : $doBasePath"
Write-Host "  Windows services : $doServices"
Write-Host "  Wipe BasePath    : $doWipeBase"
if ($DryRun) { Write-Host "  Mode     : DryRun (no deletes)" -ForegroundColor Yellow }

if (-not $Force -and -not $DryRun) {
    Write-Host ""
    Write-Host "Targets are defined in config\cleanup.json." -ForegroundColor Yellow
    if ($doServices) {
        Write-Host "Windows services WILL be stopped and deleted." -ForegroundColor Yellow
    }
    if ($doWipeBase) {
        Write-Host "ENTIRE BasePath will be deleted." -ForegroundColor Red
    }
    Write-Host -ForegroundColor Yellow -NoNewline "Type YES to continue "
    $answer = Read-Host
    if ($answer -ne "YES") {
        Write-Host "Aborted." -ForegroundColor Yellow
        exit 0
    }
}

# -------------------------------------------------------------------------
# 1) Toolkit folders / files / globs
# -------------------------------------------------------------------------
if ($doToolkit) {
    # Always attempt the known SSL output folder first (most common leftover)
    Write-Host ""
    Write-Host "--- SSL folder (forced) ---" -ForegroundColor Cyan
    $sslForced = Join-Path $toolkitRoot "ssl"
    Write-Host "  Looking for: $sslForced"
    Remove-Target -Path $sslForced -Label "toolkit\ssl"

    Write-Host ""
    Write-Host "--- Toolkit folders ---" -ForegroundColor Cyan
    foreach ($rel in @($cfg.ToolkitFolders)) {
        if (-not $rel) { continue }
        if ($rel -eq "ssl") { continue }  # already handled above
        $p = Join-Path $toolkitRoot $rel
        Remove-Target -Path $p -Label "toolkit\$rel"
    }

    Write-Host ""
    Write-Host "--- Toolkit files ---" -ForegroundColor Cyan
    foreach ($rel in @($cfg.ToolkitFiles)) {
        if (-not $rel) { continue }
        $p = Join-Path $toolkitRoot $rel
        Remove-Target -Path $p -Label "toolkit\$rel"
    }

    Write-Host ""
    Write-Host "--- Toolkit Python cache / junk ---" -ForegroundColor Cyan
    Get-ChildItem -Path $toolkitRoot -Directory -Recurse -Force -Filter "__pycache__" -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Target -Path $_.FullName -Label $_.FullName.Replace($toolkitRoot, ".") }
    Get-ChildItem -Path $toolkitRoot -File -Recurse -Force -Include "*.pyc","*.pyo" -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Target -Path $_.FullName -Label $_.FullName.Replace($toolkitRoot, ".") }
    foreach ($name in @("Thumbs.db", "desktop.ini", ".DS_Store")) {
        Get-ChildItem -Path $toolkitRoot -File -Recurse -Force -Filter $name -ErrorAction SilentlyContinue |
            ForEach-Object { Remove-Target -Path $_.FullName -Label $_.Name }
    }
    Get-ChildItem -Path $toolkitRoot -File -Force -Filter "*.log" -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Target -Path $_.FullName -Label $_.Name }
}

# -------------------------------------------------------------------------
# 2) BasePath folders / files / globs
# -------------------------------------------------------------------------
if ($doBasePath -and $bp) {
    if (-not (Test-Path -LiteralPath $bp)) {
        Write-Host ""
        Write-Host "BasePath not found (skip targets): $bp" -ForegroundColor Yellow
    } else {
        Write-Host ""
        Write-Host "--- BasePath folders ---" -ForegroundColor Cyan
        foreach ($rel in @($cfg.BasePathFolders)) {
            if (-not $rel) { continue }
            $p = Join-Path $bp $rel
            Remove-Target -Path $p -Label "BasePath\$rel"
        }

        Write-Host ""
        Write-Host "--- BasePath files ---" -ForegroundColor Cyan
        foreach ($rel in @($cfg.BasePathFiles)) {
            if (-not $rel) { continue }
            $p = Join-Path $bp $rel
            Remove-Target -Path $p -Label "BasePath\$rel"
        }

        Write-Host ""
        Write-Host "--- BasePath globs ---" -ForegroundColor Cyan
        foreach ($pattern in @($cfg.BasePathGlobs)) {
            if (-not $pattern) { continue }
            Get-ChildItem -Path $bp -Force -Filter $pattern -ErrorAction SilentlyContinue |
                ForEach-Object { Remove-Target -Path $_.FullName -Label "BasePath\$($_.Name)" }
        }
    }
} elseif ($doBasePath -and -not $bp) {
    Write-Host ""
    Write-Host "No BasePath set in cleanup.json or -BasePath; skipping BasePath targets." -ForegroundColor Yellow
}

# -------------------------------------------------------------------------
# 3) Windows services
# -------------------------------------------------------------------------
if ($doServices) {
    $isAdmin = $false
    try {
        $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).
            IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { }
    if (-not $isAdmin -and -not $DryRun) {
        Write-Host ""
        Write-Host "WARNING: Not running as Administrator - service deletion may fail." -ForegroundColor Yellow
    }

    Write-Host ""
    Write-Host "--- Windows services ---" -ForegroundColor Cyan
    foreach ($svc in @($cfg.WindowsServices)) {
        if (-not $svc) { continue }
        Remove-ServiceTarget -ServiceName $svc -TimeoutSeconds $svcTimeout
    }
}

# -------------------------------------------------------------------------
# 4) Optional wipe of entire BasePath
# -------------------------------------------------------------------------
if ($doWipeBase -and $bp) {
    Write-Host ""
    Write-Host "--- Entire BasePath ---" -ForegroundColor Cyan
    Remove-Target -Path $bp -Label "BasePath (entire tree)"
}

# -------------------------------------------------------------------------
# Summary
# -------------------------------------------------------------------------
Write-Banner "Summary"
Write-Host ("  Removed : {0}" -f $deleted.Count) -ForegroundColor Green
Write-Host ("  Failed  : {0}" -f $failed.Count)  -ForegroundColor $(if ($failed.Count) { "Red" } else { "Gray" })
Write-Host ("  Skipped : {0} (missing or DryRun)" -f $skipped.Count) -ForegroundColor Gray

if ($failed.Count -gt 0) {
    Write-Host ""
    Write-Host "Failures:" -ForegroundColor Red
    $failed | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}

Write-Host ""
Write-Host "Done. Edit config\cleanup.json to change what gets deleted." -ForegroundColor Cyan
Write-Host "Full component uninstall: .\Cleanup-KD.ps1" -ForegroundColor Gray
exit 0
