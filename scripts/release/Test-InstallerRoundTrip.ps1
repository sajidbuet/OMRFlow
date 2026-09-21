<#
.SYNOPSIS
    Install, launch and uninstall OMRFlow, verifying each step.

.DESCRIPTION
    Exercises the installer the way a tester will: a silent per-user install
    into a scratch directory, a launch of what it installed, then a silent
    uninstall, checking after each step that the right things appeared and
    disappeared.

    The check that matters most is the last one: an uninstall must not take
    the operator's projects, configuration or logs with it. Examination data
    outliving the application is not a nicety - it is the difference between
    an upgrade and a loss.

    Installs into a temporary directory rather than Program Files, so
    running this cannot disturb a real OMRFlow installation on the same
    machine.

.PARAMETER Installer
    Installer to test. Defaults to the newest in `dist/installer`.

.PARAMETER KeepInstalled
    Skip the uninstall, leaving the installation in place for manual poking.
    The installation directory is reported so it can be removed by hand.

.EXAMPLE
    .\scripts\release\Test-InstallerRoundTrip.ps1
#>
[CmdletBinding()]
param(
    [string] $Installer,
    [switch] $KeepInstalled
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

$checks = [System.Collections.Generic.List[object]]::new()
function Add-Check([string] $Name, [bool] $Passed, [string] $Detail = '') {
    $checks.Add([pscustomobject]@{ Name = $Name; Passed = $Passed })
    $mark = if ($Passed) { 'PASS' } else { 'FAIL' }
    $colour = if ($Passed) { 'Green' } else { 'Red' }
    Write-Host ("  {0,-4} {1,-46} {2}" -f $mark, $Name, $Detail) -ForegroundColor $colour
}

$installDirectory = Join-Path $env:TEMP "omrflow-installer-test-$(Get-Random)"

try {
    $version = & (Join-Path $PSScriptRoot 'Get-OMRFlowVersion.ps1')

    if (-not $Installer) {
        $found = Get-ChildItem (Join-Path $repositoryRoot 'dist\installer') -Filter '*Setup*.exe' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $found) { throw 'No installer found in dist\installer. Run Build-Installer.ps1 first.' }
        $Installer = $found.FullName
    }

    Write-Host "Installer round trip: $(Split-Path $Installer -Leaf)" -ForegroundColor Cyan
    Write-Host "  install directory : $installDirectory"
    Write-Host ''

    $userDataRoot = Join-Path $env:LOCALAPPDATA 'OMRFlow'
    $userDataBefore = @()
    if (Test-Path $userDataRoot) {
        $userDataBefore = @(Get-ChildItem $userDataRoot -Recurse -File -ErrorAction SilentlyContinue |
            ForEach-Object { $_.FullName })
    }

    # Trimmed: Inno Setup pads the version strings it writes into the
    # installer's resources with trailing spaces, so a direct comparison
    # fails on a version that is in fact correct.
    $installerVersion = ((Get-Item $Installer).VersionInfo.ProductVersion ?? '').Trim()
    Add-Check 'installer reports the right version' `
        ($installerVersion -eq $version.Version) $installerVersion

    # --------------------------------------------------------------- install
    Write-Host '  installing (silent, per-user)...'
    $log = Join-Path $env:TEMP "omrflow-install-$(Get-Random).log"
    $process = Start-Process -FilePath $Installer -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER',
        "/DIR=$installDirectory", "/LOG=$log"
    )
    Add-Check 'installer exited successfully' ($process.ExitCode -eq 0) "exit $($process.ExitCode)"

    $installedExe = Join-Path $installDirectory 'OMRFlow.exe'
    Add-Check 'application was installed' (Test-Path $installedExe)
    Add-Check 'licence was installed' (Test-Path (Join-Path $installDirectory 'LICENSE.txt'))
    Add-Check 'runtime was installed' (Test-Path (Join-Path $installDirectory '_internal'))

    $uninstaller = Get-ChildItem $installDirectory -Filter 'unins*.exe' -ErrorAction SilentlyContinue |
        Select-Object -First 1
    Add-Check 'uninstaller was created' ([bool] $uninstaller)

    $registered = Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
        ForEach-Object { Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue } |
        Where-Object { $_.DisplayName -like 'OMRFlow*' }
    Add-Check 'listed in Apps & features' ([bool] $registered) $(
        if ($registered) { "$($registered.DisplayName) $($registered.DisplayVersion)" })

    if (Test-Path $installedExe) {
        $installedVersion = ((Get-Item $installedExe).VersionInfo.ProductVersion ?? '').Trim()
        Add-Check 'installed application has the right version' `
            ($installedVersion -eq $version.Version) $installedVersion

        # No Python on PATH is needed: this is the whole point of bundling.
        Write-Host '  launching the installed application...'
        $app = Start-Process -FilePath $installedExe -PassThru
        $deadline = (Get-Date).AddSeconds(90)
        $title = ''
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 500
            $app.Refresh()
            if ($app.HasExited) { break }
            if ($app.MainWindowTitle) { $title = $app.MainWindowTitle; break }
        }
        $app.Refresh()
        Add-Check 'installed application launches' (-not $app.HasExited -and [bool] $title) $title
        if (-not $app.HasExited) {
            $null = $app.CloseMainWindow()
            if (-not $app.WaitForExit(20000)) { $app.Kill() }
            Add-Check 'installed application closes cleanly' ($app.ExitCode -eq 0) "exit $($app.ExitCode)"
        }
    }

    # ------------------------------------------------------------- uninstall
    if ($KeepInstalled) {
        Write-Host ''
        Write-Host "Left installed at $installDirectory (remove it by hand)." -ForegroundColor Yellow
    }
    elseif ($uninstaller) {
        Write-Host '  uninstalling (silent)...'
        $removal = Start-Process -FilePath $uninstaller.FullName -Wait -PassThru -ArgumentList @(
            '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'
        )
        # Inno Setup's uninstaller relaunches itself from a temporary copy,
        # so the parent exits before the work is finished.
        Start-Sleep -Seconds 5
        Add-Check 'uninstaller exited successfully' ($removal.ExitCode -eq 0) "exit $($removal.ExitCode)"
        Add-Check 'application was removed' (-not (Test-Path $installedExe))
        Add-Check 'runtime was removed' (-not (Test-Path (Join-Path $installDirectory '_internal')))

        $stillRegistered = Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
            ForEach-Object { Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue } |
            Where-Object { $_.DisplayName -like 'OMRFlow*' }
        Add-Check 'removed from Apps & features' (-not $stillRegistered)
    }

    # -------------------------------------------- the check that matters most
    $userDataAfter = @()
    if (Test-Path $userDataRoot) {
        $userDataAfter = @(Get-ChildItem $userDataRoot -Recurse -File -ErrorAction SilentlyContinue |
            ForEach-Object { $_.FullName })
    }
    $lost = @($userDataBefore | Where-Object { $_ -notin $userDataAfter })
    Add-Check 'user configuration and logs were preserved' ($lost.Count -eq 0) $(
        if ($lost.Count) { "$($lost.Count) file(s) removed" } else { "$($userDataAfter.Count) file(s) intact" })

    Write-Host ''
    $failed = @($checks | Where-Object { -not $_.Passed })
    if ($failed.Count -gt 0) {
        Write-Host "$($failed.Count) of $($checks.Count) checks FAILED." -ForegroundColor Red
        Write-Host "Installer log: $log"
        exit 1
    }
    Write-Host "All $($checks.Count) checks passed." -ForegroundColor Green
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
finally {
    if (-not $KeepInstalled -and (Test-Path $installDirectory)) {
        Remove-Item -Recurse -Force $installDirectory -ErrorAction SilentlyContinue
    }
}
