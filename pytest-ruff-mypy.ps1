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
#       pytest-progress.jsonl   one event per test (see tools\gate_progress.py)
#       ruff.log
#       mypy.log
#       git-status-after.log
#       summary.log
#
# Scratch\Log\LATEST.txt contains the path of the newest run.
# Scratch\Log\pytest-durations.json keeps per-test durations across runs,
# for the time estimate.
#
# The script continues through all checks even if one fails.
#
# Progress is shown while it runs, refreshed twice a second: a bar for the
# whole gate (check 1 of 3, ...) and one for the check running. For pytest
# that bar shows tests done of total, failures so far, elapsed time, an
# estimate of the time left and the test running now (and for how long).
# The window title carries the same figures, for the taskbar. The estimate
# comes from the durations recorded on earlier runs; the first run has none
# and uses its own average per test.
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

$PytestProgressLog = Join-Path $RunLogDir "pytest-progress.jsonl"
$PytestDurations   = Join-Path $LogRoot "pytest-durations.json"

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
# Progress display
#
# Bar 0 is the whole gate; bar 1 is the check that is running.
# The window title repeats the progress, so it is visible from the
# taskbar. A host without a window title is ignored.
# ============================================================================

$TotalChecks = 3
$CheckIndex = 0

try {
    $OriginalWindowTitle = $Host.UI.RawUI.WindowTitle
}
catch {
    $OriginalWindowTitle = $null
}


function Set-GateWindowTitle {

    param (
        [Parameter(Mandatory = $true)]
        [string]$Title
    )

    try {
        $Host.UI.RawUI.WindowTitle = $Title
    }
    catch {
    }
}


function Format-Elapsed {

    param (
        [Parameter(Mandatory = $true)]
        [TimeSpan]$Span
    )

    return "{0:hh\:mm\:ss}" -f $Span
}


# pytest progress comes from tools\gate_progress.py, a pytest plugin the
# gate loads with -p. It writes one JSON event per line to
# pytest-progress.jsonl in the run's log folder (collected, start and
# finish of each test, finished), with its own estimate of the time left;
# pytest's console output is not parsed. The file also shows which test
# was running if pytest dies.
#
# The estimate uses Scratch\Log\pytest-durations.json, the per-test
# durations the plugin recorded on earlier runs. The first run has none
# and estimates from its own average per test.

$PytestProgress = @{
    Path        = $PytestProgressLog
    Reader      = $null
    Pending     = ""
    Phase       = "starting"
    Total       = 0
    Known       = 0
    Done        = 0
    Failed      = 0
    Current     = ""
    CurrentAt   = $null
    Remaining   = $null
    RemainingAt = $null
    Basis       = ""
}


