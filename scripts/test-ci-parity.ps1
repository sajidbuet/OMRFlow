# test-ci-parity.ps1
#
# Reproduce, on a Windows development machine, the two conditions GitHub CI
# runs under and the native gate (pytest-ruff-mypy.ps1) does not:
#
#   1. mypy analysing Linux    - the Ubuntu "Lint and type check" job
#   2. Qt offscreen GUI tests  - both "Tests" jobs set QT_QPA_PLATFORM=offscreen
#
# Not a replacement for the native gate: that one is what tests the product's
# real desktop geometry. See docs/TESTING.md, "Native Windows gate vs GitHub's
# offscreen gate".
#
# Usage:
#
#   .\scripts\test-ci-parity.ps1                 # mypy + the GUI layout modules
#   .\scripts\test-ci-parity.ps1 -AllGui         # mypy + all of tests/gui (~15+ min)
#
# Exit code: 0 when both pass, 1 otherwise.

param(
    [switch]$AllGui
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

$failed = $false

Write-Host "== mypy --platform linux" -ForegroundColor Cyan
& $Python -m mypy --platform linux src/omr_scanner
if ($LASTEXITCODE -ne 0) { $failed = $true }

$targets = if ($AllGui) {
    @("tests/gui")
} else {
    @(
        "tests/gui/test_resolve_page.py",
        "tests/gui/test_resolve_provenance_gui.py",
        "tests/gui/test_ui_zoom.py",
        "tests/gui/test_workflow_ribbon.py"
    )
}

Write-Host "== pytest, QT_QPA_PLATFORM=offscreen: $($targets -join ' ')" -ForegroundColor Cyan
$previous = $env:QT_QPA_PLATFORM
$env:QT_QPA_PLATFORM = "offscreen"
try {
    & $Python -m pytest -q -rs @targets
    if ($LASTEXITCODE -ne 0) { $failed = $true }
} finally {
    $env:QT_QPA_PLATFORM = $previous
}

if ($failed) {
    Write-Host "CI parity: FAIL" -ForegroundColor Red
    exit 1
}
Write-Host "CI parity: PASS" -ForegroundColor Green
exit 0
