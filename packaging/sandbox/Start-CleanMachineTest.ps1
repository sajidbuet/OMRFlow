<#
.SYNOPSIS
    Runs inside Windows Sandbox. Performs every part of the clean-machine
    test that can be decided without a person looking at the screen.

.DESCRIPTION
    Launched automatically by OMRFlow-CleanMachine.wsb. It does not replace
    docs/release/CLEAN_MACHINE_TEST.md - that procedure requires a person to
    look at the interface, and the steps involving judgement stay theirs.
    What it does is remove the tedium and the opportunity for error from the
    mechanical parts, and - unlike a transcript that dies with the sandbox -
    write its verdict somewhere the host can read afterwards.

    The phases, in order:

      1. confirm the machine really is clean (no Python, no Qt, no VC++
         runtime beyond what Windows ships, no source checkout);
      2. verify the installer's SHA-256 against SHA256SUMS.txt, and that the
         installer asks for no elevation;
      3. install it, unprivileged, into the per-user location;
      4. inspect what was laid down - Qt plugins, the C++ runtime, the
         imaging and Excel backends, and the absence of a Python
         interpreter;
      5. launch it, and check it starts, titles its window with the version,
         stays responsive, logs under the user profile and exits cleanly;
      6. prove the run wrote nothing into the installation directory;
      7. launch it a second time - the persistence check;
      8. uninstall, and prove user data survived;
      9. reinstall, and prove the surviving data is still reachable.

    Everything that needs eyes is listed at the end, unperformed and
    labelled as such. A step that was not run is not a pass.

.PARAMETER ResultsPath
    Where to write the JSON report and the transcript. Defaults to the
    writable folder the sandbox configuration maps in, and falls back to the
    Desktop when the test runs somewhere with no such mapping - a separate
    physical machine, say - in which case the results must be copied out by
    hand before the machine is discarded.

.PARAMETER SkipReinstall
    Stop after the uninstall phase. For re-running the early phases quickly
    while diagnosing a failure.

.PARAMETER Unattended
    Return instead of waiting for a keypress at the end. Set this when the
    script is driven by `wsb exec` rather than by the sandbox's
    <LogonCommand>, so the calling process is not left blocked on a prompt
    nobody is going to answer. The report is written either way, and the
    sandbox stays open for the manual steps.
