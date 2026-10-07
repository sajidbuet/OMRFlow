# SMB qualification - p9-custom-20261007T121328Z-20261007

**Verdict: `NOT SMB QUALIFICATION`** (mode `rehearsal`)

> This run is **not** SMB qualification evidence: it was a rehearsal on local folders or below the required scale. It shows only that the tooling works.

## Machines and topology

- OMRFlow machine: `Sajid-Asus-Laptop` - Windows-11-10.0.26200-SP0, 16 logical CPUs
- Scanner A writer: host `SAJID-ASUS-LAPT` - Microsoft Windows 11 Education Microsoft Windows NT 10.0.26200.0 (build 26200), PowerShell 5.1.26100.9444 Desktop; target `C:\Research\OMRflow-p10\Scratch\phase10\smb-rehearsal-final\rehearsal-20261007T181327\rehearsal\disk\Scanner_A`
- Scanner B writer: host `SAJID-ASUS-LAPT` - Microsoft Windows 11 Education Microsoft Windows NT 10.0.26200.0 (build 26200), PowerShell 5.1.26100.9444 Desktop; target `C:\Research\OMRflow-p10\Scratch\phase10\smb-rehearsal-final\rehearsal-20261007T181327\rehearsal\disk\Scanner_B`
- Source A: `C:\Research\OMRflow-p10\Scratch\phase10\smb-rehearsal-final\rehearsal-20261007T181327\rehearsal\share\Scanner_A` - NOT SMB (not a UNC path (a local folder, junction, subst or mapped drive))
- Source B: `C:\Research\OMRflow-p10\Scratch\phase10\smb-rehearsal-final\rehearsal-20261007T181327\rehearsal\share\Scanner_B` - NOT SMB (not a UNC path (a local folder, junction, subst or mapped drive))
- Project database: `C:\Research\OMRflow-p10\Scratch\phase10\smb-rehearsal-final\rehearsal-20261007T181327\p9-custom-20261007T121328Z-20261007\rehearsal\workspace\SMB qualification rehearsal\database.sqlite` - drive type fixed (local)
- SMB dialect: not discoverable here (Get-SmbConnection : Access is denied. 
At line:1 char:1
+ Get-SmbConnection -ErrorAction Stop | Select-Object ServerName, Share ...
+ ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : PermissionDenied: (MSFT_SMBConnection:ROOT/Microsoft/...T_SMBConnection) [Get-SmbConnect 
   ion], CimException
    + FullyQualifiedErrorId : Windows System Error 5,Get-SmbConnection)
- Coordinator: source

## Assertions

| Assertion | Status | Checked | Failures |
|---|---|---:|---|
| `genuine_smb_topology` | NOT_EXERCISED | 0 |  |
| `database_on_local_disk` | PASS | 1 |  |
| `scale_and_workload` | NOT_EXERCISED | 0 |  |
| `completed_files_discovered_exactly_once` | PASS | 131 |  |
| `no_partial_file_processed` | PASS | 127 |  |
| `no_stable_file_duplicated` | PASS | 4 |  |
| `provenance_correct` | PASS | 127 |  |
| `same_filenames_independent` | PASS | 100 |  |
| `outage_marked_unreachable` | PASS | 1 |  |
| `reconnection_recovers_files` | PASS | 26 |  |
| `restart_same_session_and_offline_arrivals` | PASS | 1 |  |
| `no_completed_scan_rerecognised` | PASS | 33 |  |
| `no_database_lock_errors` | PASS | 698 |  |
| `sqlite_integrity` | PASS | 2 |  |
| `project_health` | PASS | 2 |  |

## Measurements

Listing cost per reconciliation (seconds):

- A: n 108, min 0.0002, median 0.0004, p95 0.001, max 0.002; files listed n 108, min 0.0, median 25.0, p95 50.0, max 50.0; listing errors 0
- B: n 89, min 0.0002, median 0.0005, p95 0.0011, max 0.0015; files listed n 89, min 0.0, median 26.0, p95 81.0, max 81.0; listing errors 266

Stabilisation latency, writer completion to ready (seconds, on this machine's clock):

- all: n 101, min 2.1555, median 2.7507, p95 8.3249, max 23.4297
- A: n 49, min 2.1651, median 2.8241, p95 3.116, max 6.6807
- B: n 52, min 2.1555, median 2.7416, p95 8.3421, max 23.4297

- Outage of Scanner B: rehearsal: the local folder link was removed (NOT SMB); project marked it unreachable 1.017 s after it was gone; online again 1.027 s after it returned
- Restart: killed with 6 sheet(s) in flight and 33 committed; 3 file(s) written while down; restart sequence 0.234 s; recovery ok
- Clock offset, Scanner A host minus this machine: -0.0015 s +/- 0.0125 s
- Clock offset, Scanner B host minus this machine: -0.0018 s +/- 0.0123 s

## What this does not prove

- real scanner hardware or real paper (the images are synthetic, written by a script)
- power-loss durability (the restart is a process termination)
- the installed build's GUI on a share (the coordinator is headless)
- other SMB servers, NAS devices, VPNs or Wi-Fi links than the ones described here
- real-paper calibration of the quality policy (the unvalidated default)
- production readiness

