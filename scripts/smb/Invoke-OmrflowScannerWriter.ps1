<#
.SYNOPSIS
    One simulated scanner for OMRFlow's SMB qualification: writes a prepared
    schedule of synthetic answer-sheet images into a folder, the way a scanner
    workstation saves its scans, and logs every file it writes.

.DESCRIPTION
    Run this on the REMOTE Windows machine (the "scanner PC"), never on the
    machine running OMRFlow. It needs nothing but Windows PowerShell 5.1 or
    PowerShell 7 - no Python, no OMRFlow.

    It is the PowerShell twin of the Phase 9 writer
    (omr_scanner.evaluation.intake_qualification.writer): the same schedule,
    the same write patterns and the same evidence events, so the evaluator on
    the OMRFlow machine can judge the run exactly as it judges a local one.

    Write patterns (from the schedule, per file):
      atomic        the whole file in one write
      stepped       appended in several steps, closed between them
      long_pause    stepped, with a pause longer than OMRFlow's quiet period
      header_first  the first 64 bytes (a PNG header), a pause, then the rest
      held_open     written in steps through ONE open handle, then held open
      rename        written under "<name>.part", then renamed into place

    Evidence: every file's write_started is logged before its first byte and
    its write_completed - with the final size and SHA-256 - after its last
    byte and close (or rename). Each line is flushed to disk before the next
    file begins. Times are this machine's clock (Unix seconds); the OMRFlow
    side measures the offset between the two clocks and records it.

    The log must NOT be inside -Target (OMRFlow would see it as a scan); put
    it in a sibling folder the OMRFlow machine can read over the network, so
    the supervisor there can follow the run.

.PARAMETER Package
    The writer package folder prepared on the OMRFlow machine
    (python -m omr_scanner.tools.smb_qualification prepare ...). It holds
    schedule.json and pool\*.png.

.PARAMETER Target
    The scanner folder to write into: the shared folder as a LOCAL path on
    this machine (e.g. D:\Scans\omr, shared as \\THIS-PC\scans\omr), or a UNC
    path on another server.

.PARAMETER Log
    The evidence log (JSON lines), outside -Target, e.g.
    D:\Scans\omrflow-smb-logs\writer_A.jsonl.

.PARAMETER StopFile
    Optional: when this file exists the writer stops after the current file.

.PARAMETER StartDelaySeconds
    Seconds between starting and the schedule's time zero (default 5).

.PARAMETER TimeScale
    Multiplies the schedule's arrival offsets (not the write pauses);
    1.0 = as planned.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\Invoke-OmrflowScannerWriter.ps1 `
        -Package . -Target D:\Scans\omr -Log D:\Scans\omrflow-smb-logs\writer_A.jsonl
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Package,
    [Parameter(Mandatory = $true)] [string] $Target,
    [Parameter(Mandatory = $true)] [string] $Log,
    [string] $StopFile = '',
    [double] $StartDelaySeconds = 5.0,
    [double] $TimeScale = 1.0
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$HeaderBytes = 64
$Utf8 = New-Object System.Text.UTF8Encoding($false)

function Get-UnixSeconds {
    # Ticks since 1970-01-01T00:00:00Z / 1e7: 100 ns resolution, no rounding to ms.
    return ([DateTime]::UtcNow.Ticks - 621355968000000000) / 10000000.0
}

$schedulePath = Join-Path $Package 'schedule.json'
if (-not (Test-Path -LiteralPath $schedulePath)) { throw "No schedule.json in $Package" }
$schedule = Get-Content -LiteralPath $schedulePath -Raw -Encoding UTF8 | ConvertFrom-Json
$campaign = [string] $schedule.campaign
$source = [string] $schedule.source

$targetFull = [IO.Path]::GetFullPath($Target)
$logFull = [IO.Path]::GetFullPath($Log)
if ($logFull.StartsWith($targetFull.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "The log ($logFull) must not be inside the scanner folder ($targetFull)."
}
[void] [IO.Directory]::CreateDirectory($targetFull)
[void] [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($logFull))

$logStream = New-Object System.IO.FileStream($logFull, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)

function Write-Evidence([string] $Event, [hashtable] $Fields) {
    $record = [ordered]@{
        event    = $Event
        campaign = $campaign
        role     = "writer:$source"
        pid      = $PID
        t        = (Get-UnixSeconds)
    }
    foreach ($key in $Fields.Keys) { $record[$key] = $Fields[$key] }
    $line = (ConvertTo-Json -InputObject $record -Compress -Depth 5) + "`n"
    $bytes = $Utf8.GetBytes($line)
    $logStream.Write($bytes, 0, $bytes.Length)
    $logStream.Flush($true)   # through to the disk before the next file begins
}

