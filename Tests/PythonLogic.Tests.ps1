<#
.SYNOPSIS
    Pester tests that exercise the Python backend of the KD installer.

.DESCRIPTION
    These tests do NOT re-implement the Python unit tests in PowerShell.
    Instead they:
      1. Locate Python and run the pytest suite (tests/) - the real unit tests.
      2. Smoke-test the CLI entry point (install_kd.py --help, --dry-run with a
         throwaway config) so the PowerShell / Python boundary is verified.

    Run from an elevated or normal PowerShell session:

        cd C:\Tools\kd-win-setup-python
        Invoke-Pester -Path .\Tests\PythonLogic.Tests.ps1

    Or with detailed output:

        Invoke-Pester -Path .\Tests\PythonLogic.Tests.ps1 -Output Detailed
#>

#Requires -Version 5.1

BeforeAll {
    $script:Root = Split-Path $PSScriptRoot -Parent
    if (-not $script:Root) { $script:Root = $PSScriptRoot }

    function Get-PythonExe {
        # PowerShell 5.1 compatible (no ?. operator)
        $list = New-Object System.Collections.Generic.List[string]
        foreach ($name in @('python', 'python3', 'py')) {
            $cmd = Get-Command $name -ErrorAction SilentlyContinue
            if ($null -ne $cmd -and $cmd.Source -and (Test-Path -LiteralPath $cmd.Source)) {
                $list.Add($cmd.Source) | Out-Null
            }
        }
        if ($list.Count -eq 0) { return $null }
        return $list[0]
    }

    $script:Python = Get-PythonExe
    $script:InstallPy = Join-Path $script:Root 'install_kd.py'
    $script:TestsDir  = Join-Path $script:Root 'tests'
}

Describe 'Python runtime' {
    It 'Python 3.8+ is available' {
        $script:Python | Should -Not -BeNullOrEmpty
        $ver = & $script:Python -c "import sys; print('{0}.{1}'.format(sys.version_info.major, sys.version_info.minor))"
        [version]$ver | Should -BeGreaterOrEqual ([version]'3.8')
    }

    It 'install_kd.py exists' {
        Test-Path -LiteralPath $script:InstallPy | Should -Be $true
    }

    It 'pytest test directory exists' {
        Test-Path -LiteralPath $script:TestsDir | Should -Be $true
    }
}

Describe 'Python unit tests (pytest)' {
    BeforeAll {
        # Ensure pytest is present (install quietly if missing)
        $hasPytest = & $script:Python -c "import pytest; print('yes')" 2>$null
        if ($hasPytest -ne 'yes') {
            & $script:Python -m pip install --quiet pytest 2>$null | Out-Null
        }
    }

    It 'All pytest tests pass' {
        $result = & $script:Python -m pytest $script:TestsDir -q --tb=line 2>&1
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            Write-Host ($result -join "`n") -ForegroundColor Red
        }
        $exitCode | Should -Be 0
    }
}

Describe 'CLI smoke tests' {
    It 'install_kd.py --help exits 0 and mentions Mode' {
        $out = & $script:Python $script:InstallPy --help 2>&1 | Out-String
        $LASTEXITCODE | Should -Be 0
        $out | Should -Match 'mode|Install|Configure'
    }

    It 'Dry-run with missing config fails gracefully (non-zero, no crash)' {
        $missing = Join-Path $env:TEMP ("kd-missing-config-{0}.json" -f (Get-Random))
        $out = & $script:Python $script:InstallPy --mode Install --dry-run --non-interactive --config $missing 2>&1 | Out-String
        $LASTEXITCODE | Should -Not -Be 0
        $out | Should -Match 'FATAL|not found|Configuration'
    }

    It 'Dry-run with valid default config starts and reports validation' {
        $cfg = Join-Path $script:Root 'config\default-config.json'
        if (-not (Test-Path -LiteralPath $cfg)) {
            Set-ItResult -Skipped -Because 'default-config.json not present'
            return
        }
        $out = & $script:Python $script:InstallPy --mode Install --dry-run --non-interactive --config $cfg --force 2>&1 | Out-String
        $out | Should -Match 'Knowledge Discovery|CONFIGURATION SUMMARY|ENVIRONMENT VALIDATION'
        $LASTEXITCODE | Should -BeIn @(0, 1)
    }
}

Describe 'Module import smoke' {
    It 'kd package imports without error' {
        $rootEscaped = $script:Root.Replace('\', '\\')
        $code = @"
import sys
sys.path.insert(0, r'$rootEscaped')
from kd import config, discovery, ini_config, state, service_manager, validation
print('OK')
"@
        $out = & $script:Python -c $code 2>&1 | Out-String
        $LASTEXITCODE | Should -Be 0
        $out | Should -Match 'OK'
    }
}
