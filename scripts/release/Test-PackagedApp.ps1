<#
.SYNOPSIS
    Smoke-test the packaged OMRFlow build.

.DESCRIPTION
    Launches `dist/OMRFlow/OMRFlow.exe` exactly as a user would - no Python
    on the PATH, no source tree on sys.path - and checks that it starts,
    reports the right version, stays responsive and shuts down cleanly.

    This is the test that catches what a unit suite cannot: a resource that
    was never bundled, a Qt plugin that did not come along, an import that
    only worked because the source tree was importable. Every one of those
    fails here and passes everywhere else.

    Requires an interactive Windows desktop; the application briefly appears
    on screen.

.PARAMETER BundleDirectory
    The built bundle. Defaults to `dist/OMRFlow`.

.PARAMETER TimeoutSeconds
    How long to wait for the window. A cold first launch unpacking Qt can
    take a while on a slow disk.

.EXAMPLE
    .\scripts\release\Test-PackagedApp.ps1
#>
[CmdletBinding()]
param(
    [string] $BundleDirectory,
    [int] $TimeoutSeconds = 90
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $BundleDirectory) { $BundleDirectory = Join-Path $repositoryRoot 'dist\OMRFlow' }

$checks = [System.Collections.Generic.List[object]]::new()
function Add-Check([string] $Name, [bool] $Passed, [string] $Detail = '') {
    $checks.Add([pscustomobject]@{ Name = $Name; Passed = $Passed; Detail = $Detail })
    $mark = if ($Passed) { 'PASS' } else { 'FAIL' }
    $colour = if ($Passed) { 'Green' } else { 'Red' }
    Write-Host ("  {0,-4} {1,-44} {2}" -f $mark, $Name, $Detail) -ForegroundColor $colour
}

try {
    $version = & (Join-Path $PSScriptRoot 'Get-OMRFlowVersion.ps1')
    Write-Host "Smoke-testing the packaged build of OMRFlow $($version.Version)" -ForegroundColor Cyan
    Write-Host ''

    $exe = Join-Path $BundleDirectory 'OMRFlow.exe'
    Add-Check 'bundle directory exists' (Test-Path $BundleDirectory) $BundleDirectory
    Add-Check 'executable exists' (Test-Path $exe)
    if (-not (Test-Path $exe)) { throw "No packaged build at $BundleDirectory. Run Build-App.ps1 first." }

    # Trimmed: Windows pads the version strings in a binary's resources.
    $info = (Get-Item $exe).VersionInfo
    $reportedVersion = ($info.ProductVersion ?? '').Trim()
    $reportedName = ($info.ProductName ?? '').Trim()
    Add-Check 'executable reports the right version' ($reportedVersion -eq $version.Version) $reportedVersion
    Add-Check 'executable names the product' ($reportedName -eq 'OMRFlow') $reportedName

    # Bundled at build time and loaded through importlib.resources at run
    # time, so nothing imports them and a packaging mistake is invisible
    # until the interface is missing its icons.
    $resources = @(
        '_internal\omr_scanner\gui\resources\branding\logo.svg',
        '_internal\omr_scanner\gui\resources\branding\icon.ico',
        '_internal\omr_scanner\gui\resources\icons\lucide\menu.svg',
        '_internal\omr_scanner\gui\resources\icons\lucide\folder.svg'
    )
    foreach ($relative in $resources) {
        Add-Check "bundled: $(Split-Path $relative -Leaf)" (Test-Path (Join-Path $BundleDirectory $relative))
    }

    # The application must not need a development Python. Qt's own runtime
    # has to be inside the bundle.
    Add-Check 'Qt runtime is bundled' (
        (Test-Path (Join-Path $BundleDirectory '_internal\PySide6\Qt6Core.dll')) -or
        (Test-Path (Join-Path $BundleDirectory '_internal\Qt6Core.dll'))
    )

    Write-Host ''
    Write-Host '  launching...'
    $process = Start-Process -FilePath $exe -PassThru
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $title = ''
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $process.Refresh()
        if ($process.HasExited) { break }
        if ($process.MainWindowTitle) { $title = $process.MainWindowTitle; break }
    }
    $process.Refresh()

    Add-Check 'process survived start-up' (-not $process.HasExited) $(
        if ($process.HasExited) { "exited with $($process.ExitCode)" } else { 'running' })
    Add-Check 'main window appeared' ([bool] $title) $title

    if (-not $process.HasExited) {
        Add-Check 'window title carries the version' ($title -like "*$($version.Version)*") $title
        Add-Check 'application name appears exactly once' (
            ([regex]::Matches($title, 'OMRFlow')).Count -eq 1) $title
        Start-Sleep -Seconds 2
        $process.Refresh()
        Add-Check 'still responding' $process.Responding

        $null = $process.CloseMainWindow()
        $exitedCleanly = $process.WaitForExit(20000)
        if (-not $exitedCleanly) { $process.Kill() }
        Add-Check 'closed cleanly on request' $exitedCleanly $(
            if ($exitedCleanly) { "exit code $($process.ExitCode)" } else { 'had to be killed' })
        if ($exitedCleanly) {
            Add-Check 'exit code is zero' ($process.ExitCode -eq 0) "$($process.ExitCode)"
        }
    }

    Write-Host ''
    $failed = @($checks | Where-Object { -not $_.Passed })
    if ($failed.Count -gt 0) {
        Write-Host "$($failed.Count) of $($checks.Count) checks FAILED." -ForegroundColor Red
        exit 1
    }
    Write-Host "All $($checks.Count) checks passed." -ForegroundColor Green
    Write-Host ''
    Write-Host 'Note: this exercises the packaged build on the machine that built it.'
    Write-Host 'Clean-machine validation is a separate step - docs/release/CLEAN_MACHINE_TEST.md'
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
