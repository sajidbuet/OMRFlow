# pytest-ruff-mypy.ps1
#
# OMRFlow full Python quality gate with persistent diagnostic logging.
#
# Runs:
#   1. Full pytest suite
#   2. Ruff lint/static checks
#   3. Mypy static type checking
#
# Logs are written to:
#
#   <repository>\Scratch\Log\<timestamp>\
#
# Example:
#
#   Scratch\Log\2026-10-03_104512\
#       environment.log
#       git-status-before.log
#       pytest.log
#       ruff.log
#       mypy.log
#       git-status-after.log
#       summary.log
#
# Scratch\Log\LATEST.txt contains the path of the newest run.
#
# The script continues through all checks even if one fails.
#
# Exit code:
#   0 = all checks passed
#   1 = one or more checks failed, or testing modified a clean worktree
#
# Usage:
#
#   .\pytest-ruff-mypy.ps1
#
# If PowerShell execution policy blocks it:
#
#   powershell -ExecutionPolicy Bypass -File .\pytest-ruff-mypy.ps1


$ErrorActionPreference = "Stop"


# ============================================================================
# Repository root
# ============================================================================

$RepoRoot = $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = (Get-Location).Path
}

Set-Location $RepoRoot


# ============================================================================
# Diagnostic log directory
# ============================================================================

$LogRoot = Join-Path $RepoRoot "Scratch\Log"

if (-not (Test-Path $LogRoot)) {
    New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
}

$Timestamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$RunLogDir = Join-Path $LogRoot $Timestamp

New-Item -ItemType Directory -Path $RunLogDir -Force | Out-Null


$EnvironmentLog = Join-Path $RunLogDir "environment.log"
$GitBeforeLog   = Join-Path $RunLogDir "git-status-before.log"
$PytestLog      = Join-Path $RunLogDir "pytest.log"
$RuffLog        = Join-Path $RunLogDir "ruff.log"
$MypyLog        = Join-Path $RunLogDir "mypy.log"
$GitAfterLog    = Join-Path $RunLogDir "git-status-after.log"
$SummaryLog     = Join-Path $RunLogDir "summary.log"

$LatestPointer = Join-Path $LogRoot "LATEST.txt"

Set-Content `
    -Path $LatestPointer `
    -Value $RunLogDir `
    -Encoding UTF8


# ============================================================================
# Helper output function
# ============================================================================

function Write-Section {

    param (
        [Parameter(Mandatory = $true)]
        [string]$Title
    )

    Write-Host ""
    Write-Host "============================================================"
    Write-Host " $Title"
    Write-Host "============================================================"
    Write-Host ""
}


# ============================================================================
# Initial information
# ============================================================================

Write-Section "OMRFlow - Pytest / Ruff / Mypy"

Write-Host "Repository : $RepoRoot"
Write-Host "Log folder : $RunLogDir"
Write-Host "Started    : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host ""


# ============================================================================
# Locate Python
#
# Prefer repository-local .venv.
# ============================================================================

$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (Test-Path $VenvPython) {

    $Python = $VenvPython

}
else {

    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue

    if ($null -eq $PythonCommand) {

        $Message = @"
ERROR: Python was not found.

Expected either:

    $VenvPython

or a 'python' executable available on PATH.
"@

        Write-Host $Message -ForegroundColor Red

        Set-Content `
            -Path $SummaryLog `
            -Value $Message `
            -Encoding UTF8

        exit 1
    }

    $Python = $PythonCommand.Source
}


# ============================================================================
# Environment diagnostics
# ============================================================================

$EnvironmentLines = @()

$EnvironmentLines += "OMRFlow quality-gate environment"
$EnvironmentLines += "================================"
$EnvironmentLines += ""
$EnvironmentLines += "Timestamp   : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
$EnvironmentLines += "Repository  : $RepoRoot"
$EnvironmentLines += "Log folder  : $RunLogDir"
$EnvironmentLines += "Python      : $Python"

try {
    $PythonVersion = (& $Python --version 2>&1 | Out-String).Trim()
}
catch {
    $PythonVersion = "ERROR: $($_.Exception.Message)"
}

