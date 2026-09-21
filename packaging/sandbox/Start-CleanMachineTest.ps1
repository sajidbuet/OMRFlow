<#
.SYNOPSIS
    Runs inside Windows Sandbox. Prepares the clean-machine test and performs
    the parts of it that can be checked without a person watching.

.DESCRIPTION
    Launched automatically by OMRFlow-CleanMachine.wsb. It does not replace
    docs/release/CLEAN_MACHINE_TEST.md - that procedure requires a person to
    look at the interface, and the steps involving judgement stay theirs.
    What it does is remove the tedium and the opportunity for error from the
    mechanical parts:

      * confirms the sandbox really is clean (no Python, no Qt, no VC++
        runtime beyond what Windows ships, no source checkout);
      * verifies the installer's SHA-256 against SHA256SUMS.txt;
      * installs it as the unprivileged sandbox user;
      * confirms the application launches, shows a window, and is responsive;
      * writes a transcript to the Desktop, and prints the remaining manual
        steps so the tester can work straight down them.

    Nothing here writes to the mapped folder: it is mounted read-only, so the
    transcript and the results go to the sandbox Desktop, from where they can
    be copied out through the clipboard.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$payload = $PSScriptRoot
$desktop = 'C:\Users\WDAGUtilityAccount\Desktop'
$transcript = Join-Path $desktop 'OMRFlow-clean-machine-test.log'

Start-Transcript -Path $transcript -Force | Out-Null

$results = [System.Collections.Generic.List[object]]::new()
function Add-Result([string] $Name, [bool] $Passed, [string] $Detail = '') {
    $mark = if ($Passed) { 'PASS' } else { 'FAIL' }
    $results.Add([pscustomobject]@{ Check = $Name; Result = $mark; Detail = $Detail })
    $colour = if ($Passed) { 'Green' } else { 'Red' }
    Write-Host ("  {0,-4} {1,-54} {2}" -f $mark, $Name, $Detail) -ForegroundColor $colour
}

