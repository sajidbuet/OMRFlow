<#
.SYNOPSIS
    Stage the clean-machine test payload, and report whether Windows Sandbox
    is available to run it.

.DESCRIPTION
    Copies the installer, its checksum file and the in-sandbox test script
    into packaging/sandbox/payload, which OMRFlow-CleanMachine.wsb maps into
    the sandbox read-only.

    It stages the installer and nothing else. No source tree, no wheel, no
    virtual environment: an importable source tree is one of the two defects
    the clean-machine test exists to catch, and staging it would hide it.

    A second folder - packaging/sandbox/results - is mapped writable, and is
    where the in-sandbox script leaves its JSON report and transcript. A
    sandbox discards everything when it closes, so without that the evidence
    for a release would be a screenshot. It is deliberately a different
    folder from the payload, so that the payload can stay read-only.

.PARAMETER Installer
    Installer to test. Defaults to the newest in dist/installer.

.PARAMETER ResultsPath
    Where the sandbox writes its report. Defaults to
    packaging/sandbox/results, which is ignored by Git.

.PARAMETER Launch
    Start Windows Sandbox once the payload is staged.

.PARAMETER Wait
    After launching, wait for the in-sandbox script to finish and print its
    verdict on the host. The sandbox window stays open for the manual steps.

.PARAMETER TimeoutMinutes
    How long -Wait waits before giving up. The default allows for a slow
    first launch, an uninstall and a reinstall.

.EXAMPLE
    .\packaging\sandbox\New-SandboxPayload.ps1 -Launch -Wait
#>
[CmdletBinding()]
param(
    [string] $Installer,
    [string] $ResultsPath,
    [switch] $Launch,
    [switch] $Wait,
    [int] $TimeoutMinutes = 45
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$payload = Join-Path $PSScriptRoot 'payload'
if (-not $ResultsPath) { $ResultsPath = Join-Path $PSScriptRoot 'results' }

if (-not $Installer) {
    $found = Get-ChildItem (Join-Path $repositoryRoot 'dist\installer') -Filter '*Setup*.exe' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $found) { throw 'No installer in dist\installer. Run .\scripts\release\Build-Installer.ps1 first.' }
    $Installer = $found.FullName
}

if (Test-Path $payload) { Remove-Item -Recurse -Force $payload }
New-Item -ItemType Directory -Force $payload | Out-Null

Copy-Item $Installer $payload
# Both scripts: the first decides what a machine can decide, the second
# collects what only a person can. Staging only the first is how the manual
# steps end up recorded nowhere.
Copy-Item (Join-Path $PSScriptRoot 'Start-CleanMachineTest.ps1') $payload
Copy-Item (Join-Path $PSScriptRoot 'Complete-ManualChecks.ps1') $payload

$sums = Join-Path (Split-Path $Installer -Parent) 'SHA256SUMS.txt'
if (Test-Path $sums) {
    Copy-Item $sums $payload
}
else {
    Write-Warning "No SHA256SUMS.txt beside the installer. Run .\scripts\release\New-Checksums.ps1 - the test cannot verify the download otherwise."
}

# A results folder from a previous run would make it impossible to tell a
# fresh report from a stale one, so it starts empty every time.
if (Test-Path $ResultsPath) { Remove-Item -Recurse -Force $ResultsPath }
New-Item -ItemType Directory -Force $ResultsPath | Out-Null

# Windows Sandbox's support for a relative <HostFolder> depends on the
# Windows build, and a rejected configuration is a confusing way to start a
# release test. Generate a copy with the paths resolved, and launch that.
$template = Join-Path $PSScriptRoot 'OMRFlow-CleanMachine.wsb'
$generated = Join-Path $PSScriptRoot 'OMRFlow-CleanMachine.generated.wsb'
(Get-Content -Raw $template).
    Replace('<HostFolder>.\payload</HostFolder>', "<HostFolder>$payload</HostFolder>").
    Replace('<HostFolder>.\results</HostFolder>', "<HostFolder>$ResultsPath</HostFolder>") |
    Set-Content -LiteralPath $generated -Encoding UTF8

Write-Host "Payload staged in $payload" -ForegroundColor Green
Get-ChildItem $payload | ForEach-Object {
    Write-Host ("  {0,-42} {1,8:N1} MB" -f $_.Name, ($_.Length / 1MB))
}
Write-Host ''

# ------------------------------------------- is Windows Sandbox available?
#
# There are two Windows Sandbox launchers, and which one is present decides
# how this script has to drive it.
#
#   * `wsb.exe` - the CLI in the Store-delivered Sandbox app. It takes the
#     configuration as an XML *string*, not a path (a path is rejected with
#     "The configuration file was invalid"), and it does not run
#     <LogonCommand>. It does, however, offer `exec`, which is better: the
#     test is started deliberately and its exit status comes back.
#   * `System32\WindowsSandbox.exe` - the older launcher, which takes a .wsb
#     path and does honour <LogonCommand>.
#
# On a machine where the Sandbox app has updated itself, the old executable
# still exists but silently ignores both the configuration file and the logon
# command - the sandbox starts with no mapped folders and the test never
# runs. So `wsb` is preferred wherever it exists, rather than falling back to
# it.
$wsb = Get-Command 'wsb' -ErrorAction SilentlyContinue
$legacy = Join-Path $env:SystemRoot 'System32\WindowsSandbox.exe'

