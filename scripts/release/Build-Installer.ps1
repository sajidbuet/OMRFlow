<#
.SYNOPSIS
    Compile the OMRFlow Windows installer with Inno Setup.

.DESCRIPTION
    Wraps the bundle built by Build-App.ps1 into
    `dist/installer/OMRFlow-<version>-Setup-x64.exe`.

    Requires Inno Setup 6 (https://jrsoftware.org/isdl.php). It is a
    developer tool and is not installed automatically; if it is missing this
    script says exactly how to install it and exits non-zero rather than
    silently producing nothing.

        winget install --id JRSoftware.InnoSetup

    The version is read from the package and passed to the compiler, so the
    installer, the executable it installs and the About dialog cannot
    disagree.

    The resulting installer is UNSIGNED. Windows SmartScreen will warn about
    it. See docs/wiki/Installation.md - signing is a Phase 11C item and is
    not silently implied anywhere.

.PARAMETER Iscc
    Path to ISCC.exe. Found automatically in the usual locations.

.EXAMPLE
    .\scripts\release\Build-Installer.ps1
#>
[CmdletBinding()]
param(
    [string] $Iscc
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

function Find-Iscc {
    # `Get-Command` returns nothing when the command is absent, and under
    # Set-StrictMode reading a property off that is an error rather than
    # $null - so it is resolved separately instead of inline.
    $onPath = Get-Command 'iscc' -ErrorAction SilentlyContinue
    $candidates = @(
        $(if ($onPath) { $onPath.Source }),
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    return $null
}

try {
    $version = & (Join-Path $PSScriptRoot 'Get-OMRFlowVersion.ps1')
    Write-Host "Building the installer for OMRFlow $($version.Version)" -ForegroundColor Cyan

    $bundle = Join-Path $repositoryRoot 'dist\OMRFlow'
    if (-not (Test-Path (Join-Path $bundle 'OMRFlow.exe'))) {
        throw "No application bundle at $bundle. Run Build-App.ps1 first."
    }

    $builtVersion = (Get-Item (Join-Path $bundle 'OMRFlow.exe')).VersionInfo.ProductVersion
    if ($builtVersion -ne $version.Version) {
        throw "The bundle in dist\OMRFlow is version '$builtVersion' but the package says '$($version.Version)'. Rebuild it."
    }

    if (-not $Iscc) { $Iscc = Find-Iscc }
    if (-not $Iscc) {
        Write-Host ''
        Write-Error @'
Inno Setup 6 was not found, so the installer cannot be compiled here.

Install it with:

    winget install --id JRSoftware.InnoSetup

or download it from https://jrsoftware.org/isdl.php, then run this script
again. The application bundle in dist\OMRFlow is already built and is
unaffected.
'@
        exit 1
    }
    Write-Host "  compiler : $Iscc"

    $outputDir = Join-Path $repositoryRoot 'dist\installer'
    New-Item -ItemType Directory -Force $outputDir | Out-Null

    & $Iscc `
        "/DAppVersion=$($version.Version)" `
        "/DAppNumericVersion=$($version.NumericVersion)" `
        "/DSourceDir=$bundle" `
        "/DOutputDir=$outputDir" `
        (Join-Path $repositoryRoot 'packaging\omrflow.iss')
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed ($LASTEXITCODE)." }

    $installer = Join-Path $outputDir "OMRFlow-$($version.Version)-Setup-x64.exe"
    if (-not (Test-Path $installer)) {
        throw "Inno Setup reported success but $installer does not exist."
    }

    $size = [math]::Round((Get-Item $installer).Length / 1MB, 1)
    Write-Host ''
    Write-Host "Built $installer  -  $size MB" -ForegroundColor Green
    Write-Host '  UNSIGNED: Windows SmartScreen will warn. See docs/wiki/Installation.md.'
    Write-Host "  next    : .\scripts\release\New-Checksums.ps1"
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
