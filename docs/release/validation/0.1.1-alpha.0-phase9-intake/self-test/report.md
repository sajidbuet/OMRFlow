# Intake qualification - p9-self_test-20261006T100610Z-20261006

**Verdict: ALL RUNS PASSED — NOT THE RELEASE QUALIFICATION**

Mode `self_test` · commit `542b2afef85888094a8497ba83147a41db80aef6` (working tree clean: True) · schema 17 · OMRFlow 0.1.1-alpha.0

Not the release qualification because: 132 arrivals < 10,000; mode 'self_test' is not the release mode

## What this proves

Automated source-build behaviour under a large synthetic multi-source intake campaign with controlled failures and exact ground truth: separate scanner writer processes on local folders, real forced termination of the OMRFlow coordinator, restarts into the same project and session, a scripted operator using the production services, closure through the authoritative finish policy, and Results / report cell values compared with an independent reference.

It does **not** prove: genuine SMB / network-share behaviour (the source outage is a local folder link removed and restored); real scanner behaviour (images are synthetic, written by processes); actual power-loss durability (process termination is not power removal); real-paper quality-policy calibration (the quality policy is the unvalidated default); installed-build behaviour (this runs from source); recognition accuracy on real scans; production readiness.

## Required assertions

| Assertion | Status | Evidence |
|---|---|---|
| `stable_files_discovered_exactly_once` | PASS | checked 132 (min 132), written_files 132, vanished_seen 0, manual_source_rows 1, ledger_rows 137, temporary_names_ignored 4 |
| `no_incomplete_file_processed` | PASS | checked 128 (min 123), partial_writes 90, min_seconds_completion_to_submission 1.932, median_seconds_completion_to_submission 14.058 |
| `source_provenance_retained` | PASS | checked 128 (min 1), registered_files_checked 128, original_names_checked 132, reprocessed_sheets_traced 1 |
| `duplicate_content_identified` | PASS | checked 4 (min 1), planted_copies 4, within_source 2, across_sources 2, same_name_across_sources 0, flagged 4 |
| `independent_filenames_do_not_collide` | PASS | checked 125 (min 2), names_on_several_sources 44, files_checked 125, independent_registered 117 |
| `batches_finite` | PASS | checked 30 (min 1), batches 30, sealed 30, watched_sources_with_units 4, digests_rechecked 28 |
| `no_accepted_image_lost` | PASS | checked 120 (min 1), expected_effective 120, actual_effective 120, kills_checked 6 |
| `no_completed_scan_rerecognised` | PASS | checked 488 (min 1), claimed_twice_in_one_incarnation 0 |
| `offline_arrivals_discovered` | PASS | checked 44 (min 1), written_while_coordinator_down 32, written_while_source_unreachable 12, down_intervals 6 |
| `conflict_counts_correct_as_population_grows` | PASS | checked 62 (min 1) |
| `rescan_relationships_survive_restart` | PASS | checked 8 (min 1), links 8, cross_source 5, chains 2, restarts_checked 6 |
| `aggregate_counts_consistent` | PASS | checked 5 (min 3), checkpoints 5 |
| `session_results_match_ground_truth` | PASS | checked 3,591 (min 1), cells_compared 2869, results_compared 3591 |
| `sqlite_integrity` | PASS | checked 9 (min 1), reports 9 |
| `application_invariants` | PASS | checked 9 (min 1), reports 9 |
| `finite_mode_regression` | PASS | checked 4,033 (min 1), finite_batches 2, finite_seconds 37.6 |

## Crash matrix

| Case | Scale | Result | Evidence |
|---|---|---|---|
| case_01_clean_close_during_scan - Clean application close halfway through Scan | 112 committed, 16 still to read (11 in a worker) | PASS | checked 1 (min 1) |
| case_02_forced_kill_during_scan - Forced process termination halfway through Scan | kill@25%: 57 committed / 13 in flight; kill@75%: 97 committed / 14 in flight | PASS | checked 2 (min 1) |
| case_03_sheet_interrupted_in_processing - Sheet interrupted during processing |  | PASS | checked 27 (min 1) |
| case_04_kill_after_commit - Kill after a recognition commit, before the next | 1 sheet(s) in the committed group, 58 committed | PASS | checked 1 (min 1) |
| case_05_clean_close_during_resolve - Clean close halfway through Resolve | 89 operator events committed, 16 conflicts open | PASS | checked 1 (min 1) |
| case_06_forced_kill_after_resolve_corrections - Forced kill after Resolve corrections | 6 decisions in this incarnation, 62 operator audit events | PASS | checked 1 (min 1) |
| case_07_restart_retains_corrections - Restart retains resolved corrections |  | PASS | checked 27 (min 1) |
| case_08_unresolved_stay_unresolved - Unresolved items remain unresolved |  | PASS | checked 6 (min 1) |
| case_09_repeated_restarts - Repeated restarts create nothing twice |  | PASS | checked 6 (min 3) |
| case_10_results_after_interruption - Session results after interruption = uninterrupted |  | PASS | checked 184 (min 1) |
| case_11_resume_percentages - Resume at about 1 / 25 / 50 / 75 / 99 % | kills at [25, 75] % of 128 sheets | PASS | checked 2 (min 1) |
| case_12_integrity_after_every_case - Integrity after every case |  | PASS | checked 6 (min 1) |
| case_13_kill_before_conflict_generation - Kill after recognition, before conflict generation | 4 sheet(s) committed, 2 duplicate conflict(s) created by recovery | PASS | checked 1 (min 1) |
| case_14_sealed_batch_interrupted - Sealed batch interrupted mid-processing |  | PASS | checked 7 (min 1) |
| case_15_resolve_reachable_after_reopen - Resolve reachable after reopen without Scan |  | PASS | checked 6 (min 1) |

