# `0.1.1-alpha.0` — acceptance criteria

> **Planning document.** Nothing here has been run. Every criterion is a
> requirement for later work. Reconciled 2026-09-30 against `main` at
> `128512d` (schema 12); see [ROADMAP.md](ROADMAP.md) §11.

Rules that apply to every section:

- A criterion is met only by **evidence recorded in the repository** — a test,
  a committed report, or a handoff entry with the command and its output — not
  by a statement that it was checked.
- `pytest`, `ruff check src tests tools scripts` and `mypy` (strict, as
  configured) pass at the end of every phase.
- Every pre-existing test passes **unchanged**. A test may be edited only if the
  phase's handoff explains why its old expectation was wrong, not merely
  inconvenient.
- **Passing tests do not complete a phase.** Status is reported on the six
  tracks of ROADMAP.md §7, separately.
- No real candidate data is committed. Real scans stay in the git-ignored
  `private_test_data/` or `local_test_data/`.

---

## 1. Cross-cutting invariants (must hold after every phase)

| # | Invariant | Checked by |
|---|---|---|
| X1 | A **single-batch session** — the traditional finite workflow — behaves exactly as `main` did before this line: registration, resume, retry, conflicts, reconciliation, scoring, reports, and no new operator step | Existing suites unchanged; a golden comparison of a finite project (rows and workbook cell values) |
| X2 | Every `ScanBatch` belongs to exactly one `ScanSession` after backfill; a **sealed** batch's membership never changes | Service tests; a database invariant check in `project_health` |
| X3 | Workers never open the database; only the coordinator writes | `tests/unit/test_architecture.py` |
| X4 | Services contain no Qt; the GUI imports no OpenCV/NumPy/SQLAlchemy | `tests/unit/test_architecture.py` |
| X5 | Nothing is deleted to express a decision: rejected, superseded, duplicate and reprocessed-away scans and batches, and their history, persist | Tests on every transition |
| X6 | Original source files are never modified, moved or deleted | Hash-before / hash-after tests on every intake path |
| X7 | `audit_event` and `batch_scan_history` remain append-only | Existing trigger tests |
| X8 | Migrations are additive and forward-only; numbers 1–12 are never reused; a real schema-12 project upgrades, backfills and scores identically | Upgrade tests from a committed schema-12 fixture |
| X9 | The Phase 10 harness and its assertions are unchanged and pass their small-scale self-test | `tests/unit/test_qualification.py`, `tests/integration/test_stress_*` |
| X10 | Answer ambiguity never becomes a conflict | Existing `test_conflict_policy.py` cases |
| X11 | Every set-code comparison goes through `set_identity` | Architecture test (from Phase A on) |
| X12 | No downstream stage chooses its input by `updated_at` or "latest batch" | Architecture/grep test and GUI tests (from Phase B on) |
| X13 | **Supersession invariant:** only effective, non-superseded batch/sheet membership contributes to session-level attendance, reconciliation, scoring, results and final reports; superseded batches remain retained and auditable | Effective-set tests for every cause (reprocess, whole-batch rescan, algorithmic reprocessing, reopen/recompute); nothing deleted (from Phase B on) |
| X14 | Lifecycle invariants: an OPEN batch may receive members, a SEALED batch never does, a SUPERSEDED batch is excluded from aggregation; an OPEN session may receive batches, a CLOSED one never does and has only sealed batches, a REOPENED one is open again and its prior final outputs are stale | Service and database-invariant tests (from Phase B on) |
| X15 | **Crash-safe persistence and resume:** after any interruption, reopening the project preserves every committed Scan and Resolve unit of work, retries only uncommitted work, reconstructs counts from committed rows, and never creates a new session or a superseding batch | §5.4 crash-safety tests (from Phase B on) |
| X16 | **Durable completion:** a sheet reported to the operator as recognised/completed already has its recognition result and all review/conflict state required for Resolve durably committed; the completed count shown is committed state, never buffered state | §5.4 cases 2, 4, 13; GUI tests (from Phase B on) |

---

## 2. Per-phase exit criteria

### A — Set identity

- A1. `__version__` is `0.1.1-alpha.0` in `_version.py` only, committed alone
  first; version and release-automation tests pass; `CHANGELOG.md`
  `[Unreleased]` notes the line has begun.
- A2. Migration 13 adds canonical code and physical mark; a schema-12 project
  with sets `A` and `a` upgrades, reports the collision by name, and blocks only
  the set-dependent stages until the operator renames or merges.
