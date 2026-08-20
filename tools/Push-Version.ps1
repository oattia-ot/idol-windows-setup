<#
.SYNOPSIS
    Write version.txt at the toolkit root and push the full repo to GitHub.

.DESCRIPTION
    Creates (or overwrites) version.txt one level above this script's folder
    (i.e. the toolkit / repo root), with the version number you pass in.
    Then stages ALL files under the repo root, commits, and pushes to:
      https://github.com/oattia-ot/idol-windows-setup.git

    Place this script under:  <repo>\tools\Push-Version.ps1
    (or any subfolder one level under the repo root)
    Output file is written to: <repo>\version.txt

.PARAMETER Version
    Version string to write into version.txt.
    Optional if environment variable KD_VERSION or VERSION is set.
    Precedence: -Version argument  >  $env:KD_VERSION  >  $env:VERSION
    Examples: 0.6.21   v6.42   1.0.0

.PARAMETER Message
    Optional commit message. Default:
      "Bump version to <Version>"

.PARAMETER RemoteName
    Git remote name to use (default: origin).

.PARAMETER Branch
    Branch to push (default: current branch, or main if none).

.PARAMETER RemoteUrl
    Remote URL (default: https://github.com/oattia-ot/idol-windows-setup.git).

.EXAMPLE
    .\tools\Push-Version.ps1 -Version 0.6.21

.EXAMPLE
    $env:KD_VERSION = "v6.42"
    .\tools\Push-Version.ps1

.EXAMPLE
    # CMD / batch:
    set KD_VERSION=v6.42
    tools\Push-Version.bat

.EXAMPLE
    .\tools\Push-Version.ps1 -Version 0.6.21 -Message "Release 0.6.21"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $false, Position = 0)]
    [string]$Version = "",

    [Parameter(Mandatory = $false)]
    [string]$Message = "",

    [Parameter(Mandatory = $false)]
    [string]$RemoteName = "origin",

    [Parameter(Mandatory = $false)]
    [string]$Branch = "",

    [Parameter(Mandatory = $false)]
    [string]$RemoteUrl = "https://github.com/oattia-ot/idol-windows-setup.git"
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Resolve version: -Version arg > $env:KD_VERSION > $env:VERSION
# ---------------------------------------------------------------------------
if ([string]::IsNullOrWhiteSpace($Version)) {
    if (-not [string]::IsNullOrWhiteSpace($env:KD_VERSION)) {
        $Version = $env:KD_VERSION.Trim()
    }
    elseif (-not [string]::IsNullOrWhiteSpace($env:VERSION)) {
        $Version = $env:VERSION.Trim()
    }
}

if ([string]::IsNullOrWhiteSpace($Version)) {
    Write-Host ""
    Write-Host "ERROR: No version supplied." -ForegroundColor Red
    Write-Host "  Pass -Version <value>  or  set env var KD_VERSION (or VERSION)." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Examples:" -ForegroundColor DarkGray
    Write-Host '    .\Push-Version.ps1 -Version v6.42' -ForegroundColor DarkGray
    Write-Host '    $env:KD_VERSION = "v6.42"; .\Push-Version.ps1' -ForegroundColor DarkGray
    Write-Host '    set KD_VERSION=v6.42 && Push-Version.bat' -ForegroundColor DarkGray
    Write-Host ""
    exit 1
}

$Version = $Version.Trim()

# ---------------------------------------------------------------------------
# Paths: script lives under <root>\<subfolder>\  ->  version.txt in <root>\
# ---------------------------------------------------------------------------
$scriptDir = $PSScriptRoot
if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$rootDir   = (Resolve-Path (Join-Path $scriptDir "..")).Path
$versionFile = Join-Path $rootDir "version.txt"

Write-Host ""
Write-Host "  Push-Version" -ForegroundColor Cyan
Write-Host "  Root        : $rootDir" -ForegroundColor DarkGray
Write-Host "  Version file: $versionFile" -ForegroundColor DarkGray
Write-Host "  Version     : $Version" -ForegroundColor Yellow
Write-Host "  Remote      : $RemoteUrl" -ForegroundColor DarkGray
Write-Host ""

# ---------------------------------------------------------------------------
# 1. Write version.txt (version + execution timestamp)
#    Use .NET UTF-8 *without BOM* so it works on Windows PowerShell 5.1
#    (Set-Content -Encoding utf8NoBOM is PowerShell 7+ only).
# ---------------------------------------------------------------------------
$timestamp = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")  # local time with offset
$versionContent = ($Version + [Environment]::NewLine + $timestamp + [Environment]::NewLine)