## Endurance

| Case | Scale / duration | Result | Evidence |
|---|---|---|---|
| endurance_a_many_finite_batches - >= 10 sealed batches in one session, several sources | 30 sealed batches from 4 sources in one session | PASS | checked 30 (min 10) |
| endurance_b_continuous_random_intake - Continuous random intake with operator decisions | 132 files over 151 s (0.87/s); 50 operator decisions while files were still arriving | PASS | checked 132 (min 130), duration_seconds 151.2, files 132, max_gap_seconds 53.05, median_gap_seconds 0.535, operator_decisions_during_intake 50 |
| endurance_c_supersession_reprocess - Reprocess All mid-session: nothing counted twice | Reprocess All of a 1-sheet unit while intake continued | PASS | checked 1 (min 1) |
| endurance_d_repeated_kill_restart - Repeated real kills and restarts | 5 real process kills, 6 restarts | PASS | checked 5 (min 3) |
| endurance_e_uninterrupted_control - Interrupted results = uninterrupted control |  | PASS | checked 184 (min 1) |

## Kill / restart sequence (interrupted run)

| # | Kind | Trigger | Committed at kill | In flight | Offline files | Restart s | Recovery | Integrity |
|---|---|---|---|---|---|---|---|---|
| 1 | syncing_duplicates | syncing_duplicates@10% | 57 | 12 | 3 | 0.375 | ok | clean |
| 2 | forced | kill@25% | 57 | 13 | 3 | 0.344 | ok | clean |
| 3 | after_commit | after_commit@35% | 58 | 12 | 3 | 0.421 | ok | clean |
| 4 | operator | operator@60% | 92 | 13 | 3 | 0.281 | ok | clean |
| 5 | forced | kill@75% | 97 | 14 | 0 | 0.562 | ok | clean |
| 6 | clean_close | clean_close@85% | 127 | 0 | 0 | 0.063 | ok | clean |

Checkpoints: checkpoint@10% (16 committed, ok), checkpoint@50% (65 committed, ok), checkpoint@90% (127 committed, ok), caught_up@100% (127 committed, ok)

## Workload

- sources: 3
- arrivals written: 132
- arrivals planned: 132
- unique contents: 127
- byte copy arrivals: 4
- sets: 4
- candidates on lists: 120
- partial writes: 90
- held open writes: 20
- renamed writes: 22
- long pause writes: 8
- header first writes: 7
- cross source same name: 44
- source outages: 1
- kills: 5
- clean closes: 1
- restarts: 6
- offline arrivals: 32
- duplicate id conflicts: 20
- operator corrections: 12
- duplicate acceptances: 8
- quality suggestions: 8
- confirmed rejections: 8
- dismissed suggestions: 2
- confirmed replacements: 8
- cross source replacements: 5
- replacement chains: 2
- superseded batches: 1
- sealed batches: 30
- operator decisions: 53
- real images: True

## Ground truth (planned)

- arrivals written: 131
- late arrivals: 1
- unique contents: 127
- byte copy arrivals: 4
- registered sheets: 128
- superseded sheets: 8
- effective sheets: 120
- effective scripts: 118
- blank pages: 2
- candidates on lists: 120
- scored candidates: 113

## Runs, timing and resources

- **interrupted**: 132 files over 151.2 s (0.873/s); recognition 0.861 sheets/s; run 181.3 s; peak tree RSS 328 MB; peak processes 4; max in flight 16; DB 2 MB; logged lock errors 0
- **control**: 132 files over 82.1 s (1.607/s); recognition 1.604 sheets/s; run 108.5 s; peak tree RSS 336 MB; peak processes 4; max in flight 5; DB 2 MB; logged lock errors 0
- **finite control**: 37.6 s (14.4 s recognition), batches 2
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
- workers 2, unit size 25, trickle 4.0 s, in flight 16, stability {'min_observations': 2, 'quiet_seconds': 1.2, 'max_decode_attempts': 3, 'retry_backoff_seconds': 1.0, 'poll_interval_seconds': 0.5}

