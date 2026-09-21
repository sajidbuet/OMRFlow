<#
.SYNOPSIS
    Run the Phase 10 100,000-sheet release qualification, unattended.

.DESCRIPTION
    A thin convenience wrapper around

        python -m omr_scanner.tools.phase10_qualification

    It exists for one reason: to remove the chance of typing the campaign's
    arguments wrongly at midnight before a multi-hour run. It adds no
    behaviour of its own, makes no decisions, and changes nothing about how
    the campaign is measured - see docs/phase10_qualification.md.

    The campaign runs headless and unattended. It deliberately force-kills
    processes; that is the test. Leave the machine alone once it starts.

    This script never changes a Windows power setting. Disable sleep
    yourself if your machine would otherwise suspend during the run.

.PARAMETER OutputDir
    The one directory the campaign owns. Everything is written here and
    nothing outside it is created or removed. Needs tens of GB free - run
    -Preflight to be told exactly how much.

.PARAMETER Template
    The .omrt template every run reads with. Defaults to the 100-question
    example in this repository.

.PARAMETER Sheets
    Logical sheets per run. Defaults to 100000. Lower it only to validate
    the harness: a smaller campaign is not the release qualification, and
    its report says so in its headline.

.PARAMETER Workers
    Worker processes per run. 0 (the default) uses half the logical CPUs.

.PARAMETER Preflight
    Check whether the campaign can finish, print the estimates, and exit.
    Changes nothing.

.PARAMETER Resume
    Continue an interrupted campaign in -OutputDir instead of starting one.
    Runs already verified are skipped.

.PARAMETER Status
    Print an existing campaign's progress and exit. Read-only, and safe to
    run against a campaign that is in progress.

.EXAMPLE
    .\run_phase10_100k_qualification.ps1 -OutputDir D:\OMRflow-qualification -Preflight

.EXAMPLE
    .\run_phase10_100k_qualification.ps1 -OutputDir D:\OMRflow-qualification

.EXAMPLE
    .\run_phase10_100k_qualification.ps1 -OutputDir D:\OMRflow-qualification -Status

.EXAMPLE
    .\run_phase10_100k_qualification.ps1 -OutputDir D:\OMRflow-qualification -Resume
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $OutputDir,

    [string] $Template = "examples\templates\100_question_4_choice_example.omrt",

    [int] $Sheets = 100000,

    [int] $Workers = 0,

    [switch] $Preflight,

    [switch] $Resume,

    [switch] $Status
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    Write-Error "No virtual environment at $python. Create it and install the project first (see README.md)."
    exit 2
}

$exclusive = @($Preflight, $Resume, $Status) | Where-Object { $_ }
if ($exclusive.Count -gt 1) {
    Write-Error "Choose at most one of -Preflight, -Resume and -Status."
    exit 2
}

$module = "omr_scanner.tools.phase10_qualification"

if ($Status) {
    & $python -m $module status --output-dir $OutputDir
    exit $LASTEXITCODE
}

if ($Resume) {
    & $python -m $module resume --output-dir $OutputDir
    exit $LASTEXITCODE
}

$command = if ($Preflight) { "preflight" } else { "run" }
$arguments = @(
    "-m", $module, $command,
    "--output-dir", $OutputDir,
    "--template", $Template,
    "--sheets", $Sheets,
    "--workers", $Workers
)

if ($command -eq "run") {
    Write-Host ""
    Write-Host "Phase 10 qualification campaign" -ForegroundColor Cyan
    Write-Host "  Output:   $OutputDir"
    Write-Host "  Sheets:   $Sheets per run"
    Write-Host "  Template: $Template"
    Write-Host ""
    Write-Host "This runs unattended for many hours and force-kills processes on" -ForegroundColor Yellow
    Write-Host "purpose. Leave the machine alone; make sure it will not sleep." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Watch it from another window with:"
    Write-Host "  .\run_phase10_100k_qualification.ps1 -OutputDir `"$OutputDir`" -Status"
    Write-Host ""
}

& $python @arguments
$code = $LASTEXITCODE

if ($command -eq "run") {
    Write-Host ""
    switch ($code) {
        0 { Write-Host "Finished. Read $OutputDir\qualification_summary.md" -ForegroundColor Green }
        1 { Write-Host "The campaign did NOT qualify. Read $OutputDir\qualification_summary.md - failed runs' projects have been kept as evidence." -ForegroundColor Red }
        130 { Write-Host "Interrupted. Continue with -Resume." -ForegroundColor Yellow }
        default { Write-Host "Exited with code $code." -ForegroundColor Red }
    }
}

exit $code