- A3. Defining `a` when `A` exists is refused.
- A4. A lower-case logical set and a mapped `10 → A` set pass end to end:
  Resolve, Attendance, Answer Key, Results and Reports show the logical set;
  the raw `A` is retained; the key is found (closes defect 5).
- A5. X11 holds.

### B — Scan sessions and finite batches

- B1. Migration 14 and the session service exist; `DATA_MODEL.md` documents
  each field; an ADR records the model.
- B2. First *Process All* in a project creates a session silently; the operator
  is asked nothing new.
- B3. Reopening the project and processing further scans creates a **new batch
  in the same session** (defect 1's cause removed).
- B4. Retrying failed sheets in an old batch, and `recover_interrupted` on
  open, do not change which data downstream reads (defect 2's cause removed).
- B5. A sealed batch refuses new members; a rescan of a sealed batch's sheet
  goes into a new `rescan` batch of the same session and the existing Reject &
  Rescan link still counts it once.
- B6. *Reprocess All* creates a `reprocess` batch that supersedes the batch it
  re-reads; nothing is counted twice; the superseded batch stays inspectable.
- B7. Upgrade backfill from a schema-12 fixture: one batch → one session with
  identical results; two unrelated batches of one project → two sessions, never
  combined automatically; batches joined by an unambiguous confirmed
  replacement → one session, counted-once behaviour identical; an ambiguous
  case → separate sessions, results unchanged, listed in the upgrade report.
- B7a. *Combine into one session* is refused across projects, is only ever
  operator-initiated, writes its audit event, and refuses (listing reasons) any
  combination that would create a duplicate effective sheet or a contradictory
  supersession relationship.
- B8. `processing_manifest` rows are written at the defined boundaries
  (closes defect 6).
- B9. Template change inside a session follows the ADR; never silent.
- B10. Session lifecycle: CLOSED refuses new batches and intake and seals every
  batch on close; REOPENED is open again, marks prior final outputs stale, and
  both reopen and re-close are audit events.
- B11. Batch supersession is a first-class record (reason, by, when, audited
  reversal); cycles and double supersession are refused; the superseded batch
  and its records are retained (X13).
- B12. **Required pre-Alpha defects S1, S2, S3 and R1 closed**
  (ARCHITECTURE_NOTES.md §13.5), each with a test that failed before the fix:
  - S1: recognition and its required conflict state form one crash-consistent
    work unit; after a kill between recognition persistence and conflict
    generation, the sheet is not treated as completed or Resolve-ready until
    recovery has deterministically generated its conflicts (§5.4 case 13);
  - S2: on reopen, interrupted session/batch state is discovered from persisted
    data, completed sheets are visible immediately, unfinished work is available
    for Resume;
  - S3: the completed count and "done" marks reflect committed state only;
  - R1: Resolve loads the persisted session/batch directly on reopen, without a
    visit to Scan.
- B13. §5.4 cases 1–10, 12 and 13 pass for the finite workflow with real process
  kills; recovery keeps an interrupted OPEN batch OPEN and an interrupted SEALED
  batch SEALED (resuming its unfinished processing), and never creates a
  session, batch or superseding batch.
- B14. The handoff records the measured cost of per-sheet commits and the
  chosen commit granularity (ARCHITECTURE_NOTES.md §13.8).

### C — Session-level review, reconciliation, scoring and reporting

- C1. The effective-scan-set service excludes rejected, superseded,
  re-imported, exact-duplicate and reprocessed-away scans by construction, and
  reports a count per exclusion reason.
- C2. Resolve shows one session queue with batch/source filters; cross-batch
  duplicate groups are navigable.
- C3. Duplicate-ID conflicts are computed session-wide on **effective**
  identifiers: the 10:03 / 10:47 / 11:05 scenario across two batches creates
  conflicts on both sheets and withdraws the untouched one on correction; a
  corrected ID that collides raises a conflict (closes defect 3).
- C4. (set, identifier) grouping exists as a policy option, default unchanged,
  documented as an office decision.
- C5. Byte-identical files added under different paths or batches produce one
  effective script; the other is listed as a duplicate (closes defect 4 for
  manual adds).
- C6. Attendance, reconciliation, Results and Reports aggregate the session; a
  candidate whose script is in another batch of the session is present
  (closes defect 1); pages select a session explicitly and name it (closes
  defect 2).
- C7. Batch-level diagnostic views exist and are labelled as such.
- C8. Results on an open session are labelled provisional everywhere shown or
  exported; Final Export requires a CLOSED session; on an open session it offers
  one step, "Close session and generate final export", which either lists the
  blockers and changes nothing, or closes and generates; `generated_report`
  records its scope and the close it came from; reopening marks those outputs
  stale until regenerated.
- C9. Golden regression: a single-batch project gives byte-identical
  reconciliation rows and workbook cell values.
- C10. A synthetic multi-set session over several batches with replacements
  and duplicates matches the generator's independently computed ground truth,
  and matches the same cohort processed as one batch.

> **Evidence on branch `feat/0.1.1-phase4-session-effective-set` (2026-10-02,
> automated tests; not operator-validated).** C1 `test_session_population*`,
> `test_exact_duplicates`; C2 `test_session_resolve_gui` (session queue, batch
> filter; a *source* filter waits for Phase D's intake source); C3
> `test_session_population::TestDuplicateMatrix`, `test_duplicate_sync_bounded`;
> C4 `TestGroupingPolicy`, `test_duplicate_grouping_gui`; C5
> `test_exact_duplicates`, `test_exact_duplicates_gui`; C6
> `test_session_acceptance`, `session_scope`; C7 Resolve's batch filter (labelled
> diagnostic) and Attendance's script panel, which names each script's batch; C8 `test_final_export_lifecycle`,
> `test_close_and_export_gui`; C9 `test_golden_one_batch` (against `main`
> `0e94d67`); C10 `test_generated_session_ground_truth`. Details:
> `PHASE_D_HANDOFF.md`.

### D — Intake sources and ledger

- D1. Every stable file in every enabled source is registered **exactly once**,
  including files present before the engine started.
- D2. No file becomes ready while still being written — tested with stepped
  growth, pauses, held-open files and header-first writes.
- D3. Same filenames in different sources are distinct; same relative path
  with new content is a new, flagged file.
- D4. Byte-identical content is `duplicate_content`, linked, never recognised.
- D5. An unreachable source is marked so without affecting other sources or
  marking its files vanished; on return, files created meanwhile are found.
- D6. After restart, files created while OMRFlow was closed are discovered;
  in-flight stabilisation is redone; ready files are re-verified.
- D7. Correctness holds with notifications disabled.
- D8. Stabilisation parameters are configuration, with the measured basis for
  their defaults in the handoff.
- D9. *Add Folder* and a watched source produce identical ledger records for
  identical files.

> **Evidence on branch `feat/0.1.1-phase5-intake-ledger` (2026-10-02, automated
> tests on local disks; not network-share or real-scanner validated).** D1
> `test_intake_acceptance`, `test_intake_processes::test_engine_killed_while_writers_continue_then_restarted`
> (36 files from three writer processes, engine killed and restarted: each
> registered once with its final bytes); D2 `test_intake_ledger::TestGrowingFiles`,
> `TestWriterPatterns`, `test_image_integrity`, `test_intake_filesystem` (a real
> exclusive handle), `test_intake_processes::test_a_writer_killed_mid_file_never_reaches_ready`;
> D3 `TestCollisions`, `test_intake_filesystem::TestRealWriters::test_path_reuse_on_disk`;
> D4 `TestCollisions`, `test_intake_manual::test_manual_and_watched_duplicates_use_one_rule`;
> D5 `TestReachability`; D6 `TestRestart`, `test_intake_processes`; D7 no
> notification code exists (reconciliation only; `TestOneDefinition`); D8
> `StabilityPolicy` per source, measurements in `PHASE_E_HANDOFF.md`; D9
> `test_intake_manual::test_add_folder_and_a_watched_source_record_the_same_identity`
> (identity fields: name, size, `mtime_ns`, hash, state; decode-evidence fields
> are filled for watched readiness only - see the handoff). X6 (sources never
> modified): `test_intake_filesystem::TestThreeSources`, `test_intake_processes`,
> `test_manual_intake_gui`.

### E — Continuous processing, quality decisions and session controls

- E1. Ready files are processed in sealed, per-source units satisfying every
  Phase 5 invariant; a unit interrupted by a forced kill resumes without
  resubmitting committed sheets (measured, as Phase 10 does).
- E2. Sheet-local conflicts appear after each unit commit; the finite GUI path
  syncs conflicts exactly as before.
- E3. The quality decision layer is pure, versioned, fingerprinted, pinned per
  session, labelled unvalidated; RESCAN_REQUIRED produces a **suggested**
  rejection that a named operator confirms through the existing flow; no new
  geometric threshold.
- E4. The session snapshot's counts partition in every test that produces one.
- E5. Concurrent writes (intake + recording + a review decision) complete
  without `database is locked` failures in a contention test; GUI-thread write
  latency bounded.
- E6. `ScanJobStatus.PROCESSING` is written while a sheet is in a worker and
  recovered as today.
- E7. Pause lets in-flight sheets finish **and be recorded**; finish-current
  replaces destructive cancel as the default stop.
- E8. Finish-session returns each blocker of §3 individually, or closes,
  audited; files arriving for a closed session are held.
- E9. Crash safety holds under continuous processing: §5.4 cases 1–4 and
  9–12 pass with intake running (smaller-scale case 11), including a kill
  between a unit commit and its conflict sync; recovery resumes the same
  session and sealed unit, never a new session or a superseding batch.

> **Evidence for the first part of E (revised phase 6), on branch
> `feat/0.1.1-phase6-continuous-engine` (2026-10-02, automated tests on local
> disks; not network-share, real-scanner or power-loss validated).** E1
> `test_continuous_engine` (sealed per-source units, arrivals form new units),
> `test_engine_recovery::TestKillAtEachBoundary` (8 boundaries) and
> `tests/crash/test_engine_kills.py` (real kills at 6 boundaries; submission
> log against the database: no committed sheet submitted after a kill); E2
> unit-end batch-scope pass (`TestResolveAcrossRestarts`,
> `test_three_sources_arrivals_late_duplicate_and_restart`); the finite GUI
> path is untouched and its suites pass unchanged; E5
> `test_engine_contention` (engine + concurrent Resolve decisions, no lock
> error, latency recorded; `PRAGMA busy_timeout` set); E6
> `test_processing_rules::TestClaims` (`processing` written by compare-and-set,
> recovered to `pending`); E9 kill-boundary matrix with intake running,
> `TestRestartAtProgressPoints` (1/25/50/75/99 %), `TestRepeatedRestarts`,
> `test_repeated_kills_converge`. **Not yet (revised phase 7):** E3 quality
> decisions, E4 session snapshot partition, E7 pause / finish-current *policy*
> and persisted flags (the engine has the mechanical primitives:
> `pause_intake`, `pause_scheduling`, `cancel_queued`, draining `shutdown`),
> E8 finish-session validation and held-file decisions. Details:
> `PHASE_F_HANDOFF.md`.

> **Evidence for the second part of E (revised phase 7), on branch
> `feat/0.1.1-phase7-quality-session-controls` (2026-10-04, automated tests on
> local disks; not network-share, real-scanner or power-loss validated; the
> quality defaults are unvalidated).** E3 `tests/unit/test_quality_decision.py`
> (every policy row incl. the shapes recognition really stores, multiple and
> unknown evidence, version, fingerprint stability and sensitivity, no new
> threshold), `test_quality_rescan` (decisions in the work unit, nothing
> rejected, pinned policy survives a default change, confirmation = `reject_scan`,
> dismissal audited, Resolve's evidence decision answers it),
> `test_quality_migration` (schema-16 fixture); E4 `test_session_snapshot`
> (partition checked against an independent total and the effective-set service
> in every snapshot, three progress lines, recognition falling on arrivals,
> caught-up vs unreachable / paused / disabled / stale, per-source counts and
> rate alarm, fixed statement count, read-only), `test_snapshot_scale` (10,000
> rows); E7 `test_persisted_controls` (pause lets in-flight sheets finish and be
> recorded, survives restart; finish-current and cancel-queued distinct, nothing
> left claimed), `test_phase7_kills` (real kills: pause persisted, running
> resumes); E8 `test_session_finish` (every blocker together and each alone,
> acknowledged incomplete results audited, close sealed and audited, later
> arrivals held, reopen named and stale outputs, re-close re-validates),
> `test_phase7_kills` (kill inside close / reopen: never half-done),
> `tests/gui/test_final_export_finish_policy.py` (§3's "the same checks run in
> the one-step Close session and generate final export": Reports and the Scan
> stage close only through `finish_scan_session` - held file, suggested rescan,
> unreachable source, queued work and another coordinator refuse; incomplete
> results only by named acceptance; a clean finite session still closes and
> exports - added after pre-merge review found the Reports path bypassing it). E9 extended:
> real kills inside the quality work unit, between a commit and its duplicate
> pass, inside rejection and replacement confirmation. Phase F carry-forwards:
> `test_incremental_duplicate_sync`, `test_coordinator_ownership`,
> `test_scan_coordinator_gui`, `test_phase7_recovery`. Scenario:
> `test_phase7_scenario` (341 real synthetic sheets, three scanners). Details:
> `PHASE_G_HANDOFF.md`.

### F — Operational GUI

- F1. With one implicit single-batch session, the Scan stage is the
  `0.1.0-alpha.2` workflow; existing GUI tests pass unchanged.
- F2. Session mode: configure sources, start, pause/resume, finish with a
  blocker list, reopen — each tested.
- F3. Three separate progress lines; recognition may decrease; "Caught up —
  watching for new scans" only under its definition; an unreachable source
  prevents it; no combined percentage anywhere.
- F4. Per-source status is compact and collapsible, not dominant.
- F5. Resolve and the Rescan queue update while intake runs.
- F6. At 10,000 files with ongoing arrivals the GUI stays responsive (latency
  measured, threshold recorded); growing lists are paged models.
- F7. Screenshots via the `qtguitesting` workflow for each new state, at
  1366×768 and 175 % scaling.
- F8. Reopening a project with interrupted session work shows the reconstructed
  state (per session, batch and source; recognised / failed / pending; conflicts
  resolved) before Resume is pressed, from committed rows; final outputs made
  stale by a reopen are shown as stale.

> **Evidence (revised phase 8, branch `feat/0.1.1-phase8-operational-gui`, not
> merged; GUI tests and scripted runs only - no operator, network share or real
> scanner).** F1 `test_session_scan_gui::TestFiniteModeIsUnchanged` (no panel,
> no list chooser, no polling thread, Process All tooltips unchanged) and the
> unchanged pre-existing Scan / Resolve GUI suites; F2
> `test_session_controls_gui` (incl. a coordinator-busy refusal rendered as a
> notice), `test_session_finish_gui` (grouped blockers, acknowledgement only
> for the three acceptable kinds, named operator, reopen named with stale
> outputs, re-close re-validates),
> `test_session_scan_gui` (add source incl. UNC, close / project switch while
> scanning); F3 `test_session_controls_gui` + `test_session_scan_gui::TestWordsForValues`
> (recognition falls, never a percentage; caught up only from the snapshot;
> unreachable named); F4 `test_session_scan_gui` (folded by default); F5
> `test_rescan_queue_gui`, `test_intake_decisions_gui`, the cross-scanner
> duplicate reaching Resolve live; F6 `test_operational_gui_responsiveness`
> (10,000 sheets with arrivals: event-loop latency measured, thresholds median
> < 30 ms, p95 < 150 ms, worst < 2 s - the worst case is set by rollback-journal
> waits, see the handoff), `test_scan_paging_gui` (100,000 rows, SQL-paged);
> F7 `capture_session_states.py` (16 states, 1366×768 and 1100×680, native
> 175 %, zoom 80–200 %); F8 `test_session_recovery_gui` (a real process kill,
> then the counts before Start; persisted pause shown). Details:
> `PHASE_H_HANDOFF.md`.

### G — Qualification and release

See §§4–8 (and §9 for what remains before Beta).

---

## 3. Finish scan session — closure checks

Closure is refused, listing each blocker, unless all hold after a final
reconciliation of every configured source:

- nothing stabilising, ready, queued or processing;
- no unresolved required conflict (sheet-local or cross-batch);
- no outstanding rescan item; no unmatched replacement;
- no held file awaiting a decision;
- every enabled source reachable for the final reconciliation; disabled
  sources listed by name.

A temporary absence of new files never closes a session. On success the
session becomes CLOSED: every contributing batch is sealed, no new batch or
intake is accepted, and final reconciliation, scoring, results and reports
represent its authoritative state. The same checks run in the one-step "Close
session and generate final export" at Final Export, so a finite import needs no
separate session-management screen. Reopening is explicit and audited, returns
the session to OPEN, and marks prior final outputs stale; re-closing is audited
and final outputs must then be regenerated.

---

## 4. Real-world acceptance matrix

| # | Scenario | Observable acceptance |
|---|---|---|
| A | Single scanner, finite folder | *Add Folder → Process All* identical to today; same results as `main` on the same images |
| B | Two scanners, files copied in afterwards | Two batches in one session; every sheet registered once; source recorded per batch |
| C | Three scanners writing continuously | Sealed units per source; backlog visible; no sheet processed before stable; totals equal files written |
| D | Network share disconnected mid-run | Source *unreachable*; nothing marked vanished; resumes on reconnect; no OMR failures for the outage |
| E | OMRFlow stopped, files added, restarted | New files discovered on start; no reprocessing of committed sheets |
| F | Same image imported twice (two paths/sources/batches) | One effective script; the other listed as duplicate, linked |
| G | Same physical script scanned twice | Session-wide duplicate-ID conflict; one counts after decision; both kept |
| H | Rejected sheet later rescanned on another station | Suggested and linked; counted once in the session; lineage in audit |
| I | Project reopened, more scans processed | New batch in the same session; no candidate becomes absent (defect 1) |
| J | Old batch retried / project reopened after interruption | What is scored does not change (defect 2) |
| K | File still being written | Not registered until stable; no decode failure recorded |
| L | Large backlog while processing continues | GUI responsive; backlog and rate shown; memory bounded |
| M | Logical set `10` printed as `A` | Every stage shows Set 10; raw `A` retained |
| N | Set codes typed `a` / `A` | One set; key found; duplicates refused at definition |
| O | Kill during continuous processing | Resume safe; Phase 10 kill criteria still hold |
| P | *Reprocess All* in a session | Old batch superseded; nobody counted twice |
| Q | Open vs closed session | Provisional labels while open; Final Export only when closed |
| R | Crash while scanning (sheet 638 of 1,000) | 637 results kept and not re-read; 638 retried; counts reconstructed; same session and batch |
| S | Crash after 47 of 120 Resolve decisions | 47 kept with audit history; 73 in the active queue |
| T | Reopen after Final Export | Final outputs marked stale; regeneration required after changes |

---

## 5. Synthetic qualification: intake campaign, endurance and crash safety

**Additional to, and independent of, the Phase 10 100,000-sheet campaign**,
which keeps validating deterministic finite processing and forced kill/resume.

### 5.1 Workload

- ≥ 3 simulated scanner sources, ≥ 10,000 images in total, from the existing
  template-driven synthetic generator so ground truth is exact; multi-set, with
  paired attendance workbooks and answer keys.
- The **same filenames** from different sources.
- Files written gradually and partially (stepped growth, held-open, header
  first), with random inter-arrival times.
- Byte copies within and across sources.
- Temporary source loss and reconnection.
- Forced application kill and restart while sources keep producing.
- New duplicate-ID conflicts arising while a scripted operator resolves earlier
  ones.
- Fold-generated bad sheets triggering RESCAN_REQUIRED suggestions, confirmed
  rejections, and replacements — including from a different source, and chains.
- Session closure, then session-level Results and Reports compared with ground
  truth.

Driven by a supervisor in the Phase 10 style: evidence written from outside the
process being killed; kill points chosen from committed state, not wall time.

### 5.2 Release-blocking assertions

| Assertion | Requirement |
|---|---|
| `stable_files_discovered_exactly_once` | Every stable file ↔ exactly one ledger row and at most one effective scan |
| `no_incomplete_file_processed` | No file was submitted before its writer finished (writer log vs submission log) |
| `source_provenance_retained` | Every scan's source, path and filename match the generator's manifest |
| `duplicate_content_identified` | Every planted byte copy, and nothing else, is `duplicate_content` |
| `independent_filenames_do_not_collide` | Same-named files from different sources are distinct and all processed |
| `batches_finite` | No sealed batch gained a member; every batch belongs to the session |
| `no_accepted_image_lost` | Every accepted scan is still present and effective after all restarts |
| `no_completed_scan_rerecognised` | Measured via the submission log |
| `offline_arrivals_discovered` | Files created while OMRFlow was down are all discovered after restart |
| `conflict_counts_correct_as_population_grows` | At each checkpoint, open duplicate-ID conflicts equal the ground-truth count for the population seen so far |
| `rescan_relationships_survive_restart` | Every confirmed association and supersession is intact after each restart |
| `aggregate_counts_consistent` | The session snapshot partitions at every checkpoint and equals a recount from raw rows |
| `session_results_match_ground_truth` | Session-level attendance, marks and report cell values equal the independently computed expectation |
| `sqlite_integrity` | `quick_check`, `integrity_check`, `foreign_key_check` |
| `application_invariants` | `project_health.full_check` reports no error-level or critical issue |
| `finite_mode_regression` | A single-batch session run in the same build matches its golden result |

No assertion may be demoted to a warning to let the campaign pass. A smaller run
is reported as "ALL RUNS PASSED — NOT THE RELEASE QUALIFICATION", as Phase 10
does.

### 5.3 Targeted endurance tests (required for Alpha)

A fresh 100,000-sheet run is **not** required for `0.1.1-alpha.0`. Instead,
committed endurance tests (marked `stress` where long) must cover, on the new
architecture:

- **multiple batches** in one session (≥ 10 sealed batches, several sources);
- **continuous intake** over an extended period with random arrivals;
- **supersession / reprocessing** — *Reprocess All* and an algorithmic re-read
  mid-session, with totals unchanged and nothing double-counted;
- **restart / resume** — repeated kills and restarts during intake and
  processing;
- **session-level aggregation** — attendance, marks and report cells equal the
  ground truth, and equal an uninterrupted run.

Scale and durations are recorded in the handoff; they are evidence, not a
substitute for the pre-Beta 100k qualification (§9).

### 5.4 Crash-safe persistence and resume tests (required for Alpha)

Real process termination (not a simulated exception) wherever "forced"
appears; evidence recorded from outside the killed process, in the Phase 10
style.

| # | Test | Must show |
|---|---|---|
| 1 | Clean application close halfway through Scan | Committed sheets kept; the rest resumable; nothing marked failed |
| 2 | Forced process termination halfway through Scan | Every committed sheet kept and not re-recognised (submission log); uncommitted ones retried |
| 3 | Restart with a sheet interrupted during processing | That sheet returns to a retryable state, never "completed" or "failed" |
| 4 | Restart after a recognition commit but before the next sheet starts | Nothing lost, nothing re-read |
| 5 | Clean close halfway through Resolve | Every confirmed decision kept |
| 6 | Forced termination after several Resolve corrections | Every committed correction kept, with its audit event |
| 7 | Restart retains resolved corrections | Machine value, override, effective value and history intact |
| 8 | Restart keeps unresolved items unresolved | Active queue = exactly the unresolved items |
| 9 | Repeated restarts | No duplicate sheet, conflict, attendance, scoring or audit rows; no new session; no superseding batch |
| 10 | Session-level results after interruption | Counts and scores identical to an uninterrupted run |
| 11 | Resume after interruption at ≈ 1, 25, 50, 75 and 99 % of a large run | Each point meets 2–4 and 10 (Phase G at scale; smaller-scale versions from Phase B/E on) |
| 12 | Integrity after every case | `quick_check`, `integrity_check`, `foreign_key_check`; health check clean; audit history valid and append-only |
| 13 | **Kill after recognition is persisted but before conflict generation** | The sheet is never reported completed or Resolve-ready in that state; on restart recovery deterministically generates exactly the missing conflicts (no duplicates), and only then counts the sheet as completed; the sheet is not recognised again |
| 14 | Sealed batch interrupted mid-processing | After restart it is still SEALED, gained no members, and its unfinished sheets resume |
| 15 | Resolve reachable after reopen | Without visiting Scan, Resolve opens the persisted session/batch with the committed decisions and the unresolved queue |

After reopening, the GUI shows the reconstructed state — e.g. *637 / 1,000
recognised · 4 failed (retryable) · 359 pending* and *47 / 120 conflicts
resolved* — before Resume is pressed, from committed rows, never from a stored
progress value.

> **Evidence for §5 (revised phase 9, branch
> `feat/0.1.1-phase9-automated-qualification`, commit `542b2af`, clean tree;
> synthetic, local disk, source build - not SMB, real-scanner, power-loss or
> installed-build evidence).** Harness `omr_scanner.evaluation.intake_qualification`
> ([docs](../../../docs/intake_qualification.md)); release-scale campaign
> `p9-release-20261006T151124Z`, verdict **`QUALIFIED`** (generated):
> §5.1 - 3 separate scanner-writer processes, 10,189 files written (7,082
> partially: stepped, header first, held open, a pause beyond the quiet
> period, `.part` + rename), 3,459 names written at more than one scanner,
> 200 byte copies (100 across scanners), one source outage (a junction
> removed for 288 s), 8 real kills and 1 clean close while the writers kept
> writing (9 restarts into the same project and session), 168 duplicate-ID
> conflicts arising during intake while a scripted operator resolved
> earlier ones (375 decisions during intake), 64 `RESCAN_REQUIRED`
> suggestions (56 confirmed, 10 dismissed), 56 replacements (34 from another
> scanner, 2 chains), closure through the finish policy, Results and report
> cells compared with an independent reference; §5.2 - all 16 assertions
> `PASS`; §5.3 - A 712 sealed batches from 4 sources, B 13,069 s of random
> intake, C *Reprocess All* of a finished unit while intake continued, D 8
> kills / 9 restarts, E interrupted = uninterrupted control (9,997 values
> compared); §5.4 - cases 1–15 `PASS`, case 11 kills at 1 / 25 / 50 / 75 /
> 99 % (101 / 2,501 / 4,995 / 7,506 / 9,899 committed, 15 / 10 / 16 / 9 / 12
> sheets in flight), integrity and health (no error) after every kill.
> Reports: `docs/release/validation/0.1.1-alpha.0-phase9-intake/`. Details:
> `PHASE_I_HANDOFF.md`.

---

## 6. Network-share (SMB) qualification

Phase 5 explicitly did **not** establish behaviour on a genuine network share.
On real infrastructure:

- ≥ 2 Windows machines; sources on genuine SMB shares (`\\host\share\…`),
  written by a process on the remote machine.
- ≥ 1,000 files per source, including the partial-write patterns.
- Share disconnected (cable/adapter/service) and restored mid-write.
- OMRFlow restarted during intake.
- Listing cost per reconciliation and the stabilisation latency distribution
  measured; Phase D defaults confirmed or adjusted.
- The project database on a local disk (unless an ADR says otherwise); the
  report says which.

Result committed under `docs/release/validation/` with machine descriptions and
OS/SMB versions. **Required for `0.1.1-alpha.0`.** A simulated share is never
reported as SMB.

---

## 7. Real scanning-room qualification

Not required for `0.1.1-alpha.0`. Performed on a later `0.1.1` Alpha, and part
of the evidence Phase 11B's Beta decision rests on:

- two or more real scanner workstations, scanning continuously;
- the same filenames from different scanner PCs;
- network interruption and reconnection; OMRFlow restart during intake;
- operators resolving conflicts while scanning continues;
- real scan-quality rejects, physical rescans and replacements;
- final session closure;
- **independently verified result correctness** — stored results compared with
  an independently computed expectation, never approved by visual inspection
  (the existing Phase 11B rule);
- quality-decision defaults set from these real rejects, with the evidence.

---

## 8. `0.1.1-alpha.0` release gate

All of:

1. Phases A–F: implemented and tested; X1–X12 hold.
2. §5 synthetic intake campaign: QUALIFIED at full scale.
3. §6 SMB qualification: passed and recorded.
4. The Phase 10 harness self-test passes. A fresh 100,000-sheet run is
   **optional at Alpha** (decided); if it was not run, the release notes say so
   plainly. The §5.3 endurance tests and every §5.4 crash-safety test pass.
4a. **Required defects S1, S2, S3 and R1 are closed** (B12), with their tests
   passing on the installed build as well as from source.
5. Packaged application: existing packaging smoke and installer checks, plus a
   scripted live-intake smoke run **on the installed build** (two local
   sources, a few hundred files, one restart).
6. Upgrade from a schema-12 project (and from a `0.1.0-alpha.2` project)
   verified on the installed build; the forward-only consequence stated in the
   release notes.
7. Documentation: user guide for scan sessions; updated Scanning, Processing,
   Review-and-Resolution, Attendance, Results-and-Reports, Known-Limitations and
   Upgrade-Compatibility wiki pages; README development and testing status.
8. Release notes state plainly: real scanning-room qualification **not yet
   performed**; quality-decision defaults **unvalidated**.
9. `docs/release/RELEASE_CHECKLIST.md` followed; published as a GitHub
   pre-release only on the owner's instruction.

Not required for `0.1.1-alpha.0`: §7, Phase 11B's Beta criteria, code signing,
a fresh 100,000-sheet run.

---

## 9. Gates before the first Beta

In addition to Phase 11B's own criteria (real-data qualification, which runs
alongside the `0.1.1` line), **no Beta tag may be created** until:

1. **A fresh 100,000-sheet qualification on the new architecture** —
   ScanSession, finite ScanBatch units, continuous processing — has run and
   been reported QUALIFIED, including interruption and resume at ≈ 1, 25, 50,
   75 and 99 %. The earlier Phase 10 finite-batch harness (whose 100k run was
   never completed) does not satisfy this.
2. **Finding F8 is fixed and tested**: `numeric_version` and the installer's
   upgrade comparison order every prerelease correctly — within one base
   version a Beta sorts above its Alphas (e.g. `0.1.1-beta.1` above
   `0.1.1-alpha.N`) — with tests. The Beta is **`v0.1.1-beta.x`**;
   `v0.1.0-beta.x` is not used after `v0.1.1-alpha.0`.
3. The §7 real scanning-room qualification has been performed.