function Read-PytestProgressEvents {

    $State = $script:PytestProgress

    if ($null -eq $State.Reader) {

        if (-not (Test-Path -LiteralPath $State.Path)) {
            return
        }

        try {
            $Stream = [System.IO.FileStream]::new(
                $State.Path,
                [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::Read,
                [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
            )
        }
        catch {
            return
        }

        $State.Reader = [System.IO.StreamReader]::new(
            $Stream,
            [System.Text.UTF8Encoding]::new($false)
        )
    }

    $State.Pending += $State.Reader.ReadToEnd()

    # The last piece may be a line the plugin has not finished writing.
    $Pieces = $State.Pending -split "`n"
    $State.Pending = $Pieces[$Pieces.Count - 1]

    for ($Index = 0; $Index -lt $Pieces.Count - 1; $Index++) {

        $Text = $Pieces[$Index].Trim()

        if ($Text -eq "") {
            continue
        }

        try {
            $Item = $Text | ConvertFrom-Json
        }
        catch {
            continue
        }

        switch ($Item.event) {

            "collected" {
                $State.Phase = "running"
                $State.Total = [int]$Item.total
                $State.Known = [int]$Item.known
            }

            "start" {
                $State.Current = [string]$Item.nodeid
                $State.CurrentAt = Get-Date
            }

            "finish" {
                $State.Done = [int]$Item.done
                $State.Failed = [int]$Item.failed
                $State.Basis = [string]$Item.basis

                if ($null -ne $Item.remaining_s) {
                    $State.Remaining = [double]$Item.remaining_s
                    $State.RemainingAt = Get-Date
                }
            }

            "finished" {
                $State.Phase = "finished"
            }
        }
    }
}


function Close-PytestProgress {

    $State = $script:PytestProgress

    if ($null -ne $State.Reader) {
        $State.Reader.Dispose()
        $State.Reader = $null
    }
}


function Update-PytestProgress {

    param (
        [Parameter(Mandatory = $true)]
        [TimeSpan]$Elapsed
    )

    Read-PytestProgressEvents

    $State = $script:PytestProgress
    $ElapsedText = Format-Elapsed $Elapsed

    if ($State.Phase -eq "starting") {

        Write-Progress `
            -Id 1 `
            -ParentId 0 `
            -Activity "pytest" `
            -Status "Collecting tests | elapsed $ElapsedText"

        Set-GateWindowTitle "OMRFlow gate - pytest collecting, $ElapsedText"
        return
    }

    if ($State.Phase -eq "finished") {

        Write-Progress `
            -Id 1 `
            -ParentId 0 `
            -Activity "pytest" `
            -Status ("All {0:N0} tests run, failed: {1} | elapsed {2} | writing the summary" -f `
                $State.Done, $State.Failed, $ElapsedText) `
            -PercentComplete 100

        return
    }

    $Percent = 0

    if ($State.Total -gt 0) {
        $Percent = [int][math]::Floor(100 * [math]::Min($State.Done, $State.Total) / $State.Total)
    }

    # The plugin's estimate counts down between finished tests.
    if ($null -eq $State.Remaining) {

        $RemainingText = "estimating"
        $ShortRemaining = ""
    }
    else {

        $Left = $State.Remaining - ((Get-Date) - $State.RemainingAt).TotalSeconds
        $Left = [math]::Max(0, [math]::Round($Left))
        $LeftText = Format-Elapsed ([TimeSpan]::FromSeconds($Left))

        if ($State.Basis -eq "previous-run") {
            $RemainingText = "~$LeftText (from earlier timings)"
        }
        else {
            $RemainingText = "~$LeftText (from this run's average)"
        }

        $ShortRemaining = ", ~$LeftText left"
    }

    $Status = "{0:N0} / {1:N0} tests ({2}%) | failed: {3} | elapsed {4} | remaining {5}" -f `
        $State.Done,
        $State.Total,
        $Percent,
        $State.Failed,
        $ElapsedText,
        $RemainingText

    $Operation = "Starting"

    if ($State.Current) {

        $For = [int]((Get-Date) - $State.CurrentAt).TotalSeconds
        $Operation = "Running $($State.Current) (for $For s)"
    }

    Write-Progress `
        -Id 1 `
        -ParentId 0 `
        -Activity "pytest" `
        -Status $Status `
        -CurrentOperation $Operation `
        -PercentComplete $Percent

    Set-GateWindowTitle (
        "[$Percent%] OMRFlow gate - pytest, failed: $($State.Failed)$ShortRemaining"
    )
}


# ============================================================================
# Run one native command, streaming its output
#
# STDOUT and STDERR are read as they arrive - not line by line - so the
# console shows each test's result character the moment pytest prints it,
# and the loop wakes at least twice a second to refresh the progress bars.
# STDERR (e.g. an OpenCV warning from a test) is logged output, never an
# error: the exit code alone decides PASS / FAIL. If the script is
# stopped (Ctrl+C), the command and its children are killed rather than
# left running.
# ============================================================================

function ConvertTo-CommandLineArgument {

    param (
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Value
    )

    if ($Value -ne "" -and $Value -notmatch '[\s"]') {
        return $Value
    }

    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}


function Invoke-StreamingCommand {

    param (
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [string[]]$Arguments = @(),

        [Parameter(Mandatory = $true)]
        [System.IO.StreamWriter]$LogWriter,

        [Parameter(Mandatory = $true)]
        [scriptblock]$OnTick
    )

    $Info = [System.Diagnostics.ProcessStartInfo]::new()
    $Info.FileName = $FilePath
    $Info.Arguments = (
        $Arguments | ForEach-Object { ConvertTo-CommandLineArgument $_ }
    ) -join " "
    $Info.WorkingDirectory = (Get-Location).Path
    $Info.UseShellExecute = $false
    $Info.RedirectStandardOutput = $true
    $Info.RedirectStandardError = $true

    # The encoding PowerShell itself decodes native output with.
    $Info.StandardOutputEncoding = [Console]::OutputEncoding
    $Info.StandardErrorEncoding = [Console]::OutputEncoding

    $Start = Get-Date
    $Process = [System.Diagnostics.Process]::Start($Info)

    try {

        $OutBuffer = [char[]]::new(8192)
        $ErrBuffer = [char[]]::new(8192)

        $OutTask = $Process.StandardOutput.ReadAsync($OutBuffer, 0, $OutBuffer.Length)
        $ErrTask = $Process.StandardError.ReadAsync($ErrBuffer, 0, $ErrBuffer.Length)

        $LastTick = [datetime]::MinValue

        while (($null -ne $OutTask) -or ($null -ne $ErrTask)) {

            $Pending = [System.Threading.Tasks.Task[]]@(
                @($OutTask, $ErrTask) | Where-Object { $null -ne $_ }
            )

            $null = [System.Threading.Tasks.Task]::WaitAny($Pending, 500)

            if (($null -ne $OutTask) -and $OutTask.IsCompleted) {

                $Count = $OutTask.Result

                if ($Count -eq 0) {
                    $OutTask = $null
                }
                else {
                    $Text = [string]::new($OutBuffer, 0, $Count)
                    $LogWriter.Write($Text)
                    Write-Host $Text -NoNewline
                    $OutTask = $Process.StandardOutput.ReadAsync($OutBuffer, 0, $OutBuffer.Length)
                }
            }

            if (($null -ne $ErrTask) -and $ErrTask.IsCompleted) {

                $Count = $ErrTask.Result

                if ($Count -eq 0) {
                    $ErrTask = $null
                }
                else {
                    $Text = [string]::new($ErrBuffer, 0, $Count)
                    $LogWriter.Write($Text)
                    Write-Host $Text -NoNewline -ForegroundColor DarkYellow
                    $ErrTask = $Process.StandardError.ReadAsync($ErrBuffer, 0, $ErrBuffer.Length)
                }
            }

            $Now = Get-Date

            if (($null -ne $OnTick) -and (($Now - $LastTick).TotalMilliseconds -ge 250)) {

                $LastTick = $Now

                # The display is a convenience: if it fails, say so once and
                # carry on without it rather than fail the check.
                try {
                    & $OnTick ($Now - $Start)
                }
                catch {
                    $OnTick = $null
                    Write-Host ""
                    Write-Host "Progress display stopped: $($_.Exception.Message)" `
                        -ForegroundColor Yellow
                }
            }
        }

        $Process.WaitForExit()

        if ($null -ne $OnTick) {
            try {
                & $OnTick ((Get-Date) - $Start)
            }
            catch {
            }
        }

        return $Process.ExitCode
    }
    finally {

        if (-not $Process.HasExited) {

            try {
                # Kill(bool), the whole tree, is .NET Core 3+ (PowerShell 7).
                $Process.Kill($true)
            }
            catch {
                try {
                    $null = & taskkill.exe /T /F /PID $Process.Id 2>&1
                }
                catch {
                }
            }
        }

        $Process.Dispose()
    }
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

        # Arguments to $Python.
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        # Called with the elapsed time at least twice a second, to drive
        # the check's progress bar. The default shows the elapsed time.
        [scriptblock]$OnTick = $null
    )


    Write-Section $Name

    $StartTime = Get-Date

    $script:CheckIndex += 1

    Write-Progress `
        -Id 0 `
        -Activity "OMRFlow quality gate" `
        -Status "Check $script:CheckIndex of ${TotalChecks}: $Name" `
        -PercentComplete ([int](100 * ($script:CheckIndex - 1) / $TotalChecks))

    Write-Progress `
        -Id 1 `
        -ParentId 0 `
        -Activity $Name `
        -Status "Running (started $($StartTime.ToString('HH:mm:ss')))"

    Set-GateWindowTitle (
        "[check $script:CheckIndex/$TotalChecks] OMRFlow gate - $Name"
    )


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

        # STDOUT and STDERR go, as they arrive, to the console and to the
        # diagnostic log (flushed as written, so the log is complete up to
        # the moment a run is killed).

        $LogWriter = [System.IO.StreamWriter]::new(
            $LogFile,
            $true,
            [System.Text.UTF8Encoding]::new($false)
        )
        $LogWriter.AutoFlush = $true

        if ($null -eq $OnTick) {

            $OnTick = {

                param ($Elapsed)

                Write-Progress `
                    -Id 1 `
                    -ParentId 0 `
                    -Activity $Name `
                    -Status "Running | elapsed $(Format-Elapsed $Elapsed)"
            }
        }

        try {

            $ExitCode = Invoke-StreamingCommand `
                -FilePath $Python `
                -Arguments $Arguments `
                -LogWriter $LogWriter `
                -OnTick $OnTick
        }
        finally {
            $LogWriter.Dispose()
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

    Write-Progress -Id 1 -ParentId 0 -Activity $Name -Completed


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
    -Arguments @(
        "-m", "pytest",
        "-p", "tools.gate_progress",
        "--gate-progress-file=$PytestProgressLog",
        "--gate-durations-file=$PytestDurations"
    ) `
    -OnTick {

        param ($Elapsed)

        Update-PytestProgress $Elapsed
    }

Close-PytestProgress


# ============================================================================
# 2. RUFF
# ============================================================================

Invoke-QualityCheck `
    -Name "RUFF - Lint and static checks" `
    -LogFile $RuffLog `
    -Arguments @("-m", "ruff", "check", "src", "tests", "tools", "scripts")


# ============================================================================
# 3. MYPY
# ============================================================================

Invoke-QualityCheck `
    -Name "MYPY - Static type checking" `
    -LogFile $MypyLog `
    -Arguments @("-m", "mypy", "src")


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

Write-Progress -Id 0 -Activity "OMRFlow quality gate" -Completed

if ($null -ne $OriginalWindowTitle) {
    Set-GateWindowTitle $OriginalWindowTitle
}

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