$EnvironmentLines += "Version     : $PythonVersion"
$EnvironmentLines += "PowerShell  : $($PSVersionTable.PSVersion)"
$EnvironmentLines += "OS          : $([System.Environment]::OSVersion.VersionString)"
$EnvironmentLines += "Machine     : $env:COMPUTERNAME"
$EnvironmentLines += "Processor   : $env:PROCESSOR_IDENTIFIER"
$EnvironmentLines += ""

$EnvironmentLines |
    Set-Content `
        -Path $EnvironmentLog `
        -Encoding UTF8


Write-Host "Python     : $Python"
Write-Host "Version    : $PythonVersion"


# ============================================================================
# Git information
# ============================================================================

$GitAvailable = Get-Command git -ErrorAction SilentlyContinue

$InitialGitStatus = @()
$Branch = "(Git unavailable)"
$Commit = "(Git unavailable)"

if ($null -ne $GitAvailable) {

    Write-Section "Git state before testing"

    $Branch = (git branch --show-current).Trim()
    $Commit = (git rev-parse HEAD).Trim()

    $GitBeforeLines = @()

    $GitBeforeLines += "Branch:"
    $GitBeforeLines += $Branch
    $GitBeforeLines += ""
    $GitBeforeLines += "Commit:"
    $GitBeforeLines += $Commit
    $GitBeforeLines += ""
    $GitBeforeLines += "git status --short:"
    $GitBeforeLines += ""

    $InitialGitStatus = @(git status --porcelain)

    $GitShortStatus = @(git status --short)

    if ($GitShortStatus.Count -eq 0) {
        $GitBeforeLines += "(clean)"
    }
    else {
        $GitBeforeLines += $GitShortStatus
    }

    $GitBeforeLines += ""
    $GitBeforeLines += "git log -1 --stat:"
    $GitBeforeLines += ""

    $GitBeforeLines += @(git log -1 --stat)

    $GitBeforeLines |
        Set-Content `
            -Path $GitBeforeLog `
            -Encoding UTF8


    Write-Host "Branch     : $Branch"
    Write-Host "Commit     : $Commit"

    if ($InitialGitStatus.Count -eq 0) {

        Write-Host "Worktree   : clean" -ForegroundColor Green

    }
    else {

        Write-Host "Worktree   : NOT CLEAN" -ForegroundColor Yellow
        Write-Host ""

        git status --short
    }
}
else {

    "Git executable not found." |
        Set-Content `
            -Path $GitBeforeLog `
            -Encoding UTF8

    Write-Host "Git was not found." -ForegroundColor Yellow
}


# ============================================================================
# Result storage
# ============================================================================

$Results = @()


# ============================================================================
# Execute one quality check
#
# STDOUT and STDERR are:
#
#   - shown in the terminal;
#   - written to the corresponding diagnostic log.
# ============================================================================

