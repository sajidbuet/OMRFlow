<#
.SYNOPSIS
    Record the clean-machine steps that need a person, into the same report
    the automated harness wrote.

.DESCRIPTION
    `Start-CleanMachineTest.ps1` decides everything mechanical and then lists
    what it could not judge. This walks a tester through exactly that list,
    one prompt at a time, and merges the answers back into
    `clean-machine-results.json`, so the release has a single record rather
    than a JSON file plus somebody's notes.

    Run it in the sandbox after working through the interface, with OMRFlow
    installed and the automated portion finished. Answer only for what was
    actually observed: `s` (skip) is a legitimate answer and is recorded as
    *not performed*, which is the honest outcome and is far more useful than
    a guessed pass. A failure prompts for a description, because "it failed"
    without the symptom cannot be acted on.

    Nothing here re-runs the automated checks or overwrites them: the merged
    file keeps them exactly as the harness left them, and adds the manual
    answers alongside with `Source` set to `manual`.

.PARAMETER ResultsPath
    Where the automated harness left its report. Defaults to the writable
    folder the sandbox maps in.

.PARAMETER Tester
    Recorded in the report as the person who made these observations.

.EXAMPLE
    .\Complete-ManualChecks.ps1 -Tester 'S. Choudhury'
#>
[CmdletBinding()]
param(
    [string] $ResultsPath,
    [string] $Tester = $env:USERNAME
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $ResultsPath) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $mapped = Join-Path $desktop 'OMRFlow-results'
    $ResultsPath = if (Test-Path $mapped) { $mapped } else { $desktop }
}
$jsonPath = Join-Path $ResultsPath 'clean-machine-results.json'
if (-not (Test-Path $jsonPath)) {
    throw "No automated result at $jsonPath. Run Start-CleanMachineTest.ps1 first - the manual answers attach to its report rather than standing alone."
}

# Step numbers are those of docs/release/CLEAN_MACHINE_TEST.md. Phrased as
# questions with one observable answer each, because "did the workflow work?"
# is not a question a tester can answer honestly in one keystroke.
$questions = @(
    @{ Step = '5'; Phase = 'Installation'; Check = 'SmartScreen warning appeared, and its wording matches Installation.md'
        Hint = 'Only observable with networking enabled. Skip if the sandbox has no network.' }
    @{ Step = '6'; Phase = 'Installation'; Check = 'the licence page shows the MIT licence'
        Hint = 'Run the installer interactively, not silently.' }
    @{ Step = '7'; Phase = 'Installation'; Check = 'the Alpha warning is shown during installation'
        Hint = 'The welcome page should state that this is an ALPHA release and that results need independent verification.' }
    @{ Step = '13'; Phase = 'First launch'; Check = 'all nine workflow stage icons render in the navigator'
        Hint = 'A blank or missing icon means the bundled assets did not come along.' }
    @{ Step = '14'; Phase = 'First launch'; Check = 'Help > About shows the version, Alpha, the MIT licence and the repository link'
        Hint = 'The version must read exactly the version under test.' }
    @{ Step = '14'; Phase = 'First launch'; Check = 'hovering the About version line shows the build identifier with its commit'
        Hint = 'For example 0.1.0-alpha.1+a1b2c3d.' }
    @{ Step = '15'; Phase = 'First launch'; Check = 'every one of the nine stages renders without error' }
    @{ Step = '16'; Phase = 'First launch'; Check = 'the footer shows the version, the licence and Ready' }
    @{ Step = '17'; Phase = 'Workflow'; Check = 'Create Project succeeds and the project opens' }
    @{ Step = '18'; Phase = 'Workflow'; Check = 'Project Configuration opens; an examination name and one set save correctly' }
    @{ Step = '19'; Phase = 'Workflow'; Check = 'the bundled example template opens on the Template stage and validates' }
    @{ Step = '20'; Phase = 'Workflow'; Check = 'Tools > Generate Synthetic Test Dataset produces sheets'
        Hint = 'This exercises the imaging and rendering stack - where a missing native dependency surfaces.' }
    @{ Step = '21'; Phase = 'Workflow'; Check = 'Scan stage: load template, add folder, Process All completes recognition'
        Hint = 'This is the multiprocessing path. A frozen build that re-launches the GUI instead of starting workers fails here.' }
    @{ Step = '22'; Phase = 'Workflow'; Check = 'recognised values appear in the table and a preview image renders' }
    @{ Step = '23'; Phase = 'Workflow'; Check = 'Tools > Create Diagnostic Bundle produces a zip file'
        Hint = 'Exercises writing outside the installation directory.' }
    @{ Step = '24'; Phase = 'Workflow'; Check = 'Tools > Project Health / Recovery opens and reports' }
    @{ Step = '24a'; Phase = 'Workflow'; Check = 'attendance / scoring / a result workbook can be generated from the synthetic data'
        Hint = 'Exercises openpyxl and Pillow in the frozen build. Skip if the synthetic dataset does not support it.' }
    @{ Step = '27'; Phase = 'Persistence'; Check = 'after closing and relaunching, the project appears under Recent Projects and reopens with its results' }
    @{ Step = '32'; Phase = 'Uninstall'; Check = 'the project folder and its database still exist after the uninstall'
        Hint = 'The project folder, wherever it was created - not the settings directory, which the harness already checked.' }
    @{ Step = '35'; Phase = 'Reinstall'; Check = 'after reinstalling, the previously created project opens with its results' }
)

