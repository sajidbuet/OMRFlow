<#
.SYNOPSIS
    Prepare an OMRFlow release.

.DESCRIPTION
    The human-facing entry point. It finds the repository, finds a Python
    interpreter, and hands the work to scripts/release.py, whose exit code it
    returns unchanged.

    Deliberately thin. Every decision a release makes - what the current
    version is, whether the tree is clean, whether the requested version moves
    forward, what to edit, what to run - belongs in the Python script, where
    it can be unit tested. This file exists so that a maintainer on Windows
    can type one command.

    Running it with no arguments does not start a release. It reports the
    current version, explains that a target is required, and exits non-zero.

.PARAMETER Version
    The version to release, without the tag's leading 'v' - for example
    0.1.0-alpha.3. Not mandatory on purpose: a mandatory parameter makes
    PowerShell stop and prompt ("Supply values for the following
    parameters:"), which is a worse error message than the one this script
    prints itself.

.PARAMETER DryRun
    Check everything and report what would happen, changing nothing.

.PARAMETER Help
    Show usage, including the current repository version. Also reachable as
    -h and --help.

.EXAMPLE
    .\scripts\release.ps1
    Shows the current version and explains what is required.

.EXAMPLE
    .\scripts\release.ps1 -Version 0.1.0-alpha.3 -DryRun

.EXAMPLE
    .\scripts\release.ps1 -Version 0.1.0-alpha.3
#>
# PositionalBinding = $false is what makes '--help' work. PowerShell would
# otherwise treat it as a positional value and bind it to -Version, which then
# fails as an invalid version instead of showing help. With positional binding
# off, anything that is not a recognised switch lands in -Remaining, where this
# script can interpret it.
[CmdletBinding(PositionalBinding = $false)]
param(
    [string] $Version,

    [switch] $DryRun,

    [Alias('h')]
    [switch] $Help,

    # PowerShell parameters are named with a single dash, so '--help' does not
    # bind to -Help; it arrives here as a plain argument. Collecting the
    # remainder lets this script recognise the GNU spelling people actually
    # type, and reject anything else with a real message instead of a
    # ParameterBindingException.
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Remaining
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$releaseScript = Join-Path $PSScriptRoot 'release.py'

function Find-Python {
    <#
    .SYNOPSIS
        The interpreter to run release.py with.
    .DESCRIPTION
        Prefers the repository's virtual environment, because that is where
        ruff, mypy and pytest are installed and the release gates need all
        three. Falls back to whatever Python is on PATH, then to the Windows
        launcher.
    #>
    $candidates = @(
        (Join-Path $repositoryRoot '.venv\Scripts\python.exe'),
        (Join-Path $repositoryRoot '.venv\bin\python')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    foreach ($name in @('python', 'python3')) {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }
    $launcher = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($launcher) { return $launcher.Source }
    return $null
}

function Get-CurrentVersion {
    <#
    .SYNOPSIS
        The version the repository declares, read by release.py.
    .DESCRIPTION
        Asks the Python script rather than parsing anything here. There is one
        implementation of "what version is this", so PowerShell and Python
        cannot report different answers. Returns $null if it cannot be read.
    #>
    param([string] $Python)

    $value = & $Python $releaseScript --current-version 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $value) { return $null }
    return ($value | Select-Object -First 1).Trim()
}

function Get-SuggestedVersion {
    <#
    .SYNOPSIS
        A plausible next version, for the example in a usage message.
    #>
    param([string] $Python)

    $value = & $Python $releaseScript --suggest-next-version 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $value) { return $null }
    return ($value | Select-Object -First 1).Trim()
}

function Show-Usage {
    <#
    .SYNOPSIS
        The usage block, shared by -Help and the no-argument case.
    #>
    param([string] $Example)

    Write-Host 'Usage:'
    Write-Host '  .\scripts\release.ps1 -Version <version> [-DryRun]'
    Write-Host ''
    Write-Host 'Options:'
    Write-Host '  -Version <version>  The version to release, without the leading "v",'
    Write-Host '                      for example 0.1.0-alpha.3.'
    Write-Host '  -DryRun             Check everything and report. Changes nothing.'
    Write-Host '  -Help, -h, --help   This message.'
    Write-Host ''
    Write-Host 'Example:'
    Write-Host "  .\scripts\release.ps1 -Version $Example"
}

# --------------------------------------------------------------------------
# Argument handling
# --------------------------------------------------------------------------
$helpWanted = $Help.IsPresent
$unknown = @()
if ($Remaining) {
    foreach ($argument in $Remaining) {
        if ($argument -in @('--help', '-help', '--h', '-?', '/?')) { $helpWanted = $true }
        else { $unknown += $argument }
    }
}

$python = Find-Python
if (-not $python) {
    Write-Host 'OMRFlow release automation'
    Write-Host ''
    Write-Error @'
No Python interpreter was found.

Expected the repository virtual environment at .venv\Scripts\python.exe, or
`python` on PATH. Create the environment with:

    py -3.12 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -e ".[dev]"

No release actions were performed.
'@
    exit 1
}

if (-not (Test-Path -LiteralPath $releaseScript)) {
    Write-Error "Missing $releaseScript. No release actions were performed."
    exit 1
}

$currentVersion = Get-CurrentVersion -Python $python
if (-not $currentVersion) {
    Write-Host 'OMRFlow release automation'
    Write-Host ''
    Write-Error @'
Unable to determine the current OMRFlow version from the repository metadata.

No release actions were performed.
'@
    exit 1
}

if ($unknown.Count -gt 0) {
    Write-Host 'OMRFlow release automation'
    Write-Host ''
    Write-Host "Current repository version: $currentVersion"
    Write-Host ''
    Write-Host ("ERROR: Unrecognised argument(s): {0}" -f ($unknown -join ', ')) `
        -ForegroundColor Red
    Write-Host ''
    Show-Usage -Example (Get-SuggestedVersion -Python $python)
    exit 2
}

if ($helpWanted) {
    Write-Host 'OMRFlow release automation'
    Write-Host ''
    Write-Host "Current repository version: $currentVersion"
    Write-Host ''
    Show-Usage -Example (Get-SuggestedVersion -Python $python)
    Write-Host ''
    Write-Host 'What happens when you release:'
    Write-Host '  This script checks the repository, updates the version metadata,'
    Write-Host '  runs lint, types and the full test suite, commits, creates an'
    Write-Host '  annotated tag, and pushes the commit and tag together.'
    Write-Host ''
    Write-Host '  GitHub Actions then builds the installer from that tag, verifies'
    Write-Host '  it, writes SHA256SUMS.txt and publishes the GitHub release.'
    Write-Host '  Zenodo archives the release once GitHub publishes it.'
    exit 0
}

if (-not $Version) {
    $suggested = Get-SuggestedVersion -Python $python
    Write-Host 'OMRFlow release automation'
    Write-Host ''
    Write-Host "Current repository version: $currentVersion"
    Write-Host ''
    Write-Host 'ERROR: A release version is required.' -ForegroundColor Red
    Write-Host ''
    Show-Usage -Example $suggested
    Write-Host ''
    Write-Host 'For full usage information:'
    Write-Host '  .\scripts\release.ps1 --help'
    exit 2
}

# --------------------------------------------------------------------------
# Hand over
# --------------------------------------------------------------------------
# '--version=<value>' rather than two arguments: a value that begins with a
# dash - which is what a mistyped switch looks like once PowerShell has bound
# it positionally - would otherwise be read by argparse as the next option
# instead of as the version, and the error would name the wrong problem.
$arguments = @($releaseScript, "--version=$Version")
if ($DryRun.IsPresent) { $arguments += '--dry-run' }

& $python @arguments
exit $LASTEXITCODE