try {
    Write-Host ''
    Write-Host '=============================================================' -ForegroundColor Cyan
    Write-Host ' OMRFlow - clean-machine installation test' -ForegroundColor Cyan
    Write-Host '=============================================================' -ForegroundColor Cyan
    Write-Host ''
    Write-Host "  Windows  : $((Get-CimInstance Win32_OperatingSystem).Caption) $([System.Environment]::OSVersion.Version)"
    Write-Host "  User     : $env:USERNAME"
    Write-Host "  Admin    : $([bool]([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"
    Write-Host "  Date     : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    Write-Host ''

    # ------------------------------------------- is the machine really clean?
    Write-Host 'Confirming the machine is clean:' -ForegroundColor Cyan

    $pythonOnPath = Get-Command python, python3, py -ErrorAction SilentlyContinue
    Add-Result 'no Python on the PATH' (-not $pythonOnPath) $(
        if ($pythonOnPath) { ($pythonOnPath.Source -join '; ') } else { '' })

    $pythonInstalled = @(
        Get-ChildItem 'C:\Python*', "$env:LOCALAPPDATA\Programs\Python" -ErrorAction SilentlyContinue
    )
    Add-Result 'no Python installation' ($pythonInstalled.Count -eq 0)

    $vcRuntime = @(Get-ChildItem "$env:SystemRoot\System32" -Filter 'vcruntime140*.dll' -ErrorAction SilentlyContinue)
    Add-Result 'no VC++ redistributable in System32' ($vcRuntime.Count -eq 0) $(
        if ($vcRuntime.Count) { 'present - the sandbox is NOT clean' } else { 'as expected' })

    $qtOnMachine = @(Get-ChildItem 'C:\Qt' -ErrorAction SilentlyContinue)
    Add-Result 'no Qt installation' ($qtOnMachine.Count -eq 0)

    Add-Result 'no OMRFlow source checkout' (-not (Test-Path 'C:\Research'))

    # ------------------------------------------------------- the artifact
    Write-Host ''
    Write-Host 'The artifact:' -ForegroundColor Cyan

    $installer = Get-ChildItem $payload -Filter '*Setup*.exe' | Select-Object -First 1
    if (-not $installer) { throw "No installer in the payload folder ($payload). Run New-SandboxPayload.ps1 on the host." }
    Write-Host "  installer : $($installer.Name)  ($([math]::Round($installer.Length / 1MB, 1)) MB)"

    $sumsFile = Join-Path $payload 'SHA256SUMS.txt'
    if (Test-Path $sumsFile) {
        $actual = (Get-FileHash $installer.FullName -Algorithm SHA256).Hash.ToLower()
        $expectedLine = Select-String -Path $sumsFile -Pattern ([regex]::Escape($installer.Name)) |
            Select-Object -First 1
        $expected = if ($expectedLine) { ($expectedLine.Line -split '\s+')[0].ToLower() } else { '' }
        Add-Result 'SHA-256 matches SHA256SUMS.txt' ($expected -and $actual -eq $expected) $actual
    }
    else {
        Add-Result 'SHA256SUMS.txt present' $false 'not staged - cannot verify the checksum'
    }

    # --------------------------------------------------------- installation
    Write-Host ''
    Write-Host 'Installing (silently, as an unprivileged user):' -ForegroundColor Cyan
    Write-Host '  NOTE: a silent install cannot observe SmartScreen, the licence page'
    Write-Host '        or the Alpha warning. Those are manual steps 5-7 below.'

    $process = Start-Process -FilePath $installer.FullName -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER')
    Add-Result 'installer completed without elevation' ($process.ExitCode -eq 0) "exit $($process.ExitCode)"

    $exe = Join-Path $env:LOCALAPPDATA 'Programs\OMRFlow\OMRFlow.exe'
    if (-not (Test-Path $exe)) {
        $found = Get-ChildItem $env:LOCALAPPDATA -Recurse -Filter 'OMRFlow.exe' -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($found) { $exe = $found.FullName }
    }
    Add-Result 'application installed under the user profile' (Test-Path $exe) $exe

    $startMenu = Get-ChildItem "$env:APPDATA\Microsoft\Windows\Start Menu\Programs" -Recurse `
        -Filter 'OMRFlow*.lnk' -ErrorAction SilentlyContinue
    Add-Result 'Start menu entry created' ([bool] $startMenu)

    if (-not (Test-Path $exe)) { throw 'Nothing to launch; stopping.' }

    # --------------------------------------------------------- first launch
    Write-Host ''
    Write-Host 'First launch - the single most important observation:' -ForegroundColor Cyan

    $app = Start-Process -FilePath $exe -PassThru
    $deadline = (Get-Date).AddSeconds(180)
    $title = ''
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $app.Refresh()
        if ($app.HasExited) { break }
        if ($app.MainWindowTitle) { $title = $app.MainWindowTitle; break }
    }
    $app.Refresh()

    Add-Result 'launches with no missing DLL, Python, Qt or VC++ runtime' (-not $app.HasExited) $(
        if ($app.HasExited) { "EXITED with $($app.ExitCode) - read the log on the Desktop" } else { 'running' })
    Add-Result 'a window appeared' ([bool] $title) $title

    if (-not $app.HasExited) {
        Start-Sleep -Seconds 5
        $app.Refresh()
        Add-Result 'responsive' $app.Responding
        Add-Result 'window title carries the version' ($title -match '^OMRFlow \d+\.\d+\.\d+')
    }

    # ------------------------------------------------------------- report
    Write-Host ''
    Write-Host '=============================================================' -ForegroundColor Cyan
    $failed = @($results | Where-Object { $_.Result -eq 'FAIL' })
    if ($failed.Count) {
        Write-Host " AUTOMATED PORTION: $($failed.Count) of $($results.Count) checks FAILED" -ForegroundColor Red
        Write-Host ' This is release-blocking. Capture %LOCALAPPDATA%\OMRFlow\logs\.' -ForegroundColor Red
    }
    else {
        Write-Host " AUTOMATED PORTION: all $($results.Count) checks passed" -ForegroundColor Green
    }
    Write-Host '=============================================================' -ForegroundColor Cyan

    $results | Format-Table -AutoSize | Out-String -Width 160 | Write-Host

    Write-Host 'STILL TO DO BY HAND - the automated portion cannot judge these.' -ForegroundColor Yellow
    Write-Host 'OMRFlow is open. Work down this list, then record the result in the'
    Write-Host 'sign-off table in docs\release\CLEAN_MACHINE_TEST.md:'
    Write-Host ''
    Write-Host '  * steps 5-7   SmartScreen wording, licence page, Alpha warning'
    Write-Host '                (uninstall, then run the installer interactively)'
    Write-Host '  * step 13     all nine stage icons render in the navigator'
    Write-Host '  * step 14     Help > About: version, Alpha, MIT, build identifier'
    Write-Host '  * steps 15-16 every stage renders; the footer reads Ready'
    Write-Host '  * steps 17-24 create a project, validate the example template,'
    Write-Host '                generate ~10 synthetic sheets, Process All,'
    Write-Host '                diagnostic bundle, project health'
    Write-Host '  * steps 25-28 close, relaunch, reopen the project, results intact'
    Write-Host '  * steps 29-36 uninstall, data preserved, reinstall, project opens'
    Write-Host ''
    Write-Host "  transcript : $transcript"
    Write-Host '  NOTE: the sandbox discards everything when it closes. Copy the'
    Write-Host '        transcript out through the clipboard before shutting down.'
    Write-Host ''
}
catch {
    Write-Host ''
    Write-Host "ERROR: $_" -ForegroundColor Red
    Write-Host $_.ScriptStackTrace
}
finally {
    Stop-Transcript | Out-Null
    Write-Host 'Press Enter to close this window (the sandbox stays open).'
    $null = Read-Host
}
