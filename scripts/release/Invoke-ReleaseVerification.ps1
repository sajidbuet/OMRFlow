<#
.SYNOPSIS
    Verify a built release before it is published.

.DESCRIPTION
    The last gate before a release becomes visible. Checks that the artifacts
    exist, are named for the version they contain, agree with each other, and
    match their published checksums - and that nothing about the build claims
    a maturity the version does not support.

    Runs without an interactive desktop, so it works on a CI runner. It
    therefore does *not* launch the application: that is
    Test-PackagedApp.ps1, and installing and uninstalling is
    Test-InstallerRoundTrip.ps1. Both are listed as remaining steps at the
    end of the output rather than silently skipped.

.PARAMETER InstallerDirectory
    Where the artifacts are. Defaults to `dist/installer`.

.EXAMPLE
    .\scripts\release\Invoke-ReleaseVerification.ps1
#>
[CmdletBinding()]
param(
    [string] $InstallerDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $InstallerDirectory) { $InstallerDirectory = Join-Path $repositoryRoot 'dist\installer' }

$checks = [System.Collections.Generic.List[object]]::new()
function Add-Check([string] $Name, [bool] $Passed, [string] $Detail = '') {
    $checks.Add([pscustomobject]@{ Name = $Name; Passed = $Passed })
    $mark = if ($Passed) { 'PASS' } else { 'FAIL' }
    $colour = if ($Passed) { 'Green' } else { 'Red' }
    Write-Host ("  {0,-4} {1,-48} {2}" -f $mark, $Name, $Detail) -ForegroundColor $colour
}

try {
    $version = & (Join-Path $PSScriptRoot 'Get-OMRFlowVersion.ps1')
    Write-Host "Verifying the release of OMRFlow $($version.Version)" -ForegroundColor Cyan
    Write-Host "  channel : $($version.Channel)   prerelease: $($version.IsPrerelease)"
    Write-Host ''

    # ----------------------------------------------------------- the bundle
    $bundleExe = Join-Path $repositoryRoot 'dist\OMRFlow\OMRFlow.exe'
    Add-Check 'application bundle exists' (Test-Path $bundleExe)
    if (Test-Path $bundleExe) {
        $bundleVersion = ((Get-Item $bundleExe).VersionInfo.ProductVersion ?? '').Trim()
        Add-Check 'bundle version matches the source' `
            ($bundleVersion -eq $version.Version) $bundleVersion
    }

    # -------------------------------------------------------- the installer
    $expectedName = "OMRFlow-$($version.Version)-Setup-x64.exe"
    $installer = Join-Path $InstallerDirectory $expectedName
    Add-Check 'installer exists under its versioned name' (Test-Path $installer) $expectedName
    if (-not (Test-Path $installer)) {
        $present = @(Get-ChildItem $InstallerDirectory -Filter '*.exe' -ErrorAction SilentlyContinue |
            ForEach-Object { $_.Name })
        if ($present) { Write-Host "       found instead: $($present -join ', ')" -ForegroundColor Yellow }
    }
    else {
        $installerVersion = ((Get-Item $installer).VersionInfo.ProductVersion ?? '').Trim()
        Add-Check 'installer version matches the source' `
            ($installerVersion -eq $version.Version) $installerVersion

        $sizeMb = [math]::Round((Get-Item $installer).Length / 1MB, 1)
        # A plausibility floor, not a target: an installer that lost the Qt
        # runtime would still compile and would be a fraction of this.
        Add-Check 'installer is a plausible size' ($sizeMb -gt 40) "$sizeMb MB"

        # Unsigned is expected for a prerelease and is documented. Reported
        # so it is a known fact about the artifact rather than a surprise.
        $signature = Get-AuthenticodeSignature $installer
        $signed = $signature.Status -eq 'Valid'
        Write-Host ("  {0} code signature                                   {1}" -f
            $(if ($signed) { 'PASS' } else { 'NOTE' }),
            $(if ($signed) { $signature.SignerCertificate.Subject } else { 'unsigned - SmartScreen will warn (documented)' })
        ) -ForegroundColor $(if ($signed) { 'Green' } else { 'Yellow' })
    }

    # -------------------------------------------------------- the checksums
    $sums = Join-Path $InstallerDirectory 'SHA256SUMS.txt'
    Add-Check 'SHA256SUMS.txt exists' (Test-Path $sums)
    if ((Test-Path $sums) -and (Test-Path $installer)) {
        $lines = @(Get-Content $sums | Where-Object { $_.Trim() })
        Add-Check 'checksum file is not empty' ($lines.Count -gt 0) "$($lines.Count) entry/entries"

        $recorded = $lines |
            Where-Object { $_ -match '^([0-9a-fA-F]{64})\s+(.+)$' } |
            ForEach-Object {
                $null = $_ -match '^([0-9a-fA-F]{64})\s+(.+)$'
                [pscustomobject]@{ Hash = $Matches[1].ToLower(); Name = $Matches[2].Trim() }
            }
        Add-Check 'every line is a valid checksum entry' ($recorded.Count -eq $lines.Count)

        $entry = $recorded | Where-Object { $_.Name -eq $expectedName } | Select-Object -First 1
        Add-Check 'the installer is listed' ([bool] $entry)
        if ($entry) {
            $actual = (Get-FileHash -Algorithm SHA256 -Path $installer).Hash.ToLower()
            Add-Check 'the recorded checksum is correct' ($entry.Hash -eq $actual) $actual
        }
    }

    # ------------------------------------------------- honesty of the claim
    Add-Check 'the version is a recognised prerelease or release' `
        ($version.Channel -in @('Alpha', 'Beta', 'Release Candidate', 'Stable')) $version.Channel
    if ($version.Channel -ne 'Stable') {
        Add-Check 'a prerelease is flagged as one' ($version.IsPrerelease -eq $true) `
            'must be published as a GitHub pre-release'
    }
    Add-Check 'the build records a source commit' `
        ($version.Build -ne $version.Version) $version.Build

    # ------------------------------------------------------------ the notes
    $changelog = Join-Path $repositoryRoot 'CHANGELOG.md'
    Add-Check 'the changelog mentions this version' `
        ((Test-Path $changelog) -and (Select-String -Path $changelog -SimpleMatch $version.Version -Quiet))

    Write-Host ''
    $failed = @($checks | Where-Object { -not $_.Passed })
    if ($failed.Count -gt 0) {
        Write-Host "$($failed.Count) of $($checks.Count) checks FAILED. Do not publish." -ForegroundColor Red
        exit 1
    }

    Write-Host "All $($checks.Count) automated checks passed." -ForegroundColor Green
    Write-Host ''
    Write-Host 'Still to do by hand - these need a desktop and cannot run here:' -ForegroundColor Yellow
    Write-Host '  .\scripts\release\Test-PackagedApp.ps1          launch the built bundle'
    Write-Host '  .\scripts\release\Test-InstallerRoundTrip.ps1   install, launch, uninstall'
    Write-Host '  .\scripts\release\Test-SelfContained.ps1        launch with no Python on the PATH'
    Write-Host '  .\packaging\sandbox\New-SandboxPayload.ps1      the clean-machine test, in Windows Sandbox'
    Write-Host '  docs\release\CLEAN_MACHINE_TEST.md              what that test requires, and its manual steps'
    Write-Host ''
    Write-Host 'Then work through docs\release\RELEASE_CHECKLIST.md.'
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
