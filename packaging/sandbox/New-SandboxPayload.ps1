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

.PARAMETER Installer
    Installer to test. Defaults to the newest in dist/installer.

.PARAMETER Launch
    Start Windows Sandbox once the payload is staged.

.EXAMPLE
    .\packaging\sandbox\New-SandboxPayload.ps1 -Launch
#>
[CmdletBinding()]
param(
    [string] $Installer,
    [switch] $Launch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$payload = Join-Path $PSScriptRoot 'payload'

if (-not $Installer) {
    $found = Get-ChildItem (Join-Path $repositoryRoot 'dist\installer') -Filter '*Setup*.exe' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $found) { throw 'No installer in dist\installer. Run .\scripts\release\Build-Installer.ps1 first.' }
    $Installer = $found.FullName
}

if (Test-Path $payload) { Remove-Item -Recurse -Force $payload }
New-Item -ItemType Directory -Force $payload | Out-Null

Copy-Item $Installer $payload
Copy-Item (Join-Path $PSScriptRoot 'Start-CleanMachineTest.ps1') $payload

$sums = Join-Path (Split-Path $Installer -Parent) 'SHA256SUMS.txt'
if (Test-Path $sums) {
    Copy-Item $sums $payload
}
else {
    Write-Warning "No SHA256SUMS.txt beside the installer. Run .\scripts\release\New-Checksums.ps1 - the test cannot verify the download otherwise."
}

# Windows Sandbox's support for a relative <HostFolder> depends on the
# Windows build, and a rejected configuration is a confusing way to start a
# release test. Generate a copy with the path resolved, and launch that.
$template = Join-Path $PSScriptRoot 'OMRFlow-CleanMachine.wsb'
$generated = Join-Path $PSScriptRoot 'OMRFlow-CleanMachine.generated.wsb'
(Get-Content -Raw $template).Replace('<HostFolder>.\payload</HostFolder>',
    "<HostFolder>$payload</HostFolder>") | Set-Content -LiteralPath $generated -Encoding UTF8

Write-Host "Payload staged in $payload" -ForegroundColor Green
Get-ChildItem $payload | ForEach-Object {
    Write-Host ("  {0,-42} {1,8:N1} MB" -f $_.Name, ($_.Length / 1MB))
}
Write-Host ''

# ------------------------------------------- is Windows Sandbox available?
$sandbox = Join-Path $env:SystemRoot 'System32\WindowsSandbox.exe'
if (Test-Path $sandbox) {
    Write-Host 'Windows Sandbox is available.' -ForegroundColor Green
    $configuration = $generated
    if ($Launch) {
        Write-Host 'Starting the sandbox...'
        Start-Process $sandbox -ArgumentList $configuration
    }
    else {
        Write-Host "Run it with:  & '$sandbox' '$configuration'"
        Write-Host '          or:  re-run this script with -Launch'
    }
    exit 0
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
