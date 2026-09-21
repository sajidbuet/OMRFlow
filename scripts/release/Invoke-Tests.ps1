<#
.SYNOPSIS
    Run OMRFlow's quality gates: lint, types and tests.

.DESCRIPTION
    The same three checks a pull request has to pass, in the order that
    fails fastest: ruff, then mypy, then pytest. Exits non-zero on the first
    failure unless -ContinueOnFailure is given.

    The full test suite takes roughly half an hour on an idle machine and
    considerably longer on a busy one - it processes real images, opens real
    databases and drives real Qt widgets. Use -Fast while working.

.PARAMETER Fast
    Only the unit suite: no Qt, no integration, a couple of minutes.

.PARAMETER Gui
    Only the GUI suite.

.PARAMETER ContinueOnFailure
    Run every gate even after one fails, and report them all at the end.
    Useful before a release, when you want the whole picture at once.

.EXAMPLE
    .\scripts\release\Invoke-Tests.ps1 -Fast

.EXAMPLE
    .\scripts\release\Invoke-Tests.ps1 -ContinueOnFailure
#>
[CmdletBinding()]
param(
    [switch] $Fast,
    [switch] $Gui,
    [switch] $ContinueOnFailure
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Push-Location $repositoryRoot
try {
    $python = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { $python = 'python' }

    $target = if ($Fast) { 'tests/unit' } elseif ($Gui) { 'tests/gui' } else { 'tests' }

    $gates = @(
        @{ Name = 'ruff';   Arguments = @('-m', 'ruff', 'check', 'src', 'tests') },
        @{ Name = 'mypy';   Arguments = @('-m', 'mypy', 'src/omr_scanner') },
        @{ Name = 'pytest'; Arguments = @('-m', 'pytest', $target, '-q') }
    )

    $results = [System.Collections.Generic.List[object]]::new()
    foreach ($gate in $gates) {
        Write-Host ''
        Write-Host "==> $($gate.Name)" -ForegroundColor Cyan
        $started = Get-Date
        & $python @($gate.Arguments)
        $code = $LASTEXITCODE
        $elapsed = (Get-Date) - $started
        $results.Add([pscustomobject]@{
            Gate    = $gate.Name
            Passed  = ($code -eq 0)
            Exit    = $code
            Seconds = [math]::Round($elapsed.TotalSeconds)
        })
        if ($code -ne 0 -and -not $ContinueOnFailure) { break }
    }

    Write-Host ''
    Write-Host 'Summary' -ForegroundColor Cyan
    foreach ($result in $results) {
        $mark = if ($result.Passed) { 'PASS' } else { "FAIL ($($result.Exit))" }
        $colour = if ($result.Passed) { 'Green' } else { 'Red' }
        Write-Host ("  {0,-8} {1,-12} {2,5}s" -f $result.Gate, $mark, $result.Seconds) -ForegroundColor $colour
    }

    $failures = @($results | Where-Object { -not $_.Passed })
    $skipped = $gates.Count - $results.Count
    if ($skipped -gt 0) { Write-Host "  $skipped gate(s) not run." -ForegroundColor Yellow }
    if ($failures.Count -gt 0) { exit 1 }

    Write-Host ''
    Write-Host 'All gates passed.' -ForegroundColor Green
    exit 0
}
finally {
    Pop-Location
}
