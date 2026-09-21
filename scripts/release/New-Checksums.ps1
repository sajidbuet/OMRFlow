<#
.SYNOPSIS
    Write SHA-256 checksums for the release artifacts.

.DESCRIPTION
    Produces `dist/installer/SHA256SUMS.txt` in the conventional
    `<hash>  <filename>` format, so a downloader can verify an artifact with
    either Windows' certutil or the sha256sum most other systems have.

    Checksums matter more than usual here: the Alpha installer is unsigned,
    so a hash published beside the download is the only way a tester can
    confirm they got the file the maintainer built.

.PARAMETER Path
    Directory to checksum. Defaults to `dist/installer`.

.EXAMPLE
    .\scripts\release\New-Checksums.ps1
#>
[CmdletBinding()]
param(
    [string] $Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $Path) { $Path = Join-Path $repositoryRoot 'dist\installer' }

try {
    if (-not (Test-Path $Path)) {
        throw "No such directory: $Path. Run Build-Installer.ps1 first."
    }

    # The checksum file itself is excluded, for the obvious reason.
    $artifacts = Get-ChildItem -Path $Path -File |
        Where-Object { $_.Name -ne 'SHA256SUMS.txt' } |
        Sort-Object Name

    if (-not $artifacts) { throw "No artifacts to checksum in $Path." }

    $output = Join-Path $Path 'SHA256SUMS.txt'
    $lines = foreach ($artifact in $artifacts) {
        $hash = (Get-FileHash -Algorithm SHA256 -Path $artifact.FullName).Hash.ToLower()
        Write-Host ("  {0}  {1}" -f $hash, $artifact.Name)
        # Two spaces between hash and name: the format sha256sum reads.
        "$hash  $($artifact.Name)"
    }

    # UTF-8 without a BOM and with LF endings, so `sha256sum -c` on any
    # platform reads the file the maintainer generated on Windows.
    $text = ($lines -join "`n") + "`n"
    [System.IO.File]::WriteAllText($output, $text, [System.Text.UTF8Encoding]::new($false))

    Write-Host ''
    Write-Host "Wrote $output" -ForegroundColor Green
    Write-Host ''
    Write-Host 'Verify a download with either of:'
    Write-Host '  certutil -hashfile <file> SHA256'
    Write-Host '  sha256sum --check SHA256SUMS.txt'
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
