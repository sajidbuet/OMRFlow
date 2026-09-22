<#
.SYNOPSIS
    Check that the installed application is self-contained, without needing a
    clean machine.

.DESCRIPTION
    Installs the release installer into a scratch directory, then launches
    what it installed in a **sanitised environment**: no Python on the PATH,
    every PYTHON*/QT* variable cleared, and a working directory nowhere near
    the source tree. Also audits every binary in the bundle for imports that
    would not resolve on a fresh machine.

    This is the strongest approximation of a clean-machine test that can be
    run on a development machine. It catches:

      * a dependency that was never bundled (import audit);
      * the C/C++ runtime being expected from the system rather than shipped;
      * reliance on PYTHONPATH or PYTHONHOME;
      * reliance on the source tree being importable;
      * Qt plugins found through QT_PLUGIN_PATH rather than from the bundle;
      * a Python interpreter being found on the PATH.

    It cannot catch a DLL that is present in System32 on *this* machine
    because a developer tool put it there. Only a genuinely fresh Windows
    install can, which is why this is a substitute and not a replacement -
    see docs/release/CLEAN_MACHINE_TEST.md, which remains the required test.

.PARAMETER Installer
    Installer to test. Defaults to the newest in dist/installer.

.PARAMETER InstallDirectory
    Where to install for the test. Defaults to a scratch directory under
    TEMP. Give a path containing spaces, or characters outside ASCII, to
    check that the packaged application survives one: a build that quotes a
    path badly, or that passes one through a byte-oriented API, works
    perfectly under `C:\Program Files` and fails for the operator whose
    account is named for a person rather than a login.

.EXAMPLE
    .\scripts\release\Test-SelfContained.ps1

.EXAMPLE
    .\scripts\release\Test-SelfContained.ps1 -InstallDirectory "$env:TEMP\Parikşa Dosyası 2026\OMRFlow"
