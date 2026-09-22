<#
.SYNOPSIS
    Run the OMRFlow release qualification.

.DESCRIPTION
    A thin launcher for `tools/release_validation/validate_release.py`. It
    finds the repository's virtual environment, checks that the validation
    dependencies are installed, and hands every argument straight through - so
    anything the Python entry point accepts works here too:

        .\tools\release_validation\Run-ReleaseValidation.ps1
        .\tools\release_validation\Run-ReleaseValidation.ps1 -All
        .\tools\release_validation\Run-ReleaseValidation.ps1 --gui --accessibility

    With no arguments it runs the safe qualification: everything except the
    installer round trip, which installs and uninstalls software.

    The exit code is the framework's own: 0 when every release-blocking stage
    passed, non-zero otherwise. Nothing here interprets it, so a CI step or a
    scheduled task can use this file directly.

.PARAMETER All
    Shorthand for `--all`, including the destructive installer stage.

.PARAMETER Arguments
    Everything else, passed to validate_release.py unchanged.

.PARAMETER Python
    Interpreter to use. Defaults to the repository's .venv, then to whatever
    `python` resolves to.

.EXAMPLE
    .\tools\release_validation\Run-ReleaseValidation.ps1

.EXAMPLE
    .\tools\release_validation\Run-ReleaseValidation.ps1 -All

.EXAMPLE
    .\tools\release_validation\Run-ReleaseValidation.ps1 --installer "dist\installer\OMRFlow-0.1.0-alpha.1-Setup-x64.exe"
#>
[CmdletBinding()]
param(
    [switch] $All,
    [string] $Python,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Arguments = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$entryPoint = Join-Path $PSScriptRoot 'validate_release.py'

if (-not (Test-Path $entryPoint)) {
    Write-Error "validate_release.py is missing from $PSScriptRoot."
    exit 2
}

if (-not $Python) {
    $venvPython = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
    $Python = if (Test-Path $venvPython) { $venvPython } else { 'python' }
}

$resolved = Get-Command $Python -ErrorAction SilentlyContinue
if (-not $resolved) {
    Write-Error @"
No Python interpreter found.

Create the repository's virtual environment first:

    python -m venv .venv
    .\.venv\Scripts\pip install -e ".[dev,validation]"
"@
    exit 2
}

# Fail here, with the command to fix it, rather than inside a stage forty
# minutes in. `pytest-qt` is the one the GUI stages cannot do without.
$check = & $Python -c "import importlib.util as u; print('yes' if u.find_spec('pytestqt') else 'no')" 2>$null
if ($check -ne 'yes') {
    Write-Error @"
The validation dependencies are not installed in $Python.

    .\.venv\Scripts\pip install -e ".[dev,validation]"

That installs pytest-qt (GUI stages), pywinauto (packaged-application checks)
and Pillow (visual baselines).
"@
    exit 2
}

$arguments = @()
if ($All) { $arguments += '--all' }
$arguments += $Arguments

Write-Host "OMRFlow release qualification" -ForegroundColor Cyan
Write-Host "  interpreter : $($resolved.Source)"
Write-Host "  repository  : $repositoryRoot"
Write-Host ""

Push-Location $repositoryRoot
try {
    & $Python $entryPoint @arguments
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $code
