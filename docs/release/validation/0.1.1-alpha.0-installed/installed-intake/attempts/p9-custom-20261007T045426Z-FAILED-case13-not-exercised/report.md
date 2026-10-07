# Intake qualification - p9-custom-20261007T045426Z-20261006

**Verdict: FAILED**

Mode `custom` · commit `87d9fa5bc8dc6d352af43d7502ac9ce27690d38a` (working tree clean: True) · schema 17 · OMRFlow 0.1.1-alpha.0

Not the release qualification because: 2 sources < 3; 249 arrivals < 10,000; mode 'custom' is not the release mode

## What this proves

Installed-build behaviour of the headless continuous-processing stack under a synthetic multi-source intake campaign: every coordinator incarnation is the packaged OMRFlow executable (its bundled Python, SQLite, OpenCV and frozen recognition workers), forcibly terminated and restarted into the same project and session while separate scanner writer processes keep writing to local folders; durable state is read from outside and compared with exact ground truth.

It does **not** prove: the installed build's GUI (the packaged executable runs the coordinator headless; the supervisor, writers, finite control and evaluator run from source); genuine SMB / network-share behaviour (local folders; the outage is a local folder link); real scanner behaviour (images are synthetic, written by processes); actual power-loss durability (process termination is not power removal); real-paper quality-policy calibration (the quality policy is the unvalidated default); a clean machine (the build machine ran it); recognition accuracy on real scans; production readiness.

## Required assertions

| Assertion | Status | Evidence |
|---|---|---|
| `stable_files_discovered_exactly_once` | PASS | checked 249 (min 249), written_files 249, vanished_seen 0, manual_source_rows 1, ledger_rows 252, temporary_names_ignored 2 |
| `no_incomplete_file_processed` | PASS | checked 244 (min 239), partial_writes 178, min_seconds_completion_to_submission 14.227, median_seconds_completion_to_submission 20.412 |
| `source_provenance_retained` | PASS | checked 244 (min 1), registered_files_checked 244, original_names_checked 250, reprocessed_sheets_traced 1 |
| `duplicate_content_identified` | PASS | checked 5 (min 1), planted_copies 5, within_source 3, across_sources 2, same_name_across_sources 0, flagged 5 |
| `independent_filenames_do_not_collide` | PASS | checked 216 (min 2), names_on_several_sources 108, files_checked 216, independent_registered 206 |
| `batches_finite` | PASS | checked 55 (min 1), batches 55, sealed 55, watched_sources_with_units 3, digests_rechecked 53 |
| `no_accepted_image_lost` | PASS | checked 236 (min 1), expected_effective 236, actual_effective 236, kills_checked 5 |
| `no_completed_scan_rerecognised` | PASS | checked 911 (min 1), claimed_twice_in_one_incarnation 0 |
| `offline_arrivals_discovered` | PASS | checked 40 (min 1), written_while_coordinator_down 22, written_while_source_unreachable 18, down_intervals 5 |
| `conflict_counts_correct_as_population_grows` | PASS | checked 24 (min 1) |
| `rescan_relationships_survive_restart` | PASS | checked 8 (min 1), links 8, cross_source 5, chains 2, restarts_checked 5 |
| `aggregate_counts_consistent` | PASS | checked 5 (min 3), checkpoints 5 |
| `session_results_match_ground_truth` | PASS | checked 6,667 (min 1), cells_compared 5229, results_compared 6667 |
| `sqlite_integrity` | PASS | checked 8 (min 1), reports 8 |
| `application_invariants` | PASS | checked 8 (min 1), reports 8 |
| `finite_mode_regression` | PASS | checked 6,989 (min 1), finite_batches 2, finite_seconds 42.7 |

## Crash matrix

| Case | Scale | Result | Evidence |
|---|---|---|---|
| case_01_clean_close_during_scan - Clean application close halfway through Scan | 207 committed, 14 still to read (5 in a worker) | PASS | checked 1 (min 1) |
| case_02_forced_kill_during_scan - Forced process termination halfway through Scan | kill@25%: 151 committed / 4 in flight; kill@75%: 197 committed / 4 in flight | PASS | checked 2 (min 1) |
| case_03_sheet_interrupted_in_processing - Sheet interrupted during processing |  | PASS | checked 8 (min 1) |
| case_04_kill_after_commit - Kill after a recognition commit, before the next | 4 sheet(s) in the committed group, 155 committed | PASS | checked 1 (min 1) |
| case_05_clean_close_during_resolve - Clean close halfway through Resolve | 121 operator events committed, 6 conflicts open | PASS | checked 1 (min 1) |
| case_06_forced_kill_after_resolve_corrections - Forced kill after Resolve corrections | 2 decisions in this incarnation, 114 operator audit events | PASS | checked 1 (min 1) |
| case_07_restart_retains_corrections - Restart retains resolved corrections |  | PASS | checked 50 (min 1) |
| case_08_unresolved_stay_unresolved - Unresolved items remain unresolved |  | PASS | checked 5 (min 1) |
| case_09_repeated_restarts - Repeated restarts create nothing twice |  | PASS | checked 5 (min 3) |
| case_10_results_after_interruption - Session results after interruption = uninterrupted |  | PASS | checked 300 (min 1) |
| case_11_resume_percentages - Resume at about 1 / 25 / 50 / 75 / 99 % | kills at [25, 75] % of 244 sheets | PASS | checked 2 (min 1) |
| case_12_integrity_after_every_case - Integrity after every case |  | PASS | checked 5 (min 1) |
| case_13_kill_before_conflict_generation - Kill after recognition, before conflict generation |  | NOT_EXERCISED | checked 0 (min 1) |
| case_14_sealed_batch_interrupted - Sealed batch interrupted mid-processing |  | PASS | checked 2 (min 1) |
| case_15_resolve_reachable_after_reopen - Resolve reachable after reopen without Scan |  | PASS | checked 5 (min 1) |