if ($wsb -or (Test-Path $legacy)) {
    Write-Host 'Windows Sandbox is available.' -ForegroundColor Green
    Write-Host ("  launcher : {0}" -f $(if ($wsb) { "$($wsb.Source) (CLI)" } else { "$legacy (legacy)" }))
    Write-Host "  results  : $ResultsPath"
    $configuration = $generated
    if (-not $Launch) {
        if ($wsb) {
            Write-Host '  Run it with:  re-run this script with -Launch'
            Write-Host '  (the CLI needs the configuration inline, which this script assembles)'
        }
        else {
            Write-Host "  Run it with:  & '$legacy' '$configuration'"
            Write-Host '            or:  re-run this script with -Launch'
        }
        exit 0
    }

    # Windows allows exactly one sandbox at a time, and a second launch fails
    # with a dialog inside a window nobody is watching - the configured test
    # then never starts and the wait below times out with no explanation. Say
    # so here instead.
    $alreadyRunning = @()
    if ($wsb) {
        $alreadyRunning = @(& wsb list 2>$null | Where-Object { $_ -match '^[0-9a-f-]{36}$' })
    }
    else {
        $alreadyRunning = @(Get-Process -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like 'WindowsSandbox*' -and $_.Name -ne 'WindowsSandboxServer' } |
            ForEach-Object { "$($_.Name) (pid $($_.Id))" })
    }
    if ($alreadyRunning.Count) {
        Write-Error @"
Windows Sandbox is already running, and only one instance is allowed. The
configured clean-machine test cannot start while it is open.

Close the existing sandbox and run this script again. Anything inside it is
discarded on close, which is the point of a sandbox.

    running: $($alreadyRunning -join ', ')
"@
        exit 4
    }

    Write-Host 'Starting the sandbox...'
    if ($wsb) {
        # The CLI wants one XML element, so the declaration and the comments
        # that document the template have to come off first.
        $xml = Get-Content -Raw $configuration
        $xml = [regex]::Replace($xml, '<\?xml.*?\?>', '')
        $xml = [regex]::Replace($xml, '(?s)<!--.*?-->', '')
        $xml = (($xml -split "`n" | ForEach-Object { $_.Trim() }) -join '')

        $started = & wsb start -c $xml 2>&1 | Out-String
        $id = ([regex]::Match($started, 'Id:\s*([0-9a-f-]{36})')).Groups[1].Value
        if (-not $id) { Write-Error "Windows Sandbox did not start.`n$started"; exit 5 }
        Write-Host "  sandbox  : $id"

        # `exec -r ExistingLogin` needs an interactive session, and starting
        # one is what `connect` does. The window it opens is also what the
        # tester works in for the manual steps.
        Start-Process $wsb.Source -ArgumentList @('connect', '--id', $id)
        Write-Host '  waiting for the sandbox desktop...'
        Start-Sleep -Seconds 30

        # -Unattended: nothing is watching this console, so the script must
        # not stop on a keypress it will never get.
        $command = 'cmd /c "powershell -NoProfile -ExecutionPolicy Bypass -File ' +
        'C:\Users\WDAGUtilityAccount\Desktop\OMRFlow\Start-CleanMachineTest.ps1 -Unattended ' +
        '> C:\Users\WDAGUtilityAccount\Desktop\OMRFlow-results\run-out.txt 2>&1"'
        Write-Host '  running the test inside the sandbox...'
        Start-Process $wsb.Source -ArgumentList @(
            'exec', '--id', $id, '-r', 'ExistingLogin', '-c', $command)
    }
    else {
        Start-Process $legacy -ArgumentList $configuration
    }

    if (-not $Wait) { exit 0 }

    # The in-sandbox script writes COMPLETE last, after the JSON report, so
    # its appearance means the report beside it is finished rather than
    # half-written.
    $marker = Join-Path $ResultsPath 'COMPLETE'
    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    Write-Host "Waiting up to $TimeoutMinutes minutes for the in-sandbox test to finish..."
    while (-not (Test-Path $marker) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 10
    }
    if (-not (Test-Path $marker)) {
        Write-Warning "No result after $TimeoutMinutes minutes. The sandbox is still open - read the window."
        exit 3
    }

    $verdict = (Get-Content -Raw $marker).Trim()
    $colour = if ($verdict -eq 'PASS') { 'Green' } else { 'Red' }
    Write-Host ''
    Write-Host "Automated portion: $verdict" -ForegroundColor $colour
    Write-Host "  report : $(Join-Path $ResultsPath 'clean-machine-results.json')"
    Write-Host '  The sandbox is still open for the steps that need a person.'
    exit $(if ($verdict -eq 'PASS') { 0 } else { 1 })
}

Write-Host 'Windows Sandbox is NOT installed on this machine.' -ForegroundColor Yellow
Write-Host ''
Write-Host 'The clean-machine test cannot run here. To enable Sandbox you need'
Write-Host 'administrator rights and a reboot:'
Write-Host ''
Write-Host '    # in an elevated PowerShell'
Write-Host '    Enable-WindowsOptionalFeature -FeatureName "Containers-DisposableClientVM" -Online -All -NoRestart'
Write-Host '    # then restart Windows'
Write-Host ''
Write-Host 'Requirements: Windows 10/11 Pro, Enterprise or Education (not Home),'
Write-Host 'x64, with virtualisation enabled in the firmware.'
Write-Host ''
Write-Host 'Alternatively, copy the payload folder to a separate clean machine or'
Write-Host 'a fresh virtual machine and run Start-CleanMachineTest.ps1 there.'
Write-Host ''
Write-Host 'Until then the strongest available substitute is:'
Write-Host '    .\scripts\release\Test-SelfContained.ps1'
Write-Host 'which proves less - see the note it prints on completion.'
exit 2