function Test-Stop {
    return ($StopFile -and (Test-Path -LiteralPath $StopFile))
}

function Wait-Seconds([double] $Seconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(0.0, $Seconds))
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Stop) { return }
        $left = ($deadline - [DateTime]::UtcNow).TotalMilliseconds
        Start-Sleep -Milliseconds ([int] [Math]::Max(1, [Math]::Min(50, $left)))
    }
}

function Split-Chunks([byte[]] $Data, [int] $Count) {
    $Count = [Math]::Max(1, $Count)
    $size = [Math]::Max(1, [int] [Math]::Ceiling($Data.Length / [double] $Count))
    $chunks = New-Object System.Collections.Generic.List[byte[]]
    for ($offset = 0; $offset -lt $Data.Length; $offset += $size) {
        $length = [Math]::Min($size, $Data.Length - $offset)
        $chunk = New-Object byte[] $length
        [Array]::Copy($Data, $offset, $chunk, 0, $length)
        $chunks.Add($chunk)
    }
    if ($chunks.Count -eq 0) { $chunks.Add((New-Object byte[] 0)) }
    return ,$chunks
}

function Write-Part([string] $Path, [byte[]] $Bytes, [bool] $Append) {
    $mode = if ($Append) { [IO.FileMode]::Append } else { [IO.FileMode]::Create }
    $stream = New-Object System.IO.FileStream($Path, $mode, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    try { $stream.Write($Bytes, 0, $Bytes.Length) } finally { $stream.Dispose() }
}

function Write-ScheduledFile([string] $Path, [byte[]] $Data, $Item) {
    $pattern = [string] $Item.pattern
    $pauses = @()
    if ($Item.pauses) { $pauses = @($Item.pauses | ForEach-Object { [double] $_ }) }
    $holdAfter = 0.0
    if ($Item.PSObject.Properties['hold_after']) { $holdAfter = [double] $Item.hold_after }
    switch ($pattern) {
        'atomic' {
            Write-Part $Path $Data $false
        }
        { $_ -in @('stepped', 'long_pause') } {
            $parts = Split-Chunks $Data ($pauses.Count + 1)
            for ($i = 0; $i -lt $parts.Count; $i++) {
                Write-Part $Path $parts[$i] ($i -gt 0)
                if ($i -lt $pauses.Count) { Wait-Seconds $pauses[$i] }
            }
        }
        'header_first' {
            $head = [Math]::Min($HeaderBytes, $Data.Length)
            $first = New-Object byte[] $head
            [Array]::Copy($Data, 0, $first, 0, $head)
            $rest = New-Object byte[] ($Data.Length - $head)
            [Array]::Copy($Data, $head, $rest, 0, $rest.Length)
            Write-Part $Path $first $false
            $pause = 0.5
            if ($pauses.Count -gt 0) { $pause = $pauses[0] }
            Wait-Seconds $pause
            Write-Part $Path $rest $true
        }
        'held_open' {
            $parts = Split-Chunks $Data ($pauses.Count + 1)
            $stream = New-Object System.IO.FileStream($Path, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::Read)
            try {
                for ($i = 0; $i -lt $parts.Count; $i++) {
                    $stream.Write($parts[$i], 0, $parts[$i].Length)
                    $stream.Flush()
                    if ($i -lt $pauses.Count) { Wait-Seconds $pauses[$i] }
                }
                Wait-Seconds $holdAfter
            }
            finally { $stream.Dispose() }
        }
        'rename' {
            $temporary = "$Path.part"
            $parts = Split-Chunks $Data ($pauses.Count + 1)
            for ($i = 0; $i -lt $parts.Count; $i++) {
                Write-Part $temporary $parts[$i] ($i -gt 0)
                if ($i -lt $pauses.Count) { Wait-Seconds $pauses[$i] }
            }
            if (Test-Path -LiteralPath $Path) { [IO.File]::Delete($Path) }
            [IO.File]::Move($temporary, $Path)
        }
        default { throw "unknown write pattern '$pattern'" }
    }
}

$sha = [Security.Cryptography.SHA256]::Create()
$exitCode = 0
try {
    $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction SilentlyContinue
    Write-Evidence 'writer_started' @{
        source             = $source
        files              = @($schedule.arrivals).Count
        target             = $targetFull
        target_is_unc      = $targetFull.StartsWith('\\')
        host               = $env:COMPUTERNAME
        user               = $env:USERNAME
        os_caption         = if ($os) { [string] $os.Caption } else { '' }
        os_version         = [Environment]::OSVersion.VersionString
        os_build           = if ($os) { [string] $os.BuildNumber } else { '' }
        powershell         = $PSVersionTable.PSVersion.ToString()
        powershell_edition = if ($PSVersionTable.PSObject.Properties['PSEdition']) { [string] $PSVersionTable.PSEdition } else { 'Desktop' }
        writer             = 'Invoke-OmrflowScannerWriter.ps1'
        time_scale         = $TimeScale
    }
    $origin = (Get-UnixSeconds) + $StartDelaySeconds
    $written = 0
    foreach ($item in @($schedule.arrivals)) {
        $wait = $origin + ([double] $item.at) * $TimeScale - (Get-UnixSeconds)
        if ($wait -gt 0) { Wait-Seconds $wait }
        if (Test-Stop) {
            Write-Evidence 'writer_stopped' @{ reason = 'stop requested'; written = $written }
            $exitCode = 3
            break
        }
        $data = [IO.File]::ReadAllBytes((Join-Path $Package ([string] $item.pool)))
        $digest = -join ($sha.ComputeHash($data) | ForEach-Object { $_.ToString('x2') })
        if ($digest -ne [string] $item.sha256) {
            Write-Evidence 'writer_error' @{ seq = $item.seq; error = 'pool bytes changed' }
            $exitCode = 2
            break
        }
        $path = Join-Path $targetFull ([string] $item.name)
        Write-Evidence 'write_started' @{
            seq = $item.seq; source = $source; name = [string] $item.name; path = $path
            pattern = [string] $item.pattern; content = [string] $item.content
        }
        $started = Get-UnixSeconds
        Write-ScheduledFile $path $data $item
        $completed = Get-UnixSeconds
        Write-Evidence 'write_completed' @{
            seq = $item.seq; source = $source; name = [string] $item.name; path = $path
            pattern = [string] $item.pattern; content = [string] $item.content
            size = $data.Length; sha256 = $digest; created_at = $started
            write_started_at = $started; write_completed_at = $completed
        }
        $written++
    }
    if ($exitCode -eq 0) {
        Write-Evidence 'writer_finished' @{ source = $source; written = $written }
    }
}
catch {
    Write-Evidence 'writer_error' @{ error = ($_ | Out-String).Trim() }
    $exitCode = 1
}
finally {
    $sha.Dispose()
    $logStream.Dispose()
}
exit $exitCode
