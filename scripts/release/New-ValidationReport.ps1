<#
.SYNOPSIS
    Turn a clean-machine test result into the release's validation record.

.DESCRIPTION
    `packaging/sandbox/Start-CleanMachineTest.ps1` writes
    `clean-machine-results.json` on the machine under test. This reads that
    file, attaches the identity of the release candidate it was testing -
    version, channel, source commit, installer name and SHA-256 - and writes
    the pair of reports the release process keeps:

        docs/release/validation/<version>-clean-machine.json
        docs/release/validation/<version>-clean-machine.md

    The JSON is the record; the Markdown is the same content for a person,
    and neither is edited by hand. Regenerating after a re-run overwrites
    both, which is the point: there is one current answer for a given
    candidate, not a pile of drafts.

    The installer's SHA-256 is taken from the machine that was tested, not
    from the build machine, and is then compared with `SHA256SUMS.txt`. A
    mismatch means the artifact that was tested is not the artifact that
    would be published, which is a release-blocking condition and is
    reported as one.

.PARAMETER ResultsJson
    The result file. Defaults to the one the sandbox harness leaves in
    packaging/sandbox/results.

.PARAMETER OutputDirectory
    Where the reports go. Defaults to docs/release/validation.

.PARAMETER Notes
    Free text recorded verbatim in both reports - what a rebuild was for,
    say, or which manual steps a person has since signed off.

.EXAMPLE
    .\scripts\release\New-ValidationReport.ps1
#>
[CmdletBinding()]
param(
    [string] $ResultsJson,
    [string] $OutputDirectory,
    [string] $Notes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $ResultsJson) {
    $ResultsJson = Join-Path $repositoryRoot 'packaging\sandbox\results\clean-machine-results.json'
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $repositoryRoot 'docs\release\validation'
}

function Format-Row([string[]] $Cells) { '| ' + ($Cells -join ' | ') + ' |' }

