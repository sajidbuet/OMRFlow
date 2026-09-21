<#
.SYNOPSIS
    Synchronise docs/wiki/ to the GitHub Wiki repository.

.DESCRIPTION
    A GitHub Wiki is a separate Git repository (`<repo>.wiki.git`). The
    canonical source for OMRFlow's user documentation is `docs/wiki/` in the
    main repository, so that it is reviewed with the code it describes and
    remains usable when the Wiki is unavailable. This script copies it over.

    It does not push unless you ask it to. By default it reports what would
    change and stops, because publishing documentation is a visible action
    and should be a deliberate one.

.PARAMETER WikiPath
    Working copy of the wiki repository. Cloned into a temporary directory if
    omitted.

.PARAMETER Push
    Actually commit and push. Without this the script is a dry run.

.PARAMETER Message
    Commit message. Defaults to one naming the source commit, so a wiki page
    can be traced back to the revision it was generated from.

.EXAMPLE
    .\scripts\release\Publish-Wiki.ps1
    # Dry run: shows what would change.

.EXAMPLE
    .\scripts\release\Publish-Wiki.ps1 -Push
#>
[CmdletBinding()]
param(
    [string] $WikiPath,
    [switch] $Push,
    [string] $Message
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$source = Join-Path $repositoryRoot 'docs\wiki'
$wikiRemote = 'https://github.com/sajidbuet/OMRflow.wiki.git'

$temporary = $null
try {
    if (-not (Test-Path $source)) { throw "No documentation source at $source." }

    $pages = Get-ChildItem $source -Filter '*.md' -File
    Write-Host "Source: $source ($($pages.Count) pages)" -ForegroundColor Cyan

    if (-not $WikiPath) {
        $temporary = Join-Path ([System.IO.Path]::GetTempPath()) "omrflow-wiki-$(Get-Random)"
        Write-Host "Cloning the wiki into $temporary ..."
        & git clone --quiet $wikiRemote $temporary
        if ($LASTEXITCODE -ne 0) {
            throw @"
Could not clone $wikiRemote.

A GitHub Wiki repository does not exist until the Wiki has been enabled and
at least one page created through the web interface. Do that once
(Settings -> Features -> Wikis, then create the Home page), and this script
will work from then on.

The documentation in docs/wiki/ is complete and readable as it is; wiki
publishing is a convenience, not a dependency.
"@
        }
        $WikiPath = $temporary
    }

    Write-Host "Target: $WikiPath"

    # Remove the pages that are no longer in the source, so a deleted page
    # does not linger on the wiki, but leave anything that is not ours.
    $sourceNames = $pages | ForEach-Object { $_.Name }
    $stale = Get-ChildItem $WikiPath -Filter '*.md' -File |
        Where-Object { $_.Name -notin $sourceNames }

    $changed = [System.Collections.Generic.List[string]]::new()
    foreach ($page in $pages) {
        $destination = Join-Path $WikiPath $page.Name
        $isNew = -not (Test-Path $destination)
        $differs = $false
        if (-not $isNew) {
            $differs = (Get-FileHash $page.FullName).Hash -ne (Get-FileHash $destination).Hash
        }
        if ($isNew -or $differs) {
            $changed.Add("$(if ($isNew) { 'new    ' } else { 'changed' })  $($page.Name)")
            if ($Push) { Copy-Item $page.FullName $destination -Force }
        }
    }
    foreach ($page in $stale) {
        $changed.Add("removed  $($page.Name)")
        if ($Push) { Remove-Item $page.FullName -Force }
    }

    Write-Host ''
    if ($changed.Count -eq 0) {
        Write-Host 'The wiki is already up to date.' -ForegroundColor Green
        exit 0
    }
    foreach ($line in $changed) { Write-Host "  $line" }
    Write-Host ''

    if (-not $Push) {
        Write-Host "$($changed.Count) page(s) would change. Nothing was written." -ForegroundColor Yellow
        Write-Host 'Re-run with -Push to publish.'
        exit 0
    }

    if (-not $Message) {
        $commit = (& git -C $repositoryRoot rev-parse --short HEAD 2>$null)
        $Message = if ($LASTEXITCODE -eq 0 -and $commit) {
            "Sync documentation from docs/wiki/ at $($commit.Trim())"
        } else {
            'Sync documentation from docs/wiki/'
        }
    }

    & git -C $WikiPath add --all
    & git -C $WikiPath commit -m $Message
    if ($LASTEXITCODE -ne 0) { throw "Nothing committed (exit $LASTEXITCODE)." }
    & git -C $WikiPath push
    if ($LASTEXITCODE -ne 0) { throw "Push failed (exit $LASTEXITCODE)." }

    Write-Host ''
    Write-Host "Published $($changed.Count) page(s)." -ForegroundColor Green
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
finally {
    if ($temporary -and (Test-Path $temporary)) {
        Remove-Item -Recurse -Force $temporary -ErrorAction SilentlyContinue
    }
}