#>
[CmdletBinding()]
param(
    [string] $ResultsPath,
    [switch] $SkipReinstall,
    [switch] $Unattended
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# WINDOWS POWERSHELL 5.1 ONLY.
#
# This script runs on the machine under test, and a clean Windows install has
# Windows PowerShell 5.1 and nothing else - PowerShell 7 is a separate
# download, and installing it would make the machine less clean. So the
# PowerShell 7 conveniences the host-side scripts in scripts/release use are
# all unavailable here: no `??`, no `?:`, no `?.`, no `Test-Json`. Anything
# added below must parse under 5.1, which the host cannot verify by running
# it - check with:
#
#     powershell.exe -NoProfile -Command "[void][System.Management.Automation.Language.Parser]::ParseFile('<path>',[ref]$null,[ref]$e); $e"
#
# (`powershell.exe` is 5.1; `pwsh.exe` is 7. The difference is the whole point.)
# ---------------------------------------------------------------------------

function Join-Property($Items, [string] $Name, [int] $First = 3) {
    # `$collection.Property` and `(… | Select-Object -First 1).Property` both
    # throw under Set-StrictMode in Windows PowerShell 5.1 when the collection
    # is empty - and empty is the *passing* case for most of the checks below,
    # so the naive spelling fails exactly when the machine is clean.
    if ($null -eq $Items) { return '' }
    $values = @($Items | Select-Object -First $First | ForEach-Object {
            if ($null -ne $_) { $_.$Name } })
    return (@($values | Where-Object { $_ }) -join ', ')
}

function Get-VersionString($Item) {
    # VersionInfo.ProductVersion is $null for an unversioned binary, and
    # Set-StrictMode makes calling .Trim() on that fatal rather than empty.
    if ($null -eq $Item) { return '' }
    $value = $Item.VersionInfo.ProductVersion
    if ($null -eq $value) { return '' }
    return ([string] $value).Trim()
}

$payload = $PSScriptRoot
$desktop = [Environment]::GetFolderPath('Desktop')

if (-not $ResultsPath) {
    $mapped = Join-Path $desktop 'OMRFlow-results'
    $ResultsPath = if (Test-Path $mapped) { $mapped } else { $desktop }
}
try {
    New-Item -ItemType Directory -Force $ResultsPath -ErrorAction Stop | Out-Null
    # The mapped folder is writable only if the configuration says so. Prove
    # it now rather than discovering it after the whole test has run.
    $probe = Join-Path $ResultsPath '.writable'
    Set-Content -LiteralPath $probe -Value 'probe' -ErrorAction Stop
    Remove-Item -LiteralPath $probe -Force
}
catch {
    Write-Warning "$ResultsPath is not writable ($_). Falling back to the Desktop - copy the results out through the clipboard before closing the sandbox."
    $ResultsPath = $desktop
}

$transcript = Join-Path $ResultsPath 'clean-machine-transcript.log'
Start-Transcript -Path $transcript -Force | Out-Null

$results = [System.Collections.Generic.List[object]]::new()
$phase = ''

function Set-Phase([string] $Name) {
    $script:phase = $Name
    Write-Host ''
    Write-Host $Name -ForegroundColor Cyan
}

function Add-Result([string] $Name, [bool] $Passed, [string] $Detail = '', [string] $Step = '') {
    $mark = if ($Passed) { 'PASS' } else { 'FAIL' }
    $results.Add([pscustomobject]@{
            Phase = $script:phase; Check = $Name; Result = $mark; Detail = $Detail; Step = $Step
        })
    $colour = if ($Passed) { 'Green' } else { 'Red' }
    Write-Host ("  {0,-4} {1,-56} {2}" -f $mark, $Name, $Detail) -ForegroundColor $colour
}

function Add-Skip([string] $Name, [string] $Reason, [string] $Step = '') {
    $results.Add([pscustomobject]@{
            Phase = $script:phase; Check = $Name; Result = 'SKIPPED'; Detail = $Reason; Step = $Step
        })
    Write-Host ("  {0,-4} {1,-56} {2}" -f 'SKIP', $Name, $Reason) -ForegroundColor Yellow
}

# The application, its data and its shortcut, in the per-user locations the
# installer is configured to use. Resolved once; every phase refers to these.
$installDirectory = Join-Path $env:LOCALAPPDATA 'Programs\OMRFlow'
$installedExe = Join-Path $installDirectory 'OMRFlow.exe'
$userData = Join-Path $env:LOCALAPPDATA 'OMRFlow'
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'

function Invoke-Installer([string] $Path) {
    Start-Process -FilePath $Path -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER')
}

function Get-InstallFingerprint {
    # Relative path plus hash for every installed file, so a later comparison
    # names exactly which file the application wrote into its own directory -
    # the defect that makes an installation unrepairable and breaks upgrades.
    if (-not (Test-Path $installDirectory)) { return @{} }
    $map = @{}
    Get-ChildItem $installDirectory -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
        $relative = $_.FullName.Substring($installDirectory.Length).TrimStart([char]92)
        $map[$relative] = (Get-FileHash $_.FullName -Algorithm SHA256).Hash
    }
    return $map
}

function Get-UserDataInventory {
    if (-not (Test-Path $userData)) { return @() }
    return @(Get-ChildItem $userData -Recurse -File -ErrorAction SilentlyContinue |
        ForEach-Object { $_.FullName.Substring($userData.Length) })
}

function Start-AndObserve([string] $Exe, [int] $TimeoutSeconds = 180) {
    # Returns what the caller needs in order to judge a launch: whether it
    # survived, its window title, and whether it was still responding.
    $app = Start-Process -FilePath $Exe -PassThru
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $title = ''
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $app.Refresh()
        if ($app.HasExited) { break }
        if ($app.MainWindowTitle) { $title = $app.MainWindowTitle; break }
    }
    $app.Refresh()
    $responding = $false
    if (-not $app.HasExited) {
        Start-Sleep -Seconds 5
        $app.Refresh()
        $responding = $app.Responding
    }
    return [pscustomobject]@{ Process = $app; Title = $title; Responding = $responding }
}

function Stop-Cleanly($Observation) {
    $app = $Observation.Process
    if ($app.HasExited) { return [pscustomobject]@{ Clean = $false; ExitCode = $app.ExitCode } }
    $null = $app.CloseMainWindow()
    $clean = $app.WaitForExit(30000)
    if (-not $clean) { $app.Kill(); $null = $app.WaitForExit(5000) }
    return [pscustomobject]@{ Clean = $clean; ExitCode = $(if ($clean) { $app.ExitCode } else { -1 }) }
}