function Invoke-QualityCheck {

    param (
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [string]$LogFile,

        [Parameter(Mandatory = $true)]
        [scriptblock]$Command
    )


    Write-Section $Name

    $StartTime = Get-Date


    $Header = @"
============================================================
$Name
============================================================

Started: $($StartTime.ToString("yyyy-MM-dd HH:mm:ss"))
Branch : $Branch
Commit : $Commit

"@

    Set-Content `
        -Path $LogFile `
        -Value $Header `
        -Encoding UTF8


    $ExitCode = 1


    try {

        # Merge STDERR into STDOUT so traceback/error output is saved.
        #
        # Tee-Object simultaneously:
        #   - displays output in PowerShell;
        #   - appends it to the diagnostic log.

        & $Command 2>&1 |
            Tee-Object `
                -FilePath $LogFile `
                -Append

        $ExitCode = $LASTEXITCODE


        if ($null -eq $ExitCode) {
            $ExitCode = 0
        }

    }
    catch {

        $ExitCode = 1

        $ErrorText = @"

============================================================
POWERSHELL EXCEPTION
============================================================

$($_ | Out-String)

Exception message:
$($_.Exception.Message)

Stack trace:
$($_.ScriptStackTrace)

"@

        Write-Host $ErrorText -ForegroundColor Red

        Add-Content `
            -Path $LogFile `
            -Value $ErrorText `
            -Encoding UTF8
    }


    $EndTime = Get-Date
    $Duration = $EndTime - $StartTime


    if ($ExitCode -eq 0) {

        $Status = "PASS"

        Write-Host ""
        Write-Host "$Name : PASS" -ForegroundColor Green

    }
    else {

        $Status = "FAIL"

        Write-Host ""
        Write-Host "$Name : FAIL (exit code $ExitCode)" `
            -ForegroundColor Red
    }


    $DurationText = "{0:hh\:mm\:ss}" -f $Duration

    Write-Host "Duration   : $DurationText"
    Write-Host "Log        : $LogFile"


    $Footer = @"

============================================================
RESULT
============================================================

Status    : $Status
Exit code : $ExitCode
Finished  : $($EndTime.ToString("yyyy-MM-dd HH:mm:ss"))
Duration  : $DurationText

"@

    Add-Content `
        -Path $LogFile `
        -Value $Footer `
        -Encoding UTF8


    $script:Results += [PSCustomObject]@{
        Check    = $Name
        Status   = $Status
        ExitCode = $ExitCode
        Duration = $Duration
        LogFile  = $LogFile
    }
}


# ============================================================================
# 1. PYTEST
# ============================================================================

Invoke-QualityCheck `
    -Name "PYTEST - Full test suite" `
    -LogFile $PytestLog `
    -Command {

        & $Python -m pytest
    }


# ============================================================================
# 2. RUFF
# ============================================================================

Invoke-QualityCheck `
    -Name "RUFF - Lint and static checks" `
    -LogFile $RuffLog `
    -Command {

        & $Python -m ruff check src tests tools scripts
    }


# ============================================================================
# 3. MYPY
# ============================================================================

Invoke-QualityCheck `
    -Name "MYPY - Static type checking" `
    -LogFile $MypyLog `
    -Command {

        & $Python -m mypy src
    }


# ============================================================================
# Git state after tests
# ============================================================================

$WorktreeChanged = $false
$FinalGitStatus = @()

if ($null -ne $GitAvailable) {

    Write-Section "Git state after testing"

    $FinalGitStatus = @(git status --porcelain)
    $FinalShortStatus = @(git status --short)


    $GitAfterLines = @()

    $GitAfterLines += "Branch:"
    $GitAfterLines += (git branch --show-current)
    $GitAfterLines += ""
    $GitAfterLines += "Commit:"
    $GitAfterLines += (git rev-parse HEAD)
    $GitAfterLines += ""
    $GitAfterLines += "git status --short:"
    $GitAfterLines += ""


    if ($FinalShortStatus.Count -eq 0) {

        $GitAfterLines += "(clean)"

        Write-Host "Worktree is clean." -ForegroundColor Green

    }
    else {

        $GitAfterLines += $FinalShortStatus

        Write-Host "Worktree is not clean." -ForegroundColor Yellow
        Write-Host ""

        git status --short


        # Only treat this as a quality-gate failure if the worktree
        # was clean before the checks started.
        if ($InitialGitStatus.Count -eq 0) {

            $WorktreeChanged = $true

            Write-Host ""
            Write-Host (
                "WARNING: The repository was clean before testing " +
                "but contains changes now."
            ) -ForegroundColor Red
        }
    }


    $GitAfterLines |
        Set-Content `
            -Path $GitAfterLog `
            -Encoding UTF8

}
else {

    "Git executable not found." |
        Set-Content `
            -Path $GitAfterLog `
            -Encoding UTF8
}


# ============================================================================
# Final summary
# ============================================================================

Write-Section "FINAL SUMMARY"


$SummaryLines = @()

$SummaryLines += "OMRFlow pytest / Ruff / Mypy quality-gate summary"
$SummaryLines += "================================================"
$SummaryLines += ""
$SummaryLines += "Started repository : $RepoRoot"
$SummaryLines += "Branch             : $Branch"
$SummaryLines += "Commit             : $Commit"
$SummaryLines += "Log directory      : $RunLogDir"
$SummaryLines += ""
$SummaryLines += "Checks:"
$SummaryLines += ""


foreach ($Result in $Results) {

    $DurationText = "{0:hh\:mm\:ss}" -f $Result.Duration

    $Line = "{0,-35} {1,-6} Exit={2,-3} {3}" -f `
        $Result.Check,
        $Result.Status,
        $Result.ExitCode,
        $DurationText

    $SummaryLines += $Line


    if ($Result.Status -eq "PASS") {

        Write-Host $Line -ForegroundColor Green

    }
    else {

        Write-Host $Line -ForegroundColor Red
    }
}