#>
[CmdletBinding()]
param(
    [string] $Installer,
    [string] $InstallDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

$checks = [System.Collections.Generic.List[object]]::new()
function Add-Check([string] $Name, [bool] $Passed, [string] $Detail = '') {
    $checks.Add([pscustomobject]@{ Name = $Name; Passed = $Passed })
    $mark = if ($Passed) { 'PASS' } else { 'FAIL' }
    $colour = if ($Passed) { 'Green' } else { 'Red' }
    Write-Host ("  {0,-4} {1,-50} {2}" -f $mark, $Name, $Detail) -ForegroundColor $colour
}

$installDirectory = if ($InstallDirectory) { $InstallDirectory }
else { Join-Path $env:TEMP "omrflow-selfcontained-$(Get-Random)" }
$scratch = Join-Path $env:TEMP "omrflow-scratch-$(Get-Random)"
$startedAt = Get-Date

try {
    $version = & (Join-Path $PSScriptRoot 'Get-OMRFlowVersion.ps1')
    $python = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { $python = 'python' }

    if (-not $Installer) {
        $found = Get-ChildItem (Join-Path $repositoryRoot 'dist\installer') -Filter '*Setup*.exe' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $found) { throw 'No installer in dist\installer. Run Build-Installer.ps1 first.' }
        $Installer = $found.FullName
    }

    Write-Host "Self-containment check - OMRFlow $($version.Version)" -ForegroundColor Cyan
    Write-Host "  installer : $(Split-Path $Installer -Leaf)"
    Write-Host ''

    # ------------------------------------------------- 1. the import audit
    Write-Host '  auditing the bundle''s DLL imports...'
    $auditOutput = & $python (Join-Path $repositoryRoot 'packaging\audit_dependencies.py') `
        (Join-Path $repositoryRoot 'dist\OMRFlow') 2>&1
    $auditPassed = $LASTEXITCODE -eq 0
    $unresolved = ($auditOutput | Select-String '^\s+UNRESOLVED\s+:\s+(\d+)').Matches.Groups[1].Value
    Add-Check 'no unresolved DLL imports in the bundle' $auditPassed "UNRESOLVED: $unresolved"
    if (-not $auditPassed) { $auditOutput | Select-Object -Last 25 | ForEach-Object { Write-Host "      $_" } }

    # The import audit reads PE tables, so it sees only compiled code. A pure
    # Python dependency dropped from the freeze - openpyxl, say - leaves the
    # DLL audit perfectly clean and breaks Excel reporting. Reading the PYZ
    # archive is the only way to notice.
    Write-Host '  verifying the frozen Python imports...'
    $frozenOutput = & $python (Join-Path $repositoryRoot 'packaging\verify_frozen_imports.py') `
        (Join-Path $repositoryRoot 'dist\OMRFlow') 2>&1
    $frozenPassed = $LASTEXITCODE -eq 0
    Add-Check 'every imported dependency survived the freeze' $frozenPassed
    if (-not $frozenPassed) { $frozenOutput | ForEach-Object { Write-Host "      $_" } }

    # -------------------------------------------------- 2. install it fresh
    Write-Host '  installing into a scratch directory...'
    $process = Start-Process -FilePath $Installer -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER',
        "/DIR=$installDirectory"
    )
    Add-Check 'installer succeeded' ($process.ExitCode -eq 0) "exit $($process.ExitCode)"
    $exe = Join-Path $installDirectory 'OMRFlow.exe'
    Add-Check 'application installed' (Test-Path $exe)
    if (-not (Test-Path $exe)) { throw 'Nothing installed; cannot continue.' }

    Add-Check 'no Python interpreter inside the installation' (
        -not (Get-ChildItem $installDirectory -Recurse -Filter 'python.exe' -ErrorAction SilentlyContinue)
    )

    # --------------------------------- 3. launch with a sanitised environment
    New-Item -ItemType Directory -Force $scratch | Out-Null

    # Only the Windows directories. No Python, no venv, no repository, and
    # deliberately not the current PATH - this is the environment a machine
    # without a development toolchain would present.
    $minimalPath = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\Wbem"
    $stripped = @(
        'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONEXECUTABLE',
        'PYTHONNOUSERSITE', 'VIRTUAL_ENV', 'CONDA_PREFIX', 'PIP_TARGET',
        'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'QT_QPA_PLATFORM',
        'QML2_IMPORT_PATH', 'QML_IMPORT_PATH', 'QT_DIR', 'QTDIR',
        'OPENCV_DIR', 'OMRFLOW_BUILD_COMMIT'
    )

    Write-Host '  launching with a sanitised environment...'
    Write-Host "      PATH = $minimalPath"
    Write-Host "      cleared: $($stripped -join ', ')"

    $info = [System.Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $exe
    $info.WorkingDirectory = $scratch      # nowhere near the source tree
    $info.UseShellExecute = $false
    $info.EnvironmentVariables.Clear()
    # The minimum a Windows GUI process needs to start at all.
    $info.EnvironmentVariables['PATH'] = $minimalPath
    $info.EnvironmentVariables['SystemRoot'] = $env:SystemRoot
    $info.EnvironmentVariables['SystemDrive'] = $env:SystemDrive
    $info.EnvironmentVariables['WINDIR'] = $env:WINDIR
    $info.EnvironmentVariables['TEMP'] = $env:TEMP
    $info.EnvironmentVariables['TMP'] = $env:TEMP
    $info.EnvironmentVariables['USERPROFILE'] = $env:USERPROFILE
    $info.EnvironmentVariables['LOCALAPPDATA'] = $env:LOCALAPPDATA
    $info.EnvironmentVariables['APPDATA'] = $env:APPDATA
    $info.EnvironmentVariables['USERNAME'] = $env:USERNAME
    $info.EnvironmentVariables['COMPUTERNAME'] = $env:COMPUTERNAME
    $info.EnvironmentVariables['NUMBER_OF_PROCESSORS'] = $env:NUMBER_OF_PROCESSORS
    $info.EnvironmentVariables['PROCESSOR_ARCHITECTURE'] = $env:PROCESSOR_ARCHITECTURE
    $info.EnvironmentVariables['ProgramData'] = $env:ProgramData
    $info.EnvironmentVariables['ProgramFiles'] = $env:ProgramFiles
    $info.EnvironmentVariables['PUBLIC'] = $env:PUBLIC
    $info.EnvironmentVariables['HOMEDRIVE'] = $env:HOMEDRIVE
    $info.EnvironmentVariables['HOMEPATH'] = $env:HOMEPATH

    $app = [System.Diagnostics.Process]::Start($info)
    $deadline = (Get-Date).AddSeconds(120)
    $title = ''
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $app.Refresh()
        if ($app.HasExited) { break }
        if ($app.MainWindowTitle) { $title = $app.MainWindowTitle; break }
    }
    $app.Refresh()

    Add-Check 'starts with no Python on the PATH' (-not $app.HasExited) $(
        if ($app.HasExited) { "exited with $($app.ExitCode)" } else { 'running' })
    Add-Check 'window appears' ([bool] $title) $title

    if (-not $app.HasExited) {
        Add-Check 'window title carries the version' ($title -like "*$($version.Version)*")
        Start-Sleep -Seconds 3
        $app.Refresh()
        Add-Check 'responsive' $app.Responding

        # The Qt GUI, the imaging stack and the icon resources have all been
        # exercised by now: a missing Qt platform plugin or an unbundled
        # OpenCV dependency would have prevented the window appearing at all.
        Add-Check 'loaded its Qt platform plugin from the bundle' ([bool] $title)

        $null = $app.CloseMainWindow()
        $clean = $app.WaitForExit(20000)
        if (-not $clean) { $app.Kill() }
        Add-Check 'closes cleanly' ($clean -and $app.ExitCode -eq 0) $(
            if ($clean) { "exit $($app.ExitCode)" } else { 'had to be killed' })
    }

    # ------------------------------------- 4. did it write where it should?
    # The log directory already exists on a development machine, so its mere
    # presence proves nothing: require a log touched since this run started.
    $logRoot = Join-Path $env:LOCALAPPDATA 'OMRFlow\logs'
    $freshLog = Get-ChildItem $logRoot -Filter '*.log' -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -ge $startedAt } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    Add-Check 'wrote a log under the user profile during this run' ([bool] $freshLog) $(
        if ($freshLog) { $freshLog.Name } else { "nothing newer than $($startedAt.ToString('HH:mm:ss')) in $logRoot" })
    Add-Check 'wrote nothing into the installation directory' (
        -not (Get-ChildItem $installDirectory -Recurse -Include '*.log', '*.db', '*.ini' -ErrorAction SilentlyContinue)
    )

    # ------------------------------------------------------------ uninstall
    $uninstaller = Get-ChildItem $installDirectory -Filter 'unins*.exe' -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($uninstaller) {
        $null = Start-Process -FilePath $uninstaller.FullName -Wait -PassThru -ArgumentList @(
            '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART')
        Start-Sleep -Seconds 5
        Add-Check 'uninstalled' (-not (Test-Path $exe))
    }

    Write-Host ''
    $failed = @($checks | Where-Object { -not $_.Passed })
    if ($failed.Count -gt 0) {
        Write-Host "$($failed.Count) of $($checks.Count) checks FAILED." -ForegroundColor Red
        exit 1
    }
    Write-Host "All $($checks.Count) checks passed." -ForegroundColor Green
    Write-Host ''
    Write-Host 'This is NOT a clean-machine test.' -ForegroundColor Yellow
    Write-Host 'It cannot detect a DLL that is present in System32 on this machine'
    Write-Host 'because a developer tool installed it there. Run the real test on a'
    Write-Host 'fresh Windows install: docs\release\CLEAN_MACHINE_TEST.md'
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
finally {
    foreach ($path in @($installDirectory, $scratch)) {
        if (Test-Path $path) { Remove-Item -Recurse -Force $path -ErrorAction SilentlyContinue }
    }
}
