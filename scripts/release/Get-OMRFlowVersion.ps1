<#
.SYNOPSIS
    Read OMRFlow's version from its single source of truth.

.DESCRIPTION
    Every release script needs the version, and none of them may hard-code
    it. This asks the package itself, so the installer name, the checksum
    file and the Git tag can never disagree with the About dialog.

    Returns an object with:
      Version   0.1.0-alpha.1   the SemVer spelling, used in artifact names
      Release   0.1.0           without the prerelease suffix
      Channel   Alpha           derived from the version, not configured
      IsPrerelease  $true       whether a GitHub release must be a pre-release
      Build     0.1.0-alpha.1+<commit>
      Tag       v0.1.0-alpha.1
      NumericVersion  0.1.0.1   four integers, for Windows tooling

.PARAMETER Python
    Interpreter to ask. Defaults to the repository's virtual environment.

.EXAMPLE
    $v = & scripts/release/Get-OMRFlowVersion.ps1
    Write-Host "Building $($v.Version) on the $($v.Channel) channel"
#>
[CmdletBinding()]
param(
    [string] $Python
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

if (-not $Python) {
    $venvPython = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
    $Python = if (Test-Path $venvPython) { $venvPython } else { 'python' }
}

$script = @'
import json, sys
sys.path.insert(0, "src")
from omr_scanner import _version
print(json.dumps({
    "Version": _version.__version__,
    "Release": _version.RELEASE_VERSION,
    "Channel": _version.RELEASE_CHANNEL.value,
    "IsPrerelease": _version.IS_PRERELEASE,
    "Build": _version.build_identifier(),
    "Tag": _version.release_tag(),
    # Windows tooling - the executable's VERSIONINFO resource and the
    # installer's VersionInfoVersion - requires four integers and rejects
    # "0.1.0-alpha.1" outright.
    "NumericVersion": _version.numeric_version_string(),
}))
'@

# Via a temporary file rather than `python -c`: Windows PowerShell 5.1 and
# PowerShell 7 quote native arguments differently, and 5.1 strips the quotes
# from the embedded string literals, so the same -c snippet that works in one
# host fails with a NameError in the other.
$scriptFile = Join-Path ([System.IO.Path]::GetTempPath()) "omrflow-version-$PID.py"
Set-Content -LiteralPath $scriptFile -Value $script -Encoding UTF8

Push-Location $repositoryRoot
try {
    $json = & $Python $scriptFile
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read the version using '$Python' (exit $LASTEXITCODE)."
    }
}
finally {
    Pop-Location
    Remove-Item -LiteralPath $scriptFile -Force -ErrorAction SilentlyContinue
}

$json | ConvertFrom-Json
