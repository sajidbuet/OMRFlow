# SMB (network-share) qualification

> **Status for `0.1.1-alpha.0`: NOT PERFORMED — REAL INFRASTRUCTURE REQUIRED.**
>
> `ACCEPTANCE_CRITERIA.md` §6 requires intake from genuine SMB shares written
> by processes on *other* Windows machines. The machine revised phase 10 ran
> on (2026-10-07) is a single laptop on a public Wi-Fi network with no mapped
> drives, no configured UNC sources and no second machine, so the
> qualification could not be run. The tooling below is complete and was
> exercised end to end on local folders (a **rehearsal**), which is tooling
> evidence only and is never reported as SMB. §8 gate 3 stays open until a run
> on real infrastructure reports **`PASS`**.

## What the qualification proves

That OMRFlow's continuous intake, registration, deduplication, restart and
crash-safety behave on a **real network share** as they were qualified to on
local disks (Phase 9, `docs/release/validation/0.1.1-alpha.0-phase9-intake/`):

- every completely written file on the share is discovered exactly once;
- no file is registered or recognised before its writer finished - judged by
  content (the SHA-256 OMRFlow registered must be the *completed* file's), and
  by time on a clock corrected for the offset between the machines;
- a stable file is never duplicated; same names at two scanners stay distinct;
- provenance (source, path, name) is the share's;
- a share that goes away is marked **unreachable** - nothing is marked
  vanished, the other source keeps going - and its files written meanwhile are
  found when it returns;
- OMRFlow killed during intake restarts into the same session, keeps every
  committed sheet, re-reads none of them, and finds the files written while it
  was down;
- no `database is locked` error; SQLite integrity and Project Health clean;
- the project database is on the OMRFlow machine's **local fixed disk**.

It also measures, per source, the **listing cost of one reconciliation** and
the **completion-to-ready (stabilisation) latency**, which is the evidence for
or against the network stability defaults (`NETWORK_POLICY`: quiet 15 s, poll
30 s, two observations - *"not measured on a real share; phase 10's SMB
qualification settles them"*).

## What it does not prove

Real scanners or paper (the images are synthetic, written by a script),
power-loss durability (the restart is a process termination), the GUI on a
share (OMRFlow runs headless as the qualification coordinator), SMB servers,
NAS devices, VPNs or links other than the ones the report describes, and the
quality policy's calibration.

## Topology

```text
  scanner PC A (Windows)                    OMRFlow machine (Windows)
  ---------------------                     -------------------------
  Invoke-OmrflowScannerWriter.ps1  writes   supervisor + OMRFlow (coordinator)
     D:\Scans\omr            ----share---->  watches \\SCANNER-PC-A\scans\omr
     D:\Scans\omrflow-smb-logs  ---share-->  reads the writer's evidence log
                                            project database on C:\ (local disk)
  scanner PC B (Windows) - the same, \\SCANNER-PC-B\...
```

- At least **two Windows machines**; better, two scanner PCs and the OMRFlow
  machine (two genuine sources). Two shares on *one* remote machine also
  satisfy §6 (two source streams written remotely) but test less.
- The writer runs **on the scanner PC** and writes into its own shared folder,
  as a scanner workstation saves its scans. A file copied onto a share from the
  OMRFlow machine is not this test.
- The writer's log folder is a **sibling** of the scanner folder, never inside
  it (OMRFlow would see it as a scan), shared so the OMRFlow machine can read it
  live and stamp a clock probe into it.
- The scanner PCs need only Windows PowerShell 5.1 (built in) - no Python, no
  OMRFlow.

The evaluator decides genuineness **from facts**, not from intentions: a source
counts only if it is a UNC path whose host is not the OMRFlow machine (by DNS
name, NetBIOS name, `localhost`, loopback or any of its own addresses), the
writer's own log names a host that is not the OMRFlow machine, and the
database's drive is a fixed local disk. A junction, `subst` drive, mapped drive
letter or `\\localhost\...` share is never SMB.

## Procedure

Everything is driven by `python -m omr_scanner.tools.smb_qualification`
(source build, from the repository root). OMRFlow itself can run from source
or from the installed build (`--coordinator-exe`).

### 1. Prepare the machines

1. On each scanner PC create `D:\Scans\omr` and `D:\Scans\omrflow-smb-logs`
   (any paths), share them, and give the OMRFlow machine's account read access
   to the first and read/write to the second.
2. From the OMRFlow machine check both list: `Get-ChildItem \\SCANNER-PC-A\scans\omr`.
3. Synchronise the clocks (`w32tm /resync`) and record
   `w32tm /stripchart /computer:SCANNER-PC-A /samples:5 /dataonly`. The run also
   measures the offset itself through the share.
4. Optional, elevated: `Get-SmbConnection` on the OMRFlow machine during the
   run records the SMB dialect; without elevation the report says it was not
   discoverable.

### 2. Prepare the campaign (OMRFlow machine)

```powershell
.venv\Scripts\python.exe -m omr_scanner.tools.smb_qualification prepare `
    --output Scratch\Qualification\smb `
    --source "A=\\SCANNER-PC-A\scans\omr" `
    --log "A=\\SCANNER-PC-A\scans\omrflow-smb-logs\writer_A.jsonl" `
    --writer-target "A=D:\Scans\omr" `
    --writer-log-local "A=D:\Scans\omrflow-smb-logs\writer_A.jsonl" `
    --source "B=\\SCANNER-PC-B\scans\omr" `
    --log "B=\\SCANNER-PC-B\scans\omrflow-smb-logs\writer_B.jsonl" `
    --writer-target "B=D:\Scans\omr" `
    --writer-log-local "B=D:\Scans\omrflow-smb-logs\writer_B.jsonl"
```

This plans the Phase 9 cohort for two scanners, grown until **each source gets
at least 1,000 files** (`--files-per-source`), renders the images, and writes
one package per scanner PC (`packages\writer_A`, `packages\writer_B`: the
schedule, only that scanner's images, the writer script, and the exact command
in `RUN-ON-SCANNER-PC.txt`) and `OPERATOR_STEPS.md` with the campaign's own
paths. The workload is the Phase 9 one: normal writes, stepped growth, a pause
longer than the quiet period, header first, held open, `.part` + rename, names
written at both scanners, byte copies, continuous random arrival.

Copy each package folder to its scanner PC.

### 3. Run

```powershell
.venv\Scripts\python.exe -m omr_scanner.tools.smb_qualification run --campaign <campaign folder>
# OMRFlow from the installed build instead of source:
#   ... run --campaign <folder> --coordinator-exe "$env:LOCALAPPDATA\Programs\OMRFlow\OMRFlow.exe"
```

The supervisor creates the project **on the local disk** with the two UNC
sources (production `NETWORK_POLICY`), starts OMRFlow, and prints:

- **START THE WRITERS NOW** - on each scanner PC run the command from its
  `RUN-ON-SCANNER-PC.txt`
  (`powershell -NoProfile -ExecutionPolicy Bypass -File .\Invoke-OmrflowScannerWriter.ps1 -Package . -Target ... -Log ...`).
  It waits (up to two hours) until both writers have logged `writer_started`.
- At about **35 %** of the files written, with a sheet in a recognition
  worker, it **kills OMRFlow** (`TerminateProcess`), waits for ten more files
  written while it is down, and restarts it into the same session.
- At about **60 %**: **CHECKPOINT: TAKE SHARE ... AWAY NOW**. Make scanner B's
  share unreachable from the OMRFlow machine **without stopping its writer**:
  disable the scanner PC's network adapter, unplug its cable, or
  `Stop-Service LanmanServer -Force` on it. The supervisor waits until it
  *observes* the share gone - it cannot list it, and the project marks the
  source unreachable - keeps it away at least two minutes, then prints
  **BRING SHARE ... BACK NOW** and waits until it observes it back (here and in
  the project). Nothing is assumed from the clock. `CHECKPOINT.txt` in the run
  folder holds the current instruction.
- It waits for both writers to finish and every file to be read, stops
  OMRFlow cleanly, and writes `smb\report.json` / `smb\report.md`.

Exit codes: `0` PASS; `1` FAIL; `3` completed but **NOT SMB QUALIFICATION**
(not genuine SMB or below scale); `2` bad arguments.

### 4. Record

Commit the compact report (never images or projects) as
`docs/release/validation/0.1.1-alpha.0-smb/` (`report.json`, `report.md`, the
two `writer_started` descriptions, the `w32tm` output, a short `README.md`
with the machine descriptions and the disconnect mechanism), then set §8 gate
3 in `docs/release/validation/0.1.1-alpha.0-release-gate/release-gate.json`
to the report's verdict and run `python -m tools.release_validation.release_gate`.

## The assertions

All fifteen are always emitted; a rehearsal or a run below scale can never
`PASS`.

| Assertion | Requirement |
|---|---|
| `genuine_smb_topology` | ≥ 2 sources, each a UNC share on another host; each writer's own log names a host that is not the OMRFlow machine |
| `database_on_local_disk` | the project database is on a fixed local drive |
| `scale_and_workload` | ≥ 1,000 files per source; every partial-write pattern used; a name written at both sources; one restart; one outage |
| `completed_files_discovered_exactly_once` | every completed file ↔ one ledger row with the completed file's hash; nothing unplanned; no row ever seen *vanished* |
| `no_partial_file_processed` | registered and recognised bytes are the completed file's; registration and submission after completion (clock-corrected, with the measured allowance) |
| `no_stable_file_duplicated` | every planted byte copy, and nothing else, is `duplicate_content` (Phase 9 rule) |
| `provenance_correct` | ledger path and name are the share's; sheet ↔ ledger ↔ source links hold; shown names are arrival names |
| `same_filenames_independent` | same-named files at the two sources are distinct and processed (Phase 9 rule) |
| `outage_marked_unreachable` | the project marked the source unreachable; nothing vanished; the other source kept listing and registering |
| `reconnection_recovers_files` | files written during the outage are registered after it |
| `restart_same_session_and_offline_arrivals` | one session; recovery changed nothing committed; a kill landed with a sheet in a worker; files written while down were found |
| `no_completed_scan_rerecognised` | no committed sheet submitted again after the restart (Phase 9 rule) |
| `no_database_lock_errors` | no `database is locked` in the coordinator's log |
| `sqlite_integrity` | `quick_check` / `integrity_check` ok, `foreign_key_check` empty, after the kill and at the end |
| `project_health` | no error- or critical-level Project Health issue |

## Rehearsal (tooling check only)

```powershell
.venv\Scripts\python.exe -m omr_scanner.tools.smb_qualification rehearse `
    --output Scratch\Qualification\smb-rehearsal --files-per-source 40 --fast-stability
```

The same procedure on this machine: local folders behind folder links, the
PowerShell writer run locally, the outage made by removing a link. Its verdict
is always **`NOT SMB QUALIFICATION`**. Run in revised phase 10 (2026-10-07,
source build, `1e7d493`): two writers (50 + 81 files), one real kill with a
sheet in a worker and restart, a 20 s simulated outage; every assertion that can
run on one machine `PASS`; `genuine_smb_topology` and `scale_and_workload`
*not exercised*. Also a `stress`-marked test
(`tests/integration/test_smb_rehearsal.py`). This says the tooling works; it
says nothing about SMB.

## Files

| | |
|---|---|
| `scripts/smb/Invoke-OmrflowScannerWriter.ps1` | the scanner-PC writer (PowerShell 5.1 / 7) |
| `src/omr_scanner/evaluation/smb_qualification/` | prepare, run, evaluate, report |
| `src/omr_scanner/tools/smb_qualification.py` | the command line |
| `tests/unit/test_smb_qualification.py` | topology, verdict, clock, packages, the writer on local folders |
| `tests/integration/test_smb_rehearsal.py` | the rehearsal end to end (`stress`) |