$environment = [ordered]@{}
$installerName = ''
$installerHash = ''
$installerVersion = ''
$settingsBefore = @()

try {
    Write-Host ''
    Write-Host '=============================================================' -ForegroundColor Cyan
    Write-Host ' OMRFlow - clean-machine installation test' -ForegroundColor Cyan
    Write-Host '=============================================================' -ForegroundColor Cyan

    $os = Get-CimInstance Win32_OperatingSystem
    $isAdmin = [bool] ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    $environment = [ordered]@{
        windows_caption       = $os.Caption
        windows_version       = [string] [System.Environment]::OSVersion.Version
        windows_build         = [string] $os.BuildNumber
        machine_type          = $(if (Test-Path 'C:\Users\WDAGUtilityAccount') { 'Windows Sandbox' } else { 'other' })
        username              = $env:USERNAME
        user_is_administrator = $isAdmin
        started_utc           = (Get-Date).ToUniversalTime().ToString('o')
    }
    $environment.GetEnumerator() | ForEach-Object { Write-Host ("  {0,-22} {1}" -f $_.Key, $_.Value) }

    # ------------------------------------------------ 1. is it really clean?
    Set-Phase '1. Confirming the machine is clean'

    $pythonOnPath = @(Get-Command python, python3, py -ErrorAction SilentlyContinue)
    Add-Result 'no Python on the PATH' ($pythonOnPath.Count -eq 0) (Join-Property $pythonOnPath 'Source')

    $pythonInstalled = @(Get-ChildItem 'C:\Python*', "$env:LOCALAPPDATA\Programs\Python" -ErrorAction SilentlyContinue)
    Add-Result 'no Python installation' ($pythonInstalled.Count -eq 0)

    # Recorded, not judged. Current Windows images ship the Visual C++
    # runtime in System32 themselves, so its presence no longer means a
    # developer tool put it there - failing on it would fail on a pristine
    # image. What actually matters is that OMRFlow carries its own copy
    # rather than relying on this one, and that is checked in phase 4 and by
    # packaging/audit_dependencies.py on the build machine.
    $vcRuntime = @(Get-ChildItem "$env:SystemRoot\System32" -Filter 'vcruntime140*.dll' -ErrorAction SilentlyContinue)
    Add-Skip 'VC++ runtime in System32' $(
        if ($vcRuntime.Count) {
            "present ($($vcRuntime.Count) file(s)) - shipped by Windows; phase 4 checks OMRFlow bundles its own"
        }
        else { 'absent' })

    Add-Result 'no Qt installation' (@(Get-ChildItem 'C:\Qt' -ErrorAction SilentlyContinue).Count -eq 0)

    # The payload is the only thing mapped in. If a source tree reached this
    # machine the test could not tell a bundled import from a local one.
    $sourceMarkers = @(Get-ChildItem $payload -Recurse -Include 'pyproject.toml', '*.py' -ErrorAction SilentlyContinue)
    Add-Result 'no OMRFlow source in the payload' ($sourceMarkers.Count -eq 0) (Join-Property $sourceMarkers 'Name')
    Add-Result 'no OMRFlow already installed' (-not (Test-Path $installDirectory))
    Add-Result 'no OMRFlow user data from an earlier run' (-not (Test-Path $userData))

    # ------------------------------------------------------- 2. the artifact
    Set-Phase '2. The artifact'

    $installer = Get-ChildItem $payload -Filter '*Setup*.exe' | Select-Object -First 1
    if (-not $installer) { throw "No installer in the payload folder ($payload). Run New-SandboxPayload.ps1 on the host." }
    $installerName = $installer.Name
    $installerHash = (Get-FileHash $installer.FullName -Algorithm SHA256).Hash.ToLower()
    Write-Host ("  installer : {0}  ({1:N1} MB)" -f $installer.Name, ($installer.Length / 1MB))
    Write-Host ("  sha-256   : {0}" -f $installerHash)

    $sumsFile = Join-Path $payload 'SHA256SUMS.txt'
    if (Test-Path $sumsFile) {
        $expectedLine = Select-String -Path $sumsFile -Pattern ([regex]::Escape($installer.Name)) | Select-Object -First 1
        $expected = if ($expectedLine) { ($expectedLine.Line -split '\s+')[0].ToLower() } else { '' }
        Add-Result 'SHA-256 matches SHA256SUMS.txt' ([bool] $expected -and $installerHash -eq $expected) $installerHash '3'
    }
    else {
        Add-Result 'SHA256SUMS.txt present' $false 'not staged - cannot verify the download' '3'
    }

    $installerVersion = Get-VersionString $installer
    Add-Result 'installer carries a version' ([bool] $installerVersion) $installerVersion

    # PrivilegesRequired=lowest in the Inno script means the setup binary is
    # manifested asInvoker. Read that back from the artifact rather than
    # trusting the build: it is what decides whether an operator without
    # administrator rights can install at all, and a silent install cannot
    # observe a UAC prompt that never appeared.
    $manifestLevel = ''
    try {
        $text = [System.Text.Encoding]::UTF8.GetString(
            [System.IO.File]::ReadAllBytes($installer.FullName))
        $match = [regex]::Match($text, '<requestedExecutionLevel[^>]*level="(?<level>[a-zA-Z]+)"')
        if ($match.Success) { $manifestLevel = $match.Groups['level'].Value }
    }
    catch { $manifestLevel = '' }
    if ($manifestLevel) {
        Add-Result 'installer requests no elevation (asInvoker)' ($manifestLevel -eq 'asInvoker') $manifestLevel '4'
    }
    else {
        Add-Skip 'installer elevation level' 'no requestedExecutionLevel found in the manifest' '4'
    }

    # --------------------------------------------------------- 3. installing
    Set-Phase '3. Installing, unprivileged, into the per-user location'
    Write-Host '  NOTE: a silent install cannot observe SmartScreen, the licence page'
    Write-Host '        or the Alpha warning. Those stay manual - listed at the end.'

    $process = Invoke-Installer $installer.FullName
    Add-Result 'installer exited successfully' ($process.ExitCode -eq 0) "exit $($process.ExitCode)" '8'
    Add-Result 'installed under the user profile' (Test-Path $installedExe) $installDirectory '8'
    if (-not (Test-Path $installedExe)) { throw 'Nothing was installed; stopping.' }

    $shortcuts = @(Get-ChildItem $startMenu -Recurse -Filter 'OMRFlow*.lnk' -ErrorAction SilentlyContinue)
    Add-Result 'Start menu entry created' ($shortcuts.Count -gt 0) (Join-Property $shortcuts 'Name' 1) '9'

    $registered = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -like 'OMRFlow*' } | Select-Object -First 1
    Add-Result 'listed in Apps & features' ([bool] $registered) $(if ($registered) { $registered.DisplayName })

    # ---------------------------------------- 4. what was actually laid down
    Set-Phase '4. What the installer laid down'

    $installedVersion = Get-VersionString (Get-Item $installedExe)
    Add-Result 'installed executable reports the version' ([bool] $installedVersion) $installedVersion
    Add-Result 'licence installed beside the application' (Test-Path (Join-Path $installDirectory 'LICENSE.txt')) '' '6'

    $interpreters = @(Get-ChildItem $installDirectory -Recurse -Include 'python.exe', 'pythonw.exe' -ErrorAction SilentlyContinue)
    Add-Result 'no Python interpreter inside the installation' ($interpreters.Count -eq 0) (Join-Property $interpreters 'Name' 2)

    # Each of these is a dependency that behaves differently once frozen, and
    # each has its own way of failing silently on a machine with no
    # development toolchain.
    #
    # Only dependencies with compiled extensions appear here. PyInstaller
    # lays those out as directories beside the executable, but freezes a
    # *pure* Python package - openpyxl, et_xmlfile - into the PYZ archive
    # inside OMRFlow.exe, where there is nothing for Test-Path to find.
    # Looking for a directory that was never going to exist reports a missing
    # dependency that is present, which is worse than not checking. Those are
    # verified on the build machine instead, by
    # packaging/verify_frozen_imports.py, which reads the archive.
    $required = [ordered]@{
        'Qt core runtime'              = '_internal\PySide6\Qt6Core.dll'
        'Qt GUI runtime'               = '_internal\PySide6\Qt6Gui.dll'
        'Qt Windows platform plugin'   = '_internal\PySide6\plugins\platforms\qwindows.dll'
        'Qt image format plugins'      = '_internal\PySide6\plugins\imageformats'
        'Visual C++ runtime (bundled)' = '_internal\VCRUNTIME140.dll'
        'Pillow imaging backend'       = '_internal\PIL'
        'OpenCV'                       = '_internal\cv2'
        'NumPy'                        = '_internal\numpy'
        'application branding assets'  = '_internal\omr_scanner\gui\resources\branding\icon.ico'
        'workflow stage icons'         = '_internal\omr_scanner\gui\resources\icons\lucide'
        'bundled sample workbook'      = '_internal\omr_scanner\resources\templates'
    }
    foreach ($entry in $required.GetEnumerator()) {
        Add-Result "bundled: $($entry.Key)" (Test-Path (Join-Path $installDirectory $entry.Value)) $entry.Value
    }

    $iconDirectory = Join-Path $installDirectory '_internal\omr_scanner\gui\resources\icons\lucide'
    $iconCount = @(Get-ChildItem $iconDirectory -Filter '*.svg' -ErrorAction SilentlyContinue).Count
    # A proxy for "all nine stage icons render", which needs eyes. Files that
    # are absent certainly cannot render.
    Add-Result 'stage icon set is present' ($iconCount -ge 9) "$iconCount SVG icons" '13'

    $fingerprintBefore = Get-InstallFingerprint
    Write-Host ("  fingerprinted {0} installed files" -f $fingerprintBefore.Count)

    # ------------------------------------------------------- 5. first launch
    Set-Phase '5. First launch - the single most important observation'

    $first = Start-AndObserve $installedExe
    Add-Result 'launches with no missing DLL, Python, Qt or VC++ runtime' (-not $first.Process.HasExited) $(
        if ($first.Process.HasExited) { "EXITED with $($first.Process.ExitCode)" } else { 'running' }) '11'
    Add-Result 'a window appeared' ([bool] $first.Title) $first.Title '10'
    if ($first.Title) {
        Add-Result 'window title reads OMRFlow <version>' ($first.Title -match '^OMRFlow \d+\.\d+\.\d+') $first.Title '12'
        Add-Result 'window title matches the installed version' (
            [bool] $installedVersion -and $first.Title -like "*$installedVersion*") $first.Title '12'
    }
    if (-not $first.Process.HasExited) { Add-Result 'responsive' $first.Responding }

    $logDirectory = Join-Path $userData 'logs'
    $logs = @(Get-ChildItem $logDirectory -Filter '*.log' -ErrorAction SilentlyContinue)
    Add-Result 'wrote a log under the user profile' ($logs.Count -gt 0) $logDirectory
    if ($logs.Count) {
        # -Raw returns $null for a zero-length file, and both -match and
        # [regex]::Match are fatal on $null under StrictMode.
        $logText = [string] (Get-Content $logs[0].FullName -Raw -ErrorAction SilentlyContinue)
        Add-Result 'log records the build identifier with its commit' (
            $logText -match 'OMRFlow \d+\.\d+\.\d+\S*\+[0-9a-f]{7}') (
            [regex]::Match($logText, 'OMRFlow \S+ \([^)]+\) starting[^\r\n]*').Value)
        Add-Result 'no traceback in the first-launch log' (
            $logText -notmatch 'Traceback \(most recent call last\)') $(
            if ($logText -match 'Traceback') { 'a traceback was logged - read the log' })
    }

    $stop = Stop-Cleanly $first
    Add-Result 'closed cleanly on request' $stop.Clean $(
        if ($stop.Clean) { "exit code $($stop.ExitCode)" } else { 'had to be killed' }) '25'
    if ($stop.Clean) { Add-Result 'exit code is zero' ($stop.ExitCode -eq 0) "$($stop.ExitCode)" '25' }
    Add-Result 'no OMRFlow process left behind' (
        @(Get-Process -Name 'OMRFlow' -ErrorAction SilentlyContinue).Count -eq 0) '' '25'

    # ------------------------------------- 6. did it write where it must not?
    Set-Phase '6. The installation directory must not be written to'

    $fingerprintAfter = Get-InstallFingerprint
    $added = @($fingerprintAfter.Keys | Where-Object { -not $fingerprintBefore.ContainsKey($_) })
    $changed = @($fingerprintAfter.Keys | Where-Object {
            $fingerprintBefore.ContainsKey($_) -and $fingerprintBefore[$_] -ne $fingerprintAfter[$_] })
    Add-Result 'wrote nothing into the installation directory' (
        $added.Count -eq 0 -and $changed.Count -eq 0) $(
        if ($added.Count -or $changed.Count) { "added: $($added -join ', '); changed: $($changed -join ', ')" } else { 'unchanged' })

    # ----------------------------------------------- 7. restart / persistence
    Set-Phase '7. Second launch - the application survives a restart'

    $second = Start-AndObserve $installedExe
    Add-Result 'relaunches after a previous session' (-not $second.Process.HasExited) $second.Title '26'
    Add-Result 'second launch shows a window' ([bool] $second.Title) $second.Title '26'
    $stop2 = Stop-Cleanly $second
    Add-Result 'second session closes cleanly' $stop2.Clean "exit $($stop2.ExitCode)" '28'

    # @(): a function returning one item returns the item, not a one-element
    # array, and .Count on a bare string is fatal under StrictMode in 5.1.
    $settingsBefore = @(Get-UserDataInventory)
    Add-Result 'user data directory populated' ($settingsBefore.Count -gt 0) "$($settingsBefore.Count) files under $userData"

    # -------------------------------------------------------- 8. uninstalling
    Set-Phase '8. Uninstall - and user data must survive it'

    $uninstaller = Get-ChildItem $installDirectory -Filter 'unins*.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $uninstaller) {
        Add-Result 'uninstaller present' $false 'no unins*.exe in the installation directory' '29'
    }
    else {
        $removal = Start-Process -FilePath $uninstaller.FullName -Wait -PassThru -ArgumentList @(
            '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART')
        Add-Result 'uninstaller exited successfully' ($removal.ExitCode -eq 0) "exit $($removal.ExitCode)" '29'
        # Inno's uninstaller returns before the last files are gone, because
        # it has to delete itself last.
        $deadline = (Get-Date).AddSeconds(60)
        while ((Test-Path $installedExe) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }

        Add-Result 'application binary removed' (-not (Test-Path $installedExe)) '' '30'
        Add-Result 'bundled runtime removed' (-not (Test-Path (Join-Path $installDirectory '_internal'))) '' '30'
        Add-Result 'Start menu entry removed' (
            @(Get-ChildItem $startMenu -Recurse -Filter 'OMRFlow*.lnk' -ErrorAction SilentlyContinue).Count -eq 0) '' '31'
        $stillRegistered = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
            Where-Object { $_.DisplayName -like 'OMRFlow*' }
        Add-Result 'removed from Apps & features' (-not $stillRegistered)

        # The requirement the uninstaller exists to honour.
        Add-Result 'user data directory survived the uninstall' (Test-Path $userData) $userData '33'
        $settingsAfter = @(Get-UserDataInventory)
        $lost = @($settingsBefore | Where-Object { $_ -notin $settingsAfter })
        Add-Result 'no user file was deleted by the uninstall' ($lost.Count -eq 0) $(
            if ($lost.Count) { "lost: $($lost -join ', ')" } else { "$($settingsAfter.Count) files intact" }) '33'
    }

    # -------------------------------------------------------- 9. reinstalling
    if ($SkipReinstall) {
        Set-Phase '9. Reinstall'
        Add-Skip 'reinstall' '-SkipReinstall was given' '34'
    }
    else {
        Set-Phase '9. Reinstall - and the retained data is still reachable'
        $again = Invoke-Installer $installer.FullName
        Add-Result 'reinstaller exited successfully' ($again.ExitCode -eq 0) "exit $($again.ExitCode)" '34'
        Add-Result 'application reinstalled' (Test-Path $installedExe) '' '34'

        $third = Start-AndObserve $installedExe
        Add-Result 'reinstalled application launches' (
            -not $third.Process.HasExited -and [bool] $third.Title) $third.Title '35'
        $settingsNow = @(Get-UserDataInventory)
        $lostNow = @($settingsBefore | Where-Object { $_ -notin $settingsNow })
        Add-Result 'retained user data still present after reinstall' ($lostNow.Count -eq 0) $(
            if ($lostNow.Count) { "missing: $($lostNow -join ', ')" } else { 'intact' }) '35'
        $stop3 = Stop-Cleanly $third
        Add-Result 'reinstalled application closes cleanly' $stop3.Clean "exit $($stop3.ExitCode)"
    }
}
catch {
    Write-Host ''
    Write-Host "ERROR: $_" -ForegroundColor Red
    Write-Host $_.ScriptStackTrace
    $results.Add([pscustomobject]@{
            Phase = $phase; Check = 'test harness ran to completion'; Result = 'FAIL'
            Detail = "$_"; Step = ''
        })
}
finally {
    # --------------------------------------------------------------- verdict
    $passed = @($results | Where-Object { $_.Result -eq 'PASS' })
    $failed = @($results | Where-Object { $_.Result -eq 'FAIL' })
    $skipped = @($results | Where-Object { $_.Result -eq 'SKIPPED' })

    # The steps of docs/release/CLEAN_MACHINE_TEST.md that no script can
    # decide. Named here so the report states what was NOT done in the same
    # place, and with the same prominence, as what was.
    $manual = @(
        @{ Step = '5'; What = 'SmartScreen warning and its exact wording (this sandbox has no network, so SmartScreen cannot appear here at all)' }
        @{ Step = '6'; What = 'the licence page shows the MIT licence' }
        @{ Step = '7'; What = 'the Alpha warning is shown during installation' }
        @{ Step = '13'; What = 'all nine stage icons actually render in the navigator' }
        @{ Step = '14'; What = 'Help > About: version, Alpha, MIT, build identifier on hover' }
        @{ Step = '15-16'; What = 'every stage renders; the footer reads Ready' }
        @{ Step = '17-24'; What = 'create a project, validate the example template, generate synthetic sheets, Process All, diagnostic bundle, project health' }
        @{ Step = '27'; What = 'a previously created project reopens with its results' }
        @{ Step = '32'; What = 'a project folder outside the user data directory survives the uninstall' }
        @{ Step = '35'; What = 'the previously created project opens after the reinstall' }
    )

    Write-Host ''
    Write-Host '=============================================================' -ForegroundColor Cyan
    if ($failed.Count) {
        Write-Host " AUTOMATED PORTION: $($failed.Count) of $($results.Count) checks FAILED" -ForegroundColor Red
        Write-Host ' This is release-blocking. Capture %LOCALAPPDATA%\OMRFlow\logs\.' -ForegroundColor Red
    }
    else {
        Write-Host " AUTOMATED PORTION: $($passed.Count) passed, $($skipped.Count) skipped, 0 failed" -ForegroundColor Green
    }
    Write-Host '=============================================================' -ForegroundColor Cyan
    $results | Format-Table Phase, Result, Check, Detail -AutoSize | Out-String -Width 200 | Write-Host

    Write-Host 'NOT PERFORMED - these need a person to look at the screen:' -ForegroundColor Yellow
    foreach ($item in $manual) { Write-Host ("  step {0,-6} {1}" -f $item.Step, $item.What) }
    Write-Host ''

    # ------------------------------------------------------------ the report
    $report = [ordered]@{
        schema        = 'omrflow.clean-machine-test/1'
        generated     = (Get-Date).ToUniversalTime().ToString('o')
        environment   = $environment
        installer     = [ordered]@{
            name = $installerName; sha256 = $installerHash; version = $installerVersion
        }
        summary       = [ordered]@{
            passed            = $passed.Count
            failed            = $failed.Count
            skipped           = $skipped.Count
            automated_verdict = $(if ($failed.Count) { 'FAIL' } else { 'PASS' })
        }
        checks        = @($results)
        not_performed = @($manual | ForEach-Object { [ordered]@{ step = $_.Step; what = $_.What } })
    }

    $jsonPath = Join-Path $ResultsPath 'clean-machine-results.json'
    try {
        $report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $jsonPath -Encoding UTF8
        Write-Host "  results    : $jsonPath" -ForegroundColor Green
    }
    catch { Write-Warning "Could not write $jsonPath - $_" }
    Write-Host "  transcript : $transcript"

    # A marker the host polls for, so an unattended run can be collected
    # without having to guess when the sandbox finished.
    try {
        Set-Content -LiteralPath (Join-Path $ResultsPath 'COMPLETE') `
            -Value $report.summary.automated_verdict -Encoding UTF8
    }
    catch { }

    Stop-Transcript | Out-Null
    Write-Host ''
    Write-Host 'OMRFlow is installed and closed. Work down the NOT PERFORMED list'
    Write-Host 'above, then record the outcome in the sign-off table in'
    Write-Host 'docs\release\CLEAN_MACHINE_TEST.md.'
    Write-Host ''
    if (-not $Unattended) {
        Write-Host 'Press Enter to close this window (the sandbox stays open).'
        $null = Read-Host
    }
}