try {
    if (-not (Test-Path $ResultsJson)) {
        throw "No clean-machine result at $ResultsJson. Run .\packaging\sandbox\New-SandboxPayload.ps1 -Launch -Wait first."
    }
    $result = Get-Content -Raw $ResultsJson | ConvertFrom-Json

    $version = & (Join-Path $PSScriptRoot 'Get-OMRFlowVersion.ps1')
    $commit = (& git -C $repositoryRoot rev-parse HEAD 2>$null)
    $commit = if ($LASTEXITCODE -eq 0 -and $commit) { $commit.Trim() } else { 'unknown' }
    $treeClean = -not (& git -C $repositoryRoot status --porcelain 2>$null)

    # What the build machine says the artifact is, so the report can state
    # whether the thing that was tested is the thing that would be shipped.
    $installerDirectory = Join-Path $repositoryRoot 'dist\installer'
    $sumsPath = Join-Path $installerDirectory 'SHA256SUMS.txt'
    $publishedHash = ''
    if (Test-Path $sumsPath) {
        $line = Select-String -Path $sumsPath -Pattern ([regex]::Escape($result.installer.name)) |
            Select-Object -First 1
        if ($line) { $publishedHash = ($line.Line -split '\s+')[0].ToLower() }
    }
    $testedHash = ($result.installer.sha256 ?? '').ToLower()
    $artifactMatches = [bool] $publishedHash -and $publishedHash -eq $testedHash

    $checks = @($result.checks)
    $passed = @($checks | Where-Object { $_.Result -eq 'PASS' })
    $failed = @($checks | Where-Object { $_.Result -eq 'FAIL' })
    $skipped = @($checks | Where-Object { $_.Result -eq 'SKIPPED' })

    $verdict = if ($failed.Count) { 'FAIL' }
    elseif (-not $artifactMatches) { 'INVALID - the artifact tested is not the artifact built' }
    else { 'PASS (automated portion)' }

    New-Item -ItemType Directory -Force $OutputDirectory | Out-Null
    $stem = Join-Path $OutputDirectory "$($version.Version)-clean-machine"

    # ------------------------------------------------------------ the record
    $record = [ordered]@{
        schema             = 'omrflow.release-validation/1'
        generated          = (Get-Date).ToUniversalTime().ToString('o')
        release_candidate  = [ordered]@{
            version              = $version.Version
            release              = $version.Release
            channel              = $version.Channel
            is_prerelease        = $version.IsPrerelease
            tag_to_be_created    = $version.Tag
            source_commit        = $commit
            working_tree_clean   = $treeClean
            installer            = $result.installer.name
            installer_sha256     = $testedHash
            published_sha256     = $publishedHash
            artifact_under_test_matches_build = $artifactMatches
        }
        test_environment   = $result.environment
        summary            = [ordered]@{
            passed            = $passed.Count
            failed            = $failed.Count
            skipped           = $skipped.Count
            automated_verdict = $verdict
        }
        checks             = $checks
        not_performed      = $result.not_performed
        notes              = $Notes
    }
    $record | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath "$stem.json" -Encoding UTF8

    # ---------------------------------------------------------- the Markdown
    $lines = [System.Collections.Generic.List[string]]::new()
    $add = { param($text) $lines.Add($text) }

    & $add "# Clean-machine validation - OMRFlow $($version.Version)"
    & $add ''
    & $add '<!-- Generated by scripts/release/New-ValidationReport.ps1. Do not edit by hand. -->'
    & $add ''
    & $add "Generated $((Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ss')) UTC."
    & $add ''
    & $add '## Release candidate'
    & $add ''
    & $add (Format-Row @('', ''))
    & $add (Format-Row @('---', '---'))
    & $add (Format-Row @('Version', "``$($version.Version)``"))
    & $add (Format-Row @('Channel', $version.Channel))
    & $add (Format-Row @('Published as a pre-release', $version.IsPrerelease))
    & $add (Format-Row @('Tag to be created', "``$($version.Tag)`` - **not yet created**"))
    & $add (Format-Row @('Source commit', "``$commit``"))
    & $add (Format-Row @('Working tree clean at build', $treeClean))
    & $add (Format-Row @('Installer', "``$($result.installer.name)``"))
    & $add (Format-Row @('Installer SHA-256', "``$testedHash``"))
    & $add (Format-Row @('Matches `SHA256SUMS.txt`', $(if ($artifactMatches) { 'yes' } else { '**NO**' })))
    & $add ''
    & $add '## Test environment'
    & $add ''
    & $add (Format-Row @('', ''))
    & $add (Format-Row @('---', '---'))
    foreach ($property in $result.environment.PSObject.Properties) {
        & $add (Format-Row @($property.Name.Replace('_', ' '), "$($property.Value)"))
    }
    & $add ''
    & $add '## Result'
    & $add ''
    & $add "**$verdict** - $($passed.Count) passed, $($failed.Count) failed, $($skipped.Count) skipped."
    & $add ''
    & $add 'This covers the mechanical portion only. The steps listed under'
    & $add '*Not performed* below need a person to look at the screen and are'
    & $add 'not claimed here.'
    & $add ''
    & $add '## Checks'
    & $add ''
    & $add (Format-Row @('Result', 'Phase', 'Check', 'Detail'))
    & $add (Format-Row @('---', '---', '---', '---'))
    foreach ($check in $checks) {
        $mark = switch ($check.Result) {
            'PASS' { 'PASS' }
            'FAIL' { '**FAIL**' }
            default { '_SKIPPED_' }
        }
        $detail = ("$($check.Detail)").Replace('|', '\|')
        & $add (Format-Row @($mark, $check.Phase, $check.Check, $detail))
    }
    & $add ''
    & $add '## Not performed'
    & $add ''
    & $add 'These require human judgement and were **not** executed by the harness.'
    & $add 'A step that was not run is not a pass.'
    & $add ''
    & $add (Format-Row @('Step', 'What'))
    & $add (Format-Row @('---', '---'))
    foreach ($item in $result.not_performed) {
        & $add (Format-Row @("``$($item.step)``", $item.what))
    }
    if ($Notes) {
        & $add ''
        & $add '## Notes'
        & $add ''
        & $add $Notes
    }
    & $add ''
    & $add '## Related'
    & $add ''
    & $add '- [Clean-machine test procedure](../CLEAN_MACHINE_TEST.md)'
    & $add '- [Release checklist](../RELEASE_CHECKLIST.md)'
    & $add ''

    Set-Content -LiteralPath "$stem.md" -Value ($lines -join "`n") -Encoding UTF8

    Write-Host "Wrote $stem.json" -ForegroundColor Green
    Write-Host "Wrote $stem.md" -ForegroundColor Green
    Write-Host ''
    Write-Host "Automated verdict: $verdict" -ForegroundColor $(if ($failed.Count -or -not $artifactMatches) { 'Red' } else { 'Green' })
    if (-not $artifactMatches) {
        Write-Warning "The SHA-256 of the installer that was tested does not match SHA256SUMS.txt in dist\installer. The artifact under test is not the artifact that would be published."
    }
    exit $(if ($failed.Count -or -not $artifactMatches) { 1 } else { 0 })
}
catch {
    Write-Error $_
    exit 1
}