Write-Host ''
Write-Host '=============================================================' -ForegroundColor Cyan
Write-Host ' OMRFlow - clean-machine test, the steps that need a person' -ForegroundColor Cyan
Write-Host '=============================================================' -ForegroundColor Cyan
Write-Host ''
Write-Host 'Answer for what you actually saw.' -ForegroundColor Yellow
Write-Host '  y = observed and correct    n = observed and wrong    s = not performed'
Write-Host ''

$answers = [System.Collections.Generic.List[object]]::new()
foreach ($question in $questions) {
    Write-Host ''
    Write-Host ("step {0} - {1}" -f $question.Step, $question.Check) -ForegroundColor White
    if ($question.ContainsKey('Hint')) { Write-Host ("         {0}" -f $question.Hint) -ForegroundColor DarkGray }

    $reply = ''
    while ($reply -notin @('y', 'n', 's')) {
        $reply = (Read-Host '  [y/n/s]').Trim().ToLower()
    }

    $detail = ''
    if ($reply -eq 'n') {
        while (-not $detail) { $detail = (Read-Host '  what happened?').Trim() }
    }
    elseif ($reply -eq 's') {
        while (-not $detail) { $detail = (Read-Host '  why was it not performed?').Trim() }
    }

    $answers.Add([pscustomobject]@{
            Phase  = "Manual - $($question.Phase)"
            Check  = $question.Check
            Result = switch ($reply) { 'y' { 'PASS' } 'n' { 'FAIL' } default { 'SKIPPED' } }
            Detail = $detail
            Step   = $question.Step
            Source = 'manual'
        })
}

# ------------------------------------------------------------------- merge
$report = Get-Content -Raw $jsonPath | ConvertFrom-Json

# The automated checks are kept verbatim; the manual ones are appended and
# marked, so a reader can always tell which machine decided what.
$existing = @($report.checks | ForEach-Object {
        $row = $_ | Select-Object *
        if (-not ($row.PSObject.Properties.Name -contains 'Source')) {
            $row | Add-Member -NotePropertyName Source -NotePropertyValue 'automated' -PassThru
        }
        else { $row }
    })
$merged = @($existing) + @($answers)

$passed = @($merged | Where-Object { $_.Result -eq 'PASS' })
$failed = @($merged | Where-Object { $_.Result -eq 'FAIL' })
$skipped = @($merged | Where-Object { $_.Result -eq 'SKIPPED' })

$report.checks = $merged
$report.summary.passed = $passed.Count
$report.summary.failed = $failed.Count
$report.summary.skipped = $skipped.Count
$report.summary.automated_verdict = $(if (@($existing | Where-Object { $_.Result -eq 'FAIL' }).Count) { 'FAIL' } else { 'PASS' })
$report | Add-Member -NotePropertyName manual_verdict -NotePropertyValue $(
    if (@($answers | Where-Object { $_.Result -eq 'FAIL' }).Count) { 'FAIL' }
    elseif (@($answers | Where-Object { $_.Result -eq 'SKIPPED' }).Count) { 'INCOMPLETE' }
    else { 'PASS' }) -Force
$report | Add-Member -NotePropertyName tester -NotePropertyValue $Tester -Force
$report | Add-Member -NotePropertyName manual_completed -NotePropertyValue (
    (Get-Date).ToUniversalTime().ToString('o')) -Force

# The manual answers replace the harness's "nobody has looked at these yet"
# list, which would otherwise contradict them.
$answered = @($answers | Where-Object { $_.Result -ne 'SKIPPED' } | ForEach-Object { $_.Step })
$report.not_performed = @($report.not_performed | Where-Object { $_.step -notin $answered })

$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $jsonPath -Encoding UTF8

Write-Host ''
Write-Host '=============================================================' -ForegroundColor Cyan
Write-Host (" Manual portion: {0} passed, {1} failed, {2} not performed" -f
    @($answers | Where-Object { $_.Result -eq 'PASS' }).Count,
    @($answers | Where-Object { $_.Result -eq 'FAIL' }).Count,
    @($answers | Where-Object { $_.Result -eq 'SKIPPED' }).Count) -ForegroundColor $(
    if (@($answers | Where-Object { $_.Result -eq 'FAIL' }).Count) { 'Red' } else { 'Green' })
Write-Host '=============================================================' -ForegroundColor Cyan
Write-Host ''
Write-Host "Merged into $jsonPath"
Write-Host 'On the host, turn it into the release record with:'
Write-Host '    .\scripts\release\New-ValidationReport.ps1'
Write-Host ''