## Endurance

| Case | Scale / duration | Result | Evidence |
|---|---|---|---|
| endurance_a_many_finite_batches - >= 10 sealed batches in one session, several sources | 55 sealed batches from 3 sources in one session | PASS | checked 55 (min 10) |
| endurance_b_continuous_random_intake - Continuous random intake with operator decisions | 249 files over 464 s (0.54/s); 50 operator decisions while files were still arriving | PASS | checked 249 (min 247), duration_seconds 464.0, files 249, max_gap_seconds 60.14, median_gap_seconds 1.087, operator_decisions_during_intake 50 |
| endurance_c_supersession_reprocess - Reprocess All mid-session: nothing counted twice | Reprocess All of a 1-sheet unit while intake continued | PASS | checked 1 (min 1) |
| endurance_d_repeated_kill_restart - Repeated real kills and restarts | 4 real process kills, 5 restarts | PASS | checked 4 (min 3) |
| endurance_e_uninterrupted_control - Interrupted results = uninterrupted control |  | PASS | checked 300 (min 1) |

## Kill / restart sequence (interrupted run)

| # | Kind | Trigger | Committed at kill | In flight | Offline files | Restart s | Recovery | Integrity |
|---|---|---|---|---|---|---|---|---|
| 1 | forced | kill@25% | 151 | 4 | 3 | 0.25 | ok | clean |
| 2 | after_commit | after_commit@35% | 155 | 0 | 3 | 0.359 | ok | clean |
| 3 | operator | operator@60% | 197 | 0 | 3 | 0.078 | ok | clean |
| 4 | forced | kill@75% | 197 | 4 | 3 | 0.187 | ok | clean |
| 5 | clean_close | clean_close@85% | 211 | 0 | 3 | 0.094 | ok | clean |

Checkpoints: checkpoint@10% (26 committed, ok), checkpoint@50% (155 committed, ok), checkpoint@90% (218 committed, ok), caught_up@100% (243 committed, ok)

## Workload

- sources: 2
- arrivals written: 249
- arrivals planned: 249
- unique contents: 243
- byte copy arrivals: 5
- sets: 4
- candidates on lists: 240
- partial writes: 178
- held open writes: 32
- renamed writes: 44
- long pause writes: 16
- header first writes: 25
- cross source same name: 108
- source outages: 1
- kills: 4
- clean closes: 1
- restarts: 5
- offline arrivals: 22
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
- sealed batches: 55
- operator decisions: 53
- real images: True

## Ground truth (planned)

- arrivals written: 248
- late arrivals: 1
- unique contents: 243
- byte copy arrivals: 5
- registered sheets: 244
- superseded sheets: 8
- effective sheets: 236
- effective scripts: 234
- blank pages: 2
- candidates on lists: 240
- scored candidates: 229

## Runs, timing and resources

- **interrupted**: 249 files over 464.0 s (0.537/s); recognition 0.516 sheets/s; run 522.6 s; peak tree RSS 574 MB; peak processes 5; max in flight 16; DB 4 MB; logged lock errors 0
- **control**: 249 files over 452.6 s (0.55/s); recognition 0.528 sheets/s; run 511.4 s; peak tree RSS 579 MB; peak processes 5; max in flight 3; DB 4 MB; logged lock errors 0
- **finite control**: 42.7 s (17.7 s recognition), batches 2
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
- workers 4, unit size 25, trickle 4.0 s, in flight 16, stability {'min_observations': 2, 'quiet_seconds': 5.0, 'max_decode_attempts': 3, 'retry_backoff_seconds': 5.0, 'poll_interval_seconds': 10.0}
- coordinator: **packaged** `C:\Users\Sajid\AppData\Local\Programs\OMRFlow\OMRFlow.exe` (12785528 bytes, SHA-256 `ac453449bfc258fa16b9f9b1ced9ee89ec86d0331b0168f119ca618a29c23a78`)
- coordinator runtime (7 incarnations of interrupted, control): frozen True, Python 3.12.7, package `C:\Users\Sajid\AppData\Local\Programs\OMRFlow\_internal\omr_scanner`, SQLite 3.45.3, journal_mode delete, synchronous 2, busy_timeout 5000, foreign_keys 1

