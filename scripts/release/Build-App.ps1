<#
.SYNOPSIS
    Build the OMRFlow Windows application bundle with PyInstaller.

.DESCRIPTION
    Produces `dist/OMRFlow/OMRFlow.exe` and its runtime, a self-contained
    directory that needs no Python installed on the target machine.

    The version is read from the package rather than passed in, and is
    stamped into the executable's Windows metadata, so the file-properties
    dialog, the About dialog and the installer all report the same build.

    The source commit is baked in through OMRFLOW_BUILD_COMMIT: a packaged
    build has no .git directory to ask, and an artifact that cannot be traced
    back to a revision is not much use in a bug report.

.PARAMETER Clean
    Remove previous build and dist output first. Slower, and what a release
    build should always do - a stale file left in `dist` ships.

.PARAMETER SkipVersionResource
    Do not regenerate the Windows VERSIONINFO resource. For quick iteration
    only; a release build must not use it.

.EXAMPLE
    .\scripts\release\Build-App.ps1 -Clean
#>
[CmdletBinding()]
param(
    [switch] $Clean,
    [switch] $SkipVersionResource
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Push-Location $repositoryRoot
try {
    $python = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { $python = 'python' }

    $version = & (Join-Path $PSScriptRoot 'Get-OMRFlowVersion.ps1')
    Write-Host "OMRFlow $($version.Version) - $($version.Channel) channel" -ForegroundColor Cyan

    # Traceability: the commit is unavailable inside a frozen build, so it is
    # recorded at build time and read back by `_version.build_identifier()`.
    $commit = (& git rev-parse --short HEAD 2>$null)
    if ($LASTEXITCODE -eq 0 -and $commit) {
        $env:OMRFLOW_BUILD_COMMIT = $commit.Trim()
        Write-Host "  source commit : $($env:OMRFLOW_BUILD_COMMIT)"
    }
    else {
        Write-Warning 'Not a Git checkout: the build will not record a source commit.'
    }

    $dirty = (& git status --porcelain 2>$null)
    if ($LASTEXITCODE -eq 0 -and $dirty) {
        Write-Warning 'The working tree has uncommitted changes. A release build should be made from a clean tree.'
    }

    if (-not $SkipVersionResource) {
        Write-Host '  generating Windows version resource...'
        & $python (Join-Path $repositoryRoot 'packaging\make_version_info.py') `
            (Join-Path $repositoryRoot 'packaging\build\file_version_info.txt')
        if ($LASTEXITCODE -ne 0) { throw "Version resource generation failed ($LASTEXITCODE)." }
    }

    if ($Clean) {
        Write-Host '  cleaning previous output...'
        foreach ($path in @('dist\OMRFlow', 'build\pyinstaller')) {
            $full = Join-Path $repositoryRoot $path
            if (Test-Path $full) { Remove-Item -Recurse -Force $full }
        }
    }

    Write-Host '  running PyInstaller (this takes a few minutes)...'
    $arguments = @(
        '-m', 'PyInstaller',
        '--noconfirm',
        '--distpath', (Join-Path $repositoryRoot 'dist'),
        '--workpath', (Join-Path $repositoryRoot 'build\pyinstaller'),
        (Join-Path $repositoryRoot 'packaging\omrflow.spec')
    )
    if ($Clean) { $arguments = @($arguments[0..1]) + '--clean' + $arguments[2..($arguments.Length - 1)] }

    & $python @arguments
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)." }

    $exe = Join-Path $repositoryRoot 'dist\OMRFlow\OMRFlow.exe'
    if (-not (Test-Path $exe)) { throw "PyInstaller reported success but $exe does not exist." }

    $info = (Get-Item $exe).VersionInfo
    # Trimmed: Windows pads the version strings in a binary's resources, so
    # a direct comparison fails on a version that is in fact correct.
    $reportedVersion = ($info.ProductVersion ?? '').Trim()
    if ($reportedVersion -ne $version.Version) {
        throw "The built executable reports version '$reportedVersion' but the package says '$($version.Version)'."
    }

    $bytes = (Get-ChildItem (Join-Path $repositoryRoot 'dist\OMRFlow') -Recurse -File |
        Measure-Object -Property Length -Sum).Sum
    Write-Host ''
    Write-Host "Built dist\OMRFlow  -  $([math]::Round($bytes / 1MB)) MB" -ForegroundColor Green
    Write-Host "  version : $reportedVersion"
    Write-Host "  next    : .\scripts\release\Test-PackagedApp.ps1"
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
finally {
    Pop-Location
}