$FailedChecks = @(
    $Results |
        Where-Object {
            $_.Status -eq "FAIL"
        }
)


$SummaryLines += ""
$SummaryLines += "Individual logs:"
$SummaryLines += ""

foreach ($Result in $Results) {

    $SummaryLines += "$($Result.Check)"
    $SummaryLines += "    $($Result.LogFile)"
}


$SummaryLines += ""
$SummaryLines += "Git state changed during testing: $WorktreeChanged"
$SummaryLines += ""


if (($FailedChecks.Count -eq 0) -and (-not $WorktreeChanged)) {

    $OverallStatus = "PASS"

    $SummaryLines += "OVERALL RESULT: PASS"

}
else {

    $OverallStatus = "FAIL"

    $SummaryLines += "OVERALL RESULT: FAIL"


    if ($FailedChecks.Count -gt 0) {

        $SummaryLines += ""
        $SummaryLines += "Failed checks:"

        foreach ($Failure in $FailedChecks) {

            $SummaryLines += `
                "  - $($Failure.Check) (exit $($Failure.ExitCode))"
        }
    }


    if ($WorktreeChanged) {

        $SummaryLines += ""
        $SummaryLines += `
            "The repository was clean before testing but was modified " +
            "during the test run."
    }
}


$SummaryLines += ""
$SummaryLines += "Finished: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"


$SummaryLines |
    Set-Content `
        -Path $SummaryLog `
        -Encoding UTF8


# ============================================================================
# Claude diagnostic instructions
# ============================================================================

$ClaudeInstructions = @"

============================================================
CLAUDE CODE DIAGNOSTIC INFORMATION
============================================================

Latest quality-gate run:

    $RunLogDir

Summary:

    $SummaryLog

Pytest:

    $PytestLog

Ruff:

    $RuffLog

Mypy:

    $MypyLog

Git before:

    $GitBeforeLog

Git after:

    $GitAfterLog

The pointer to the latest run is:

    $LatestPointer

When diagnosing a failure, inspect summary.log first, then inspect
the corresponding pytest.log, ruff.log, or mypy.log.

"@

Write-Host $ClaudeInstructions

Add-Content `
    -Path $SummaryLog `
    -Value $ClaudeInstructions `
    -Encoding UTF8


# ============================================================================
# Final exit
# ============================================================================

if ($OverallStatus -eq "PASS") {

    Write-Host ""
    Write-Host "============================================================" `
        -ForegroundColor Green
    Write-Host " ALL CHECKS PASSED" `
        -ForegroundColor Green
    Write-Host "============================================================" `
        -ForegroundColor Green
    Write-Host ""

    exit 0

}
else {

    Write-Host ""
    Write-Host "============================================================" `
        -ForegroundColor Red
    Write-Host " QUALITY GATE FAILED" `
        -ForegroundColor Red
    Write-Host "============================================================" `
        -ForegroundColor Red

    Write-Host ""
    Write-Host "Diagnostic logs:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "    $RunLogDir"
    Write-Host ""
    Write-Host "Give Claude Code this instruction:"
    Write-Host ""
    Write-Host (
        '    Inspect the latest quality-gate logs under ' +
        'Scratch\Log. Read LATEST.txt, then diagnose the failures ' +
        'from summary.log and the corresponding pytest.log, ' +
        'ruff.log, or mypy.log. Identify the root cause before ' +
        'modifying code.'
    ) -ForegroundColor Yellow
    Write-Host ""

    exit 1
}