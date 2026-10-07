# Intake qualification - p9-release-20261005T222509Z-20261006

**Verdict: FAILED**

Mode `release` · commit `e6c1e799718ae9c6cde9ef51ae0e3244a6fd1741` (working tree clean: True) · schema 17 · OMRFlow 0.1.1-alpha.0

## What this proves

Automated source-build behaviour under a large synthetic multi-source intake campaign with controlled failures and exact ground truth: separate scanner writer processes on local folders, real forced termination of the OMRFlow coordinator, restarts into the same project and session, a scripted operator using the production services, closure through the authoritative finish policy, and Results / report cell values compared with an independent reference.

It does **not** prove: genuine SMB / network-share behaviour (the source outage is a local folder link removed and restored); real scanner behaviour (images are synthetic, written by processes); actual power-loss durability (process termination is not power removal); real-paper quality-policy calibration (the quality policy is the unvalidated default); installed-build behaviour (this runs from source); recognition accuracy on real scans; production readiness.

## Required assertions

| Assertion | Status | Evidence |
|---|---|---|
| `stable_files_discovered_exactly_once` | PASS | checked 10,189 (min 10,189), written_files 10189, vanished_seen 0, manual_source_rows 1, ledger_rows 10413, temporary_names_ignored 223 |
| `no_incomplete_file_processed` | PASS | checked 9,989 (min 9,984), partial_writes 7082, min_seconds_completion_to_submission 10.32, median_seconds_completion_to_submission 40.071 |
| `source_provenance_retained` | PASS | checked 9,989 (min 1), registered_files_checked 9989, original_names_checked 10189, reprocessed_sheets_traced 1 |
| `duplicate_content_identified` | PASS | checked 200 (min 1), planted_copies 200, within_source 100, across_sources 100, same_name_across_sources 3, flagged 200 |
| `independent_filenames_do_not_collide` | PASS | checked 9,726 (min 2), names_on_several_sources 3459, files_checked 9726, independent_registered 9353 |
| `batches_finite` | PASS | checked 678 (min 1), batches 678, sealed 678, watched_sources_with_units 4, digests_rechecked 676 |
| `no_accepted_image_lost` | PASS | checked 9,933 (min 1), expected_effective 9933, actual_effective 9933, kills_checked 9 |
| `no_completed_scan_rerecognised` | PASS | checked 34,848 (min 1), claimed_twice_in_one_incarnation 0 |
| `offline_arrivals_discovered` | PASS | checked 267 (min 1), written_while_coordinator_down 165, written_while_source_unreachable 102, down_intervals 9 |
| `conflict_counts_correct_as_population_grows` | PASS | checked 298 (min 1) |
| `rescan_relationships_survive_restart` | PASS | checked 56 (min 1), links 56, cross_source 34, chains 2, restarts_checked 9 |
| `aggregate_counts_consistent` | PASS | checked 8 (min 3), checkpoints 8 |
| `session_results_match_ground_truth` | PASS | checked 262,912 (min 1), cells_compared 201749, results_compared 262912 |
| `sqlite_integrity` | PASS | checked 12 (min 1), reports 12 |
| `application_invariants` | PASS | checked 12 (min 1), reports 12 |
| `finite_mode_regression` | PASS | checked 253,290 (min 1), finite_batches 2, finite_seconds 1768.0 |

## Crash matrix

| Case | Scale | Result | Evidence |
|---|---|---|---|
| case_01_clean_close_during_scan - Clean application close halfway through Scan | 8490 committed, 32 still to read (1 in a worker) | PASS | checked 1 (min 1) |
| case_02_forced_kill_during_scan - Forced process termination halfway through Scan | kill@1%: 106 committed / 0 in flight; kill@1%#2: 106 committed / 12 in flight; kill@25%: 2504 committed / 11 in flight; kill@50%: 5013 committed / 4 in fligh... | PASS | checked 5 (min 1) |
| case_03_sheet_interrupted_in_processing - Sheet interrupted during processing |  | PASS | checked 40 (min 1) |
| case_04_kill_after_commit - Kill after a recognition commit, before the next | 11 sheet(s) in the committed group, 3510 committed | PASS | checked 1 (min 1) |
| case_05_clean_close_during_resolve - Clean close halfway through Resolve | 1431 operator events committed, 35 conflicts open | PASS | checked 1 (min 1) |
| case_06_forced_kill_after_resolve_corrections - Forced kill after Resolve corrections | 17 decisions in this incarnation, 1004 operator audit events | PASS | checked 1 (min 1) |
| case_07_restart_retains_corrections - Restart retains resolved corrections |  | PASS | checked 452 (min 1) |
| case_08_unresolved_stay_unresolved - Unresolved items remain unresolved |  | PASS | checked 9 (min 1) |
| case_09_repeated_restarts - Repeated restarts create nothing twice |  | PASS | checked 9 (min 3) |
| case_10_results_after_interruption - Session results after interruption = uninterrupted |  | PASS | checked 9,997 (min 1) |
| case_11_resume_percentages - Resume at about 1 / 25 / 50 / 75 / 99 % | kills at [1, 25, 50, 75] % of 9989 sheets | FAIL | resume points missing: [99] |
| case_12_integrity_after_every_case - Integrity after every case |  | PASS | checked 9 (min 1) |
| case_13_kill_before_conflict_generation - Kill after recognition, before conflict generation | 6 sheet(s) committed, 2 duplicate conflict(s) created by recovery | PASS | checked 1 (min 1) |
| case_14_sealed_batch_interrupted - Sealed batch interrupted mid-processing |  | PASS | checked 4 (min 1) |
| case_15_resolve_reachable_after_reopen - Resolve reachable after reopen without Scan |  | PASS | checked 9 (min 1) |

## Endurance

| Case | Scale / duration | Result | Evidence |
|---|---|---|---|
| endurance_a_many_finite_batches - >= 10 sealed batches in one session, several sources | 678 sealed batches from 4 sources in one session | PASS | checked 678 (min 10) |
| endurance_b_continuous_random_intake - Continuous random intake with operator decisions | 10189 files over 13003 s (0.78/s); 375 operator decisions while files were still arriving | PASS | checked 10,189 (min 10,187), duration_seconds 13003.3, files 10189, max_gap_seconds 1574.45, median_gap_seconds 0.623, operator_decisions_during_intake 375 |
| endurance_c_supersession_reprocess - Reprocess All mid-session: nothing counted twice | Reprocess All of a 1-sheet unit while intake continued | PASS | checked 1 (min 1) |
| endurance_d_repeated_kill_restart - Repeated real kills and restarts | 8 real process kills, 9 restarts | PASS | checked 8 (min 3) |
| endurance_e_uninterrupted_control - Interrupted results = uninterrupted control |  | PASS | checked 9,997 (min 1) |

## Kill / restart sequence (interrupted run)

| # | Kind | Trigger | Committed at kill | In flight | Offline files | Restart s | Recovery | Integrity |
|---|---|---|---|---|---|---|---|---|
| 1 | forced | kill@1% | 106 | 0 | 10 | 0.078 | ok | clean |
| 2 | forced | kill@1%#2 | 106 | 12 | 10 | 0.109 | ok | clean |
| 3 | syncing_duplicates | syncing_duplicates@15% | 1,588 | 1 | 10 | 0.281 | ok | clean |
| 4 | forced | kill@25% | 2,504 | 11 | 10 | 0.297 | ok | clean |
| 5 | after_commit | after_commit@35% | 3,510 | 0 | 11 | 0.437 | ok | clean |
| 6 | forced | kill@50% | 5,013 | 4 | 11 | 0.515 | ok | clean |
| 7 | operator | operator@60% | 6,020 | 0 | 20 | 0.344 | ok | clean |
| 8 | forced | kill@75% | 7,511 | 13 | 21 | 0.485 | ok | clean |
| 9 | clean_close | clean_close@85% | 8,490 | 0 | 14 | 0.531 | ok | clean |

Checkpoints: checkpoint@1% (102 committed, ok), checkpoint@10% (1,000 committed, ok), checkpoint@25% (2,504 committed, ok), checkpoint@50% (4,997 committed, ok), checkpoint@75% (7,500 committed, ok), checkpoint@90% (8,995 committed, ok), caught_up@100% (9,988 committed, ok)

## Workload

- sources: 3
- arrivals written: 10189
- arrivals planned: 10189
- unique contents: 9988
- byte copy arrivals: 200
- sets: 4
- candidates on lists: 10240
- partial writes: 7082
- held open writes: 1518
- renamed writes: 1488
- long pause writes: 507
- header first writes: 1006
- cross source same name: 3459
- source outages: 1
- kills: 8
- clean closes: 1
- restarts: 9
- offline arrivals: 165
- duplicate id conflicts: 168
- operator corrections: 132
- duplicate acceptances: 64
- quality suggestions: 64
- confirmed rejections: 56
- dismissed suggestions: 10
- confirmed replacements: 56
- cross source replacements: 34
- replacement chains: 2
- superseded batches: 1
- sealed batches: 678
- operator decisions: 386
- real images: True

## Ground truth (planned)

- arrivals written: 10188
- late arrivals: 1
- unique contents: 9988
- byte copy arrivals: 200
- registered sheets: 9989
- superseded sheets: 56
- effective sheets: 9933
- effective scripts: 9923
- blank pages: 10
- candidates on lists: 10240
- scored candidates: 9881

## Runs, timing and resources

- **interrupted**: 10,189 files over 13003.3 s (0.784/s); recognition 0.768 sheets/s; run 13484.6 s; peak tree RSS 1031 MB; peak processes 8; max in flight 16; DB 131 MB; logged lock errors 0
- **control**: 10,189 files over 12757.8 s (0.799/s); recognition 0.783 sheets/s; run 13258.1 s; peak tree RSS 1099 MB; peak processes 8; max in flight 16; DB 131 MB; logged lock errors 0
- **finite control**: 1768.0 s (221.0 s recognition), batches 2
- **golden one-batch regression**: match (tests\fixtures\golden_one_batch\golden.json)

## Environment

- platform: Windows-11-10.0.26200-SP0
- windows: ['11', '10.0.26200', 'SP0', 'Multiprocessor Free']
- python: 3.12.7 | packaged by Anaconda, Inc. | (main, Oct  4 2024, 13:17:27) [MSC v.1929 64 bit (AMD64)]
- processor: AMD64 Family 25 Model 80 Stepping 0, AuthenticAMD
- cpu_logical: 16
- cpu_physical: 8
- ram_bytes: 33721307136
- sqlite: 3.45.3
- seeds: cohort 20261006, timing 6102026
- workers 6, unit size 200, trickle 30.0 s, in flight 16, stability {'min_observations': 2, 'quiet_seconds': 5.0, 'max_decode_attempts': 3, 'retry_backoff_seconds': 5.0, 'poll_interval_seconds': 10.0}