$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($versionFile, $versionContent, $utf8NoBom)
if (-not (Test-Path -LiteralPath $versionFile)) {
    throw "Failed to create version.txt at $versionFile"
}
Write-Host "[OK] Wrote version.txt -> $Version  ($timestamp)" -ForegroundColor Green

# ---------------------------------------------------------------------------
# 2. Ensure git is available
# ---------------------------------------------------------------------------
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    throw "git was not found on PATH. Install Git for Windows and re-run."
}

# Run a git command without letting stderr become a terminating error under
# $ErrorActionPreference = 'Stop' (Windows PowerShell treats native stderr
# as ErrorRecords). Returns stdout lines; sets script-scope $GitLastExitCode.
function Invoke-Git {
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$GitArgs
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $output = & git @GitArgs 2>&1
    $script:GitLastExitCode = $LASTEXITCODE
    $ErrorActionPreference = $prev
    # Flatten: keep text lines, drop ErrorRecord noise for callers that just want stdout
    $lines = @()
    foreach ($item in @($output)) {
        if ($item -is [System.Management.Automation.ErrorRecord]) {
            # expected failures (not a repo, no remote, etc.) -- ignore text
            continue
        }
        $lines += [string]$item
    }
    return $lines
}

Push-Location $rootDir
try {
    # -----------------------------------------------------------------------
    # 3. Init repo if needed; ensure remote points at the target GitHub URL
    # -----------------------------------------------------------------------
    $null = Invoke-Git rev-parse --is-inside-work-tree
    if ($GitLastExitCode -ne 0) {
        Write-Host "Initializing new git repository in $rootDir ..." -ForegroundColor Yellow
        $null = Invoke-Git init
        if ($GitLastExitCode -ne 0) { throw "git init failed" }
    }

    $existingUrl = (Invoke-Git remote get-url $RemoteName | Select-Object -First 1)
    if ($GitLastExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($existingUrl)) {
        Write-Host "Adding remote '$RemoteName' -> $RemoteUrl" -ForegroundColor Yellow
        $null = Invoke-Git remote add $RemoteName $RemoteUrl
        if ($GitLastExitCode -ne 0) { throw "git remote add failed" }
    }
    elseif ($existingUrl.TrimEnd("/") -ne $RemoteUrl.TrimEnd("/")) {
        Write-Host "Updating remote '$RemoteName' URL:" -ForegroundColor Yellow
        Write-Host "  was: $existingUrl"
        Write-Host "  now: $RemoteUrl"
        $null = Invoke-Git remote set-url $RemoteName $RemoteUrl
        if ($GitLastExitCode -ne 0) { throw "git remote set-url failed" }
    }
    else {
        Write-Host "[OK] Remote '$RemoteName' already points at $RemoteUrl" -ForegroundColor Green
    }

    # -----------------------------------------------------------------------
    # 4. Resolve branch + detect whether any commit exists yet
    # -----------------------------------------------------------------------
    $null = Invoke-Git rev-parse --verify HEAD
    $hasCommit = ($GitLastExitCode -eq 0)

    if ([string]::IsNullOrWhiteSpace($Branch)) {
        if ($hasCommit) {
            $Branch = (Invoke-Git rev-parse --abbrev-ref HEAD | Select-Object -First 1)
        }
        if ([string]::IsNullOrWhiteSpace($Branch) -or $Branch -eq "HEAD") {
            $Branch = "main"
        }
    }
    # Create/switch local branch name (safe even with zero commits on modern git)
    $null = Invoke-Git checkout -B $Branch
    Write-Host "Branch: $Branch" -ForegroundColor DarkGray

    # -----------------------------------------------------------------------
    # 5. Ensure a .gitignore exists so component ZIPs / installers / logs
    #    never get staged (these are the usual cause of huge, timeout-prone
    #    pushes -- see docs/PUSH-VERSION.md sec. 7/sec. 9). Created only if missing;
    #    an existing .gitignore is never overwritten.
    # -----------------------------------------------------------------------
    $gitignorePath = Join-Path $rootDir ".gitignore"
    if (-not (Test-Path -LiteralPath $gitignorePath)) {
        Write-Host "No .gitignore found -- creating a default one (excludes ZIPs/installers/logs)." -ForegroundColor Yellow
        $gitignoreContent = @"
# Auto-created by Push-Version.ps1 -- see docs/PUSH-VERSION.md sec. 7.
# Keeps large component packages, installers, and runtime output out of git,
# which is the #1 cause of push timeouts / HTTP 408 on this repo.

# Packages / binaries (component ZIPs, JDK/NSSM installers, etc.)
*.zip
*.msi
*.exe
!nifi/nssm.exe

# Runtime / secrets
logs/
ssl/
env/
*.log

# Generated NiFi extension NARs (re-extracted from NiFiIngest ZIP on demand)
nifi/nifi-connectors/*.nar

# Python
__pycache__/
*.pyc
.venv/
"@
        $utf8NoBomGi = New-Object System.Text.UTF8Encoding $false
        [System.IO.File]::WriteAllText($gitignorePath, $gitignoreContent, $utf8NoBomGi)
        Write-Host "[OK] Wrote $gitignorePath" -ForegroundColor Green
    }

    # -----------------------------------------------------------------------
    # 6. Stage ALL files under the repo root and commit when needed
    # -----------------------------------------------------------------------
    Write-Host "Staging all files under $rootDir ..." -ForegroundColor Cyan
    $null = Invoke-Git add -A
    if ($GitLastExitCode -ne 0) { throw "git add -A failed" }

    # Safety net: even with .gitignore in place, warn loudly (and let the
    # user bail out) if anything unusually large is about to be committed --
    # e.g. a previously-tracked ZIP that .gitignore can't retroactively drop,
    # or a package that was force-added. This is what typically causes
    # 'HTTP 408' / 'unexpected disconnect while reading sideband packet'
    # on push.
    $bigFileThresholdMB = 20
    $stagedFiles = @(Invoke-Git diff --cached --name-only)
    $bigFiles = @()
    foreach ($f in $stagedFiles) {
        if ([string]::IsNullOrWhiteSpace($f)) { continue }
        $full = Join-Path $rootDir $f
        if (Test-Path -LiteralPath $full -PathType Leaf) {
            $sizeMB = [math]::Round((Get-Item -LiteralPath $full).Length / 1MB, 1)
            if ($sizeMB -ge $bigFileThresholdMB) {
                $bigFiles += [pscustomobject]@{ Path = $f; SizeMB = $sizeMB }
            }
        }
    }
    if ($bigFiles.Count -gt 0) {
        Write-Host ""
        Write-Host "WARNING: staged file(s) >= ${bigFileThresholdMB}MB detected -- these are the usual cause of" -ForegroundColor Yellow
        Write-Host "push timeouts (HTTP 408 / sideband disconnect). Consider adding a .gitignore rule" -ForegroundColor Yellow
        Write-Host "and 'git rm --cached <file>' instead of committing them:" -ForegroundColor Yellow
        foreach ($bf in ($bigFiles | Sort-Object SizeMB -Descending)) {
            Write-Host ("  {0,8:N1} MB  {1}" -f $bf.SizeMB, $bf.Path) -ForegroundColor DarkYellow
        }
        Write-Host ""
        try {
            Write-Host -ForegroundColor Yellow -NoNewline "Continue committing these large file(s) anyway? (Y/N) [N] "
            $bigConfirm = Read-Host
        }
        catch {
            $bigConfirm = "N"
        }
        if ($bigConfirm.Trim().ToUpperInvariant() -ne "Y") {
            Write-Host "Aborted. Unstage the large file(s) (git restore --staged <file>), add a .gitignore rule, and re-run." -ForegroundColor Yellow
            return
        }
    }

    $statusLines = @(Invoke-Git status --porcelain)
    $statusText  = ($statusLines | Out-String).Trim()
    $stagedCount = @($statusLines | Where-Object { $_ -match '\S' }).Count

    # Fresh repo with no commits yet: always commit so the branch exists
    $needCommit = ($stagedCount -gt 0) -or (-not $hasCommit)

    if (-not $needCommit) {
        Write-Host "No changes to commit (working tree clean)." -ForegroundColor Yellow
    }
    else {
        if ([string]::IsNullOrWhiteSpace($Message)) {
            $Message = "Bump version to $Version"
        }
        # Ensure git identity exists (required for the first commit)
        $cfgName  = (Invoke-Git tools user.name  | Select-Object -First 1)
        $cfgEmail = (Invoke-Git tools user.email | Select-Object -First 1)
        if ([string]::IsNullOrWhiteSpace($cfgName) -or [string]::IsNullOrWhiteSpace($cfgEmail)) {
            Write-Host "Git user.name / user.email not set. toolsuring local repo identity..." -ForegroundColor Yellow
            if ([string]::IsNullOrWhiteSpace($cfgName)) {
                $null = Invoke-Git tools user.name "KD Setup"
            }
            if ([string]::IsNullOrWhiteSpace($cfgEmail)) {
                $null = Invoke-Git tools user.email "kd-setup@local"
            }
        }

        if ($stagedCount -gt 0) {
            Write-Host "Files to commit ($stagedCount):" -ForegroundColor DarkGray
            foreach ($line in ($statusLines | Select-Object -First 30)) {
                Write-Host "  $line" -ForegroundColor DarkGray
            }
            if ($stagedCount -gt 30) {
                Write-Host "  ... and $($stagedCount - 30) more" -ForegroundColor DarkGray
            }
        }

        $null = Invoke-Git commit -m $Message
        if ($GitLastExitCode -ne 0) {
            if (-not $hasCommit) {
                $null = Invoke-Git commit --allow-empty -m $Message
            }
            if ($GitLastExitCode -ne 0) {
                throw "git commit failed. Ensure git identity is set (git tools user.name / user.email)."
            }
        }
        Write-Host "[OK] Committed: $Message" -ForegroundColor Green
        $hasCommit = $true
    }

    # Must have at least one commit before push (avoids: src refspec main does not match any)
    $null = Invoke-Git rev-parse --verify HEAD
    if ($GitLastExitCode -ne 0) {
        throw "No commits exist on branch '$Branch'. Cannot push."
    }

    # -----------------------------------------------------------------------
    # 6. Confirm with user before pushing
    # -----------------------------------------------------------------------
    Write-Host ""
    Write-Host "About to push full repo root:" -ForegroundColor Cyan
    Write-Host "  version : $Version"
    Write-Host "  root    : $rootDir"
    Write-Host "  remote  : $RemoteName ($RemoteUrl)"
    Write-Host "  branch  : $Branch"
    Write-Host ""
    try {
        Write-Host -ForegroundColor Yellow -NoNewline "Push to GitHub now? (Y/N) [N] "
        $confirm = Read-Host
    }
    catch {
        $confirm = "N"
    }
    if ($confirm.Trim().ToUpperInvariant() -ne "Y") {
        Write-Host "Push cancelled by user. Changes are committed locally only." -ForegroundColor Yellow
        return
    }

    Write-Host "Pushing to $RemoteName $Branch ..." -ForegroundColor Cyan

    # Large/slow pushes over HTTPS are the most common source of
    # 'HTTP 408' / 'unexpected disconnect while reading sideband packet'
    # against GitHub -- usually because the default HTTP post buffer is small
    # and/or a corporate proxy or flaky connection times out mid-upload.
    # Raise the buffer and disable the low-speed abort for *this* push only
    # (local repo config, not global) so a large-but-legitimate push has a
    # fair chance to complete.
    $null = Invoke-Git config http.postBuffer 524288000
    $null = Invoke-Git config http.lowSpeedLimit 0
    $null = Invoke-Git config http.lowSpeedTime 999999

    # Show push output live (auth prompts, progress)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & git push -u $RemoteName $Branch
    $pushCode = $LASTEXITCODE

    # One automatic retry on the classic transient failures -- a second
    # attempt frequently succeeds once the connection/proxy has recovered,
    # and http.postBuffer is now already raised for this repo.
    if ($pushCode -ne 0) {
        Write-Host ""
        Write-Host "Push failed (exit $pushCode) -- retrying once ..." -ForegroundColor Yellow
        Start-Sleep -Seconds 3
        & git push -u $RemoteName $Branch
        $pushCode = $LASTEXITCODE
    }
    $ErrorActionPreference = $prev
    if ($pushCode -ne 0) {
        throw @"
git push failed.

Common causes:
  - Not authenticated (use Git Credential Manager, a PAT, or SSH)
  - Branch protection / permissions on oattia-ot/idol-windows-setup
  - Remote rejects non-fast-forward (pull --rebase then re-run)
  - Large push timing out (HTTP 408 / sideband disconnect): usually caused
    by a large binary (ZIP/MSI/EXE) that got committed. Check for it with:
      git rev-list --objects --all | git cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(rest)' | sort -k3 -n -r | head
    Remove it from the latest commit with 'git rm --cached <file>', add a
    .gitignore rule (see docs/PUSH-VERSION.md sec. 7), commit, and re-run.
    If it's already further back in history, it needs a history rewrite
    (git filter-repo or BFG Repo-Cleaner) before the push will succeed.
  - Try SSH instead of HTTPS: git remote set-url $RemoteName git@github.com:oattia-ot/idol-windows-setup.git

Repo: $RemoteUrl
"@
    }

    Write-Host ""
    Write-Host "[OK] version.txt ($Version) pushed to $RemoteUrl ($Branch)" -ForegroundColor Green
    Write-Host ""
}
finally {
    Pop-Location
}
