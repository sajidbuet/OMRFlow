# Revised phase 7 handoff — quality decisions, rescan suggestions, persisted controls, session closure (roadmap 0.1.1-E, second half)

> `PHASE_G_HANDOFF.md` is the handoff for revised Phase 7. It implements the
> second half of roadmap phase 0.1.1-E. It is not roadmap phase 0.1.1-G
> qualification.

| Track | Status |
|---|---|
| Implemented | **Yes** - headless: quality decision layer, rescan suggestions, held-file decisions, persisted controls, coordinator lease, incremental duplicate sync, session snapshot, finish-session validation |
| Automated testing | **Passing** (exact counts in §14) |
| Synthetic validation | A 341-file, three-scanner scenario with real synthetic sheets read by the real engine, a kill and restarts (§12); real process kills at every new durable transition (§11). **Not** the phase 9 campaign |
| Network-share validation | **Not performed** |
| Real-scanner validation | **Not performed** |
| Power-loss validation | **Not performed** (process kills only) |
| Production qualification | **Not performed** |
| Verdict (after the pre-merge correction pass, §14a) | **COMPLETE WITH NON-BLOCKING LIMITATIONS** (§15). Before that pass the Reports close path bypassed the finish policy - a phase 7 integration blocker, fixed before merge |

## 1. Git state

* Branch `feat/0.1.1-phase7-quality-session-controls`, worktree
  `C:\Research\OMRflow-p7`.
* Baseline: `main` at `e8200b4` (*Merge feat/global-ui-zoom into main*), which
  contains the phase 6 merge `141d703`.
* Commits (oldest first): `5733ac1` quality layer + migration 17, `1569a23`
  rescan suggestions, `4539bd3` held-file decisions, `2c225cf` controls,
  `8b3d40f` coordinator, `3f7b8e2` snapshot, `0fce546` finish-session,
  `5a3429e` engine integration, `68c864b` migration fixture + tests, `f1ccadb`
  / `516428b` tests, `3973564` snapshot deadlock fix + recovery / contention
  tests, `9fd3168` crash kills, `888e60c` scenario, `c426146` snapshot
  performance, then the documentation commit(s) ending at `94241e0`.
* Pre-merge correction pass (§14a), worktree
  `D:\Sajid\OneDrive - BUET\Coding-Projects\OMRflow-phase7`: `7f69c13`
  *fix: route final export through session finish policy*, `d64a623` *docs:
  correct rollback-journal contention notes*, `69b5e8d` (test line wrap), then
  this documentation commit. Tip: see the final report.
* Pushed to `origin` as a branch (the phase workflow: phase branches are
  pushed, merged only on the owner's instruction). **Not merged** into `main`;
  nothing tagged or released.
* Working tree: clean apart from the main checkout's unrelated, untracked
  `docs/debug/` (not touched, not committed).

## 2. Audit before implementation

Phase 6 (`PHASE_F_HANDOFF.md`) already provided, and phase 7 reuses unchanged:
durable compare-and-set claims; the single coordinator writer; the ADR-0006
work unit (`record_results`: result + that sheet's conflicts in one
transaction); `recover_on_open` and the restart sequence; finite sealed units
from the intake ledger; the bounded session-wide duplicate pass
`sync_duplicate_identifiers_for`; the canonical effective-set resolver
(`session_population`); Reject & Rescan (`scan_lifecycle.reject_scan`,
`confirm_replacement`, `remove_replacement`, lineage history);
`close_scan_session` / `reopen_scan_session` with their audit and stale-output
marking; held diversion of files for closed sessions.

Missing at `e8200b4` (and the phase 6 carry-forwards, §§25-31 there):

| Gap | Phase 6 state |
|---|---|
| Quality interpretation | Evidence stored (`registration_failed`, `image_unreadable`, page-geometry `scan_quality` assessment in `result_json`), never mapped to a decision |
| Rescan suggestions across scanners | `possible_rescans` per rejected scan; no provenance (source, arrival relative to the rejection) |
| Held / unreadable / unsupported files | No exit: no operator decision existed |
| Pause | In memory; a restart resumed everything |
| Finish current / cancel queued | Mechanics only (`cancel_queued`, `shutdown(drain=)`), no persisted intent |
| Cross-sheet duplicates | Only at unit end (latency = unit length) |
| Finite Scan vs engine | Nothing prevented both processing one project |
| Lost-worker retry counter | In memory; undocumented whether that was safe |
| Session progress / caught up / closure | No snapshot, no finish validation beyond Phase C's `close_scan_session` checks |

## 3. Quality decision architecture

* **Policy:** `domain/quality_decision.py` `QualityPolicy` - versioned data:
  `version`, `rules` (reason -> decision), ordered `rejection_reasons`
  (reason / issue code -> the suggested `RejectionReason`), `validated`, `name`,
  `note`. `decide(evidence)` is pure and deterministic: the highest-ranked
  decision over the evidence's reasons (`ACCEPT` < `ACCEPT_WITH_WARNING` <
  `RETRY_PROCESSING` < `RESCAN_REQUIRED`).
* **Version / fingerprint:** `DEFAULT_POLICY.version = 1`; fingerprint =
  SHA-256 of canonical JSON (sorted keys). Every `scan_quality_decision` row
  stores the fingerprint that produced it.
* **Session pinning:** the first decision of a session pins the policy in
  `scan_session.quality_policy_json` / `quality_policy_fingerprint`; never
  replaced; a stored policy this build cannot read is refused, not swapped.
* **Exact mapping (`DEFAULT_POLICY`, UNVALIDATED DEFAULT):**

| Evidence (reason) | Decision |
|---|---|
| Image not decoded | `RESCAN_REQUIRED` |
| Registration failed | `RESCAN_REQUIRED` |
| Page-geometry assessment `UNUSABLE` | `RESCAN_REQUIRED` |
| Assessment `REVIEW` | `ACCEPT_WITH_WARNING` |
| `GEOMETRY_NOT_VERIFIED` | `ACCEPT_WITH_WARNING` |
| Unknown / unreadable evidence | `ACCEPT_WITH_WARNING` |
| Processing error | `RETRY_PROCESSING` |
| Template error | `RETRY_PROCESSING` |
| Alignment warning only | `ACCEPT` |
| Quality not evaluated | `ACCEPT` |

  On a page that failed to decode, to process or to register, geometry reasons
  are suppressed (a real registration failure also carries an `UNUSABLE` /
  `GEOMETRY_NOT_VERIFIED` assessment; counting it twice would only rename the
  same fact). Suggested rejection reasons, first match: not decoded -> poor
  quality; registration failed -> registration; `partial_page` -> clipped;
  page-geometry distortion / region registration error -> folded; marker
  geometry error -> registration; critical region unreadable -> ID unreadable;
  otherwise unusable -> poor quality.
* **Why unvalidated:** the mapping is ARCHITECTURE_NOTES §12 as written. No
  threshold was added or changed (`ScanQualityThresholds`, fold, displacement
  and mark thresholds untouched); the layer only interprets existing verdicts.
  Whether *this* mapping suits real scanners and real paper has not been
  measured. `DEFAULT_POLICY.validated is False` and the label travels with
  every pinned copy.
* **Where decisions are written:** `record_results` (finite and continuous
  paths) inside the sheet's work unit; `evaluate_stored` back-fills sheets
  recorded before migration 17 (idempotent, at engine start).

## 4. Suggested rejection / rescan architecture

* **Representation:** a `RESCAN_REQUIRED` row in `scan_quality_decision`. It
  creates no `scan_rejection` row and changes no effective set. Outstanding
  while the sheet is active, not dismissed, and its own evidence conflict
  (`registration_failed`, `image_unreadable`, `scan_quality`) is not resolved
  by a human. One SQL predicate (`quality_decisions.outstanding_clause`) is the
  definition everyone uses.
* **Confirmation path:** `quality_decisions.confirm_suggestion` ->
  `scan_lifecycle.reject_scan` (named reviewer, the suggested reason
  pre-filled, overridable, provenance note). `dismiss_suggestion` records an
  audited dismissal. **Never automatic** - no code path rejects, supersedes,
  replaces or purges on a decision.
* **Candidate ranking:** `scan_lifecycle.session_possible_rescans(db, session)`:
  candidates of every rejected-awaiting-rescan sheet in the session, ranked by
  agreeing set code, then *arrived after the rejection*, then newest. Ranking
  only: every candidate is offered; none is linked without
  `confirm_replacement`.
* **Cross-source:** each `ReplacementCandidate` carries `source_id`,
  `source_label`, `arrived_at`, `arrived_after_rejection` and a `provenance`
  string. A rescan from another scanner is ranked like one from the same
  scanner.
* **Lineage:** unchanged phase 2 lineage (`REJECTED -> REPLACED ->
  REPLACEMENT_REMOVED` history, audit events); verified across restarts in the
  scenario (§12).
* **Held / unreadable / unsupported files** (`services/intake_decisions.py`):
  `release` (held -> ready, only into an **open** session on a watched source;
  re-verified before registration), `retry` (unreadable -> stabilizing),
  `dismiss` (-> ignored). Named operator, audited, through
  `OPERATOR_TRANSITIONS` in the ledger state machine.

## 5. Persistent controls

* **Persisted:** `scan_session.intake_paused`, `scan_session.processing_intent`
  (`running` / `paused` / `stopped`), `intake_source.intake_paused`. Each change
  is one transaction with its audit event (entity `session_control`).
* **Restart semantics:** the engine reads the controls on every step. `start()`
  resumes recognition only when the stored intent is `running`; `paused` and
  `stopped` survive restarts (real-kill tested).
* **Pause / resume:** intake (session-wide or per source) and processing
  independently. Paused intake stops new registration; already registered
  units still run unless processing is paused. Paused processing stops new
  claims; in-flight sheets finish and commit.
* **Finish Current** (`engine.finish_current_and_stop`): persists `stopped`
  first, then stops intake and claims and drains everything already claimed to
  committed results and finished units.
* **Cancel Queued** (`engine.cancel_queued_and_stop`): persists `stopped` and
  records a named, audited `queue_cancelled`, withdraws not-yet-started sheets
  to `pending` (never deleted), lets running sheets finish. A crash during
  either drain comes back `stopped`.

## 6. Incremental conflicts

**Changed.** Phase 6 found cross-sheet duplicate Student IDs only at unit end.

* **New boundary:** after each work-unit commit group the engine runs the
  existing `review_store.sync_duplicate_identifiers_for` for that group's
  sheets, in its own short transaction. Within-sheet conflicts were already in
  the work unit.
* **Bounded:** the pass touches only the identifier groups of the committed
  sheets (phase 4's bounded routine); no session-wide scan per commit.
* **Recovery:** a kill between a commit and its pass leaves the unit `running`
  - the existing ADR-0006 marker that its batch-scope pass is owed;
  `recover_on_open` completes it from stored results without re-reading any
  sheet (real-kill tested). The unit-end pass is **retained** as the
  backstop; while a unit is `running` the snapshot never reports caught up and
  finish-session reports `units_running`. Human-touched conflict records are
  kept, as always.
* The finite Scan stage is unchanged (unit-end pass).

## 7. Coordinator ownership

`services/coordinator.py`: a process-local lease keyed by the normalised
project database path; kinds `finite_scan`, `continuous_engine`,
`session_finish`. The finite Scan page takes it **before** registering or
claiming (`ScanPage._ensure_batch`) and releases it after the batch has left
`running` (finished, failed, shutdown and every failure path). The engine
takes it in `start()` before recovery and releases it at shutdown, fault or
abandonment. A second coordinator gets `CoordinatorBusyError` (holder kind,
label); the Scan page shows its user message and starts nothing.

Process-local suffices because the project lock already refuses a second
process. Owners are weakly referenced, so a garbage-collected owner cannot
hold the project; nothing is stored in the project, so nothing can outlive a
crash (real-kill tested: a restarted process starts immediately).

## 8. Worker retry-state decision

```text
intentionally ephemeral
```

Evidence (`tests/integration/test_phase7_recovery.py`): a restart resets the
counter, granting a sheet at most `1 + infrastructure_retries` more
submissions per process start; a result is written only onto a row still
`processing` (`claimed_only=True`) and a committed row is terminal, so repeated
starts produce no duplicate result, conflict, audit event or effective-set
change; a lost worker's placeholder is a software fault, never a rescan
suggestion. Persisting it would add a write per retry and protect nothing
correctness depends on. Residual: a sheet that kills its worker every time
costs a bounded amount of work per restart (§15).

## 9. Session snapshot

`services/session_snapshot.take_snapshot(db, session_id, now=...)` returns an
immutable `SessionSnapshot` from 11 bounded SQL statements in one read
transaction.

* **Partition** (every discovered, non-ignored file or sheet in exactly one
  bucket; `partition.total == discovered`, asserted by tests): stabilizing,
  ready, held, vanished, unreadable_pending_decision | queued, processing |
  accepted, conflict, rescan_required | superseded, duplicate, excluded,
  deferred, counted_elsewhere.
* **Recognition progress:** processed / (discovered - duplicate - vanished); may
  decrease as files arrive.
* **Conflict progress:** resolved / required conflicts on effective sheets.
* **Rescan progress:** confirmed replacements / (rejected awaiting rescan +
  replaced + unanswered suggestions).
* **Caught up** (`SessionActivity.CAUGHT_UP`, "Caught up - watching for new
  scans", never "complete"): open session, processing `running`, intake not
  paused, nothing stabilizing / ready / queued / processing, no unit `running`,
  and every enabled unpaused source reconciled successfully within
  `2 x its poll interval + 5 s` (`CaughtUpPolicy`, uncalibrated). Otherwise the
  first matching activity of: closed, processing stopped, processing paused,
  waiting for source, processing, intake paused, checking sources.
* **Reachability:** per-source state, last successful reconciliation, recent
  rate; an unreachable enabled, unpaused source gives *waiting for source*.
* **Rate alarm:** per source, over its latest 20 read sheets, raised when at
  least 5 were read and at least half failed registration
  (`RegistrationAlarmPolicy`; a wrong-template signal, uncalibrated, not a
  recognition threshold).

## 10. Finish-session validation

`services/session_finish.finish_scan_session(db, session, closed_by=, intake=,
acknowledge=, reason=)` (and `engine.finish_session`): runs a final
reconciliation of every enabled source attached to the session (paused
included; disabled sources listed by name, never treated as checked), then
returns **every** blocker as a typed code with a count, or closes through the
existing one-transaction `close_scan_session`.

| Blocker | Acknowledgeable |
|---|---|
| `session_not_open` | no |
| `processing_active` (another coordinator holds the project) | no |
| `files_stabilizing`, `files_ready` | no |
| `sheets_queued`, `sheets_processing`, `units_running` | no |
| `unresolved_conflicts` | no |
| `rescan_suggested` (unanswered suggestions) | no |
| `files_awaiting_decision` (held / unreadable / unsupported) | no |
| `source_unreachable`, `source_not_reconciled` | no |
| `rescan_outstanding` | **yes** |
| `replacement_unmatched` | **yes** |
| `sheets_deferred` | **yes** |

Acknowledgeable blockers are passed only by an explicit `IncompleteAcceptance`
naming the operator - the same audited decision as *Export incomplete
results*. No `force`, no quiet-period close. `rescan_suggested` co-occurs with
`unresolved_conflicts` when the suggestion's evidence conflict is open
(resolving it answers both). **Reopen** (`reopen_session`): named, audited, one
transaction; outputs generated while closed read stale; held files stay held
until released; closing again re-runs every check.

**One closure policy.** `finish_scan_session` is the only definition of
whether a session may become `closed`. Every operational close goes through
it: the engine's `finish_session`, Reports' one-step *Close session and
generate final export*, and the Scan stage's *Close scan session* (both GUI
paths via `gui/session_close.py`, which only supplies the intake service for
the final reconciliation and renders the typed blockers as the existing
dialogs' sentences). `close_scan_session` is the commit primitive underneath;
its Phase C re-check is a last line inside the transaction, not a second
policy, and no production code calls it directly any more. Reports still asks
its own, separate question first - *can these sets' final reports be
generated?* (template, set blockers) - so that it never closes a session for
an export that could not run; it does not re-derive any closure blocker (§14a).

## 11. Crash / restart evidence

`tests/crash/test_phase7_kills.py` - a separate child process
(`tests/crash/phase7_child.py`) killed with `TerminateProcess` at an exact
state from its external evidence log; checks from outside. **7 cases, all
passing:**

| Kill point | Checked after restart |
|---|---|
| Inside the work-unit transaction of a sheet whose decision is a suggested rescan | No half decision; exactly one suggestion after recovery; nothing rejected; no orphan processes |
| After a commit, before its cross-sheet duplicate pass | Unit still `running`; recovery raises the duplicate without re-reading the sheet |
| Inside the confirmed rejection transaction | All or nothing; audited once |
| Inside the replacement confirmation transaction | All or nothing; audited once |
| Inside close | Session exactly as before |
| Inside reopen | Session exactly as before |
| Persisted pause, kill; then running, kill | Still paused after restart; `running` intent resumes; no lease outlives the process |

Plus `tests/integration/test_phase7_recovery.py` (retry counter, restarts) and
the scenario's injected kill (§12).

## 12. Synthetic validation

`tests/integration/test_phase7_scenario.py` (default suite, ~60-70 s): real
synthetic sheets (stress generator, seed 42) read by the real recognition
engine through the continuous engine on a fake filesystem.

* Three watched sources, round-robin; units of at most 40; 12 finite units.
* Wave 1: 194 clean sheets (the first 200 decodable, less the 6 used for damage) + 9 damaged (3 blank pages, 4 displaced identifier
  blocks, 2 erased identifiers - `tests/quality_rig.py`, simulated damage).
  Every damaged sheet suggested with the expected reason (folded /
  registration / ID unreadable); nothing rejected automatically.
* Operator confirms 4 suggestions, dismisses 1.
* Wave 2: 100 sheets + 3 rescans at **other** scanners; the engine killed after
  30 committed sheets, restarted.
* 3 rescans offered as candidates, arrived after rejection, with source labels;
  the 4th rejected sheet has none. Operator confirms 3, undoes 1. Lineage and
  counts verified, then verified again after 2 restarts.
* Wave 3: the remaining 35 decodable sheets (335 of the 340 generated decode);
  intake continues.
* Final: **341 files written, 338 distinct images** (the generator's exact
  duplicates are linked as duplicate content), **334 effective scripts**
  (distinct - 4 rejected originals), duplicate Student IDs found across at
  least 2 units, all 4 rejections by the named operator, snapshot recognition
  1.0, nothing queued or processing.

Printed by the run of 2026-10-04 (62.7 s):

```text
Phase 7 scenario: 341 files written, 338 distinct images, 334 effective scripts, 12 units,
partition {stabilizing 0, ready 0, held 0, vanished 0, unreadable_pending_decision 0,
queued 0, processing 0, accepted 304, conflict 26, rescan_required 6, superseded 2,
duplicate 3, excluded 0, deferred 0, counted_elsewhere 0}
```

## 13. Performance

* **Snapshot** (`scripts/benchmark_session_snapshot.py`, metadata rows, this
  machine, median): 35 ms at 10,200 rows, 86 ms at 51,000, 154 ms at 102,000;
  11 statements regardless of size. Before restructuring: 577 ms at 51,000.
  `tests/integration/test_snapshot_scale.py` re-run on 2026-10-04: 42 ms at
  10,200, 11 statements.
* **Contention** (`tests/integration/test_phase7_contention.py`: engine
  processing while another thread performs operator actions and polls
  snapshots; `busy_timeout` 5,000 ms), re-run on 2026-10-04: 750 operator
  actions, median 12.9 ms, worst 193.5 ms; 213 snapshots, median 82.4 ms,
  worst 313.7 ms. No `database is locked`. (An earlier version deadlocked:
  the snapshot opened a second connection inside its read transaction - fixed
  in `3973564`; the rule is now "no other connection inside a read
  transaction" in the snapshot, `record_results` and `pending_decisions`.)
  Re-run at the post-correction tip (`69b5e8d`, 2026-10-04, while no other
  test ran): 462 operator actions, median 27.8 ms, worst 233.3 ms; 147
  snapshots, median 113.6 ms, worst 451.3 ms; no `database is locked`.
  `test_snapshot_scale` at the same tip: 53 ms at 10,200 rows, 11 statements.
  The spread between runs is this machine's load, not a code change.
* **Journal mode (corrected before merge).** OMRFlow keeps SQLite's default
  **rollback journal**; it does not use WAL (`database/engine.py`, ADR-0002 -
  unchanged by phase 7). An earlier version of this handoff said "the writer is
  not blocked in WAL mode"; that was wrong. In rollback-journal mode a read
  transaction holds a SHARED lock, so a writer that is ready to commit waits
  (up to `busy_timeout`, 5,000 ms) until open readers finish: the snapshot's
  read transaction *can* briefly contend with the engine's or an operator's
  commit. What bounds that contention: the snapshot runs a fixed number of
  queries (11, independent of size) and its measured duration is tens of ms to
  ~150 ms at 100k rows (benchmark above). The contention test above observed no
  `database is locked` and the operator / snapshot latencies listed. These are
  automated measurements on this machine's local disk - **not** network-share
  qualification, where lock latency is different and untested.

## 14. Full test results

Baseline (`main` `e8200b4`, canonical `pytest-ruff-mypy.ps1`): 6,919 passed,
16 skipped, 7 deselected; ruff clean; mypy clean (222 files).

Phase 7 (`80ee036`, canonical `pytest-ruff-mypy.ps1` run from the worktree
with the main checkout's `.venv`, log `Scratch\Log\2026-10-04_022249`):

| Check | Result |
|---|---|
| pytest (default suite) | **7,007 passed, 29 skipped, 7 deselected** (1 h 04 min 45 s) |
| ruff `check src tests tools scripts` | All checks passed |
| mypy `src` | Success: no issues found in 232 source files |
| Stress (`-m stress`, separately) | **7 passed** (21 min 49 s): crash matrix case 11 at 1,000 sheets, 10k-scan progress model, engine endurance (waves + kills), intake soak, three full-size sets, kill/resume at 10,000, reconciliation + reports at 10,000 |

* **Skips vs baseline (29 vs 16):** the 13 extra are
  `tests/local/test_real_marked_sheets.py`, which need the gitignored
  `Scratch/Project1` fixture the worktree lacks. Run against it (temporary
  junction, removed afterwards): **13 passed**. The 10
  `test_real_folded_corner.py` skips need `Scratch/Sample Scripts`, absent in
  the main checkout too.
* **+88 passed** over the baseline: the phase 7 tests.
* **One earlier gate run failed:** run `2026-10-04_013209` of the same commit
  died with a native abort (exit `0xC0000409`, no Python traceback or Qt
  message captured) at about 18 %, inside
  `tests/gui/test_stress_qualification_gui.py` (a pre-existing file phase 7
  does not touch). That file alone: 37 passed; the whole `tests/gui`
  directory: 1,923 passed, 2 skipped; the next full run (above) passed. **Not
  reproduced, cause not identified** - recorded as an intermittent native GUI
  crash, not as a pass.
* Edited pre-existing tests (not weakened):
  `tests/integration/test_continuous_engine.py::test_status_snapshot_and_idempotent_start`
  - a second engine on the same project is now refused by the coordinator
  lease (`CoordinatorBusyError`) where it used to start; the test asserts the
  refusal, shuts the first engine down and keeps every original assertion.
  `tests/integration/test_intake_migration.py` - pinned `SCHEMA_VERSION == 16`
  and a `-to-16` backup name; now `>= 16`, `schema_version == SCHEMA_VERSION`
  and `-to-{SCHEMA_VERSION}`, so the schema-15 -> current upgrade it tests
  still runs in full.

## 14a. Pre-merge corrections (review of `94241e0`)

**Defect found and fixed before merge - alternate close path.** A pre-merge
review found that Reports' one-step *Close session and generate final export*
(`ReportsPage._ensure_closed_for_final`) still decided closure with
`scan_sessions.closure_blockers` and closed with `close_scan_session`
directly - the Phase C checks only. It bypassed the final source
reconciliation and the phase 7 blockers (held / unreadable / unsupported files,
unanswered suggested rescans, unreachable sources, stabilising / ready files,
coordinator ownership, unmatched replacements), so the application had two
definitions of "may this session close". The Scan stage's *Close scan session*
(`ScanPage.close_active_scan_session` / `_prompt_close_scan_session`) had the
same bypass. Recorded here as a phase 7 integration blocker, not phase 8 GUI
work.

Fix (`7f69c13`): both GUI paths now close through `finish_scan_session` via
the new `gui/session_close.py`, which decides nothing - it builds the intake
service the final reconciliation needs (`IntakeService(..., recover=False)`)
and renders the typed `FinishBlocker`s as the sentences the existing dialogs
list. The dialogs and workflow are unchanged. Reports first answers its own
question, *can these sets' final reports be generated?* (set blockers and
non-acknowledgeable readiness issues), and if not, closes nothing and lists
those together with the session's blockers from `finish_blockers` (the
read-only preview of the same policy). Otherwise it calls
`finish_scan_session`; when every blocker is acknowledgeable (outstanding
rescan, unmatched replacement, deferred) it asks the existing *Close and export
incomplete results* question and, on acceptance, calls it again with an
`IncompleteAcceptance` naming the operator (audited in the close event). No
operator name -> nothing closed. No force path. No production code calls
`close_scan_session` directly any more.

Tests: `tests/gui/test_final_export_finish_policy.py`, 11 cases, all passing.
A spy wraps (does not replace) `session_finish.finish_scan_session`, so each
test runs the real service and asserts the page went through it:

| Case | Result |
|---|---|
| Held (unreadable) file awaiting decision - Phase C checks alone find nothing | refused, `files_awaiting_decision`, session open |
| Unanswered suggested rescan | refused, `rescan_suggested` listed, open |
| Enabled source unreachable at final reconciliation - Phase C checks find nothing | refused, `source_unreachable` naming the source, open |
| Queued work with the engine still holding the lease; then engine stopped | refused `processing_active`; then refused `sheets_queued` / `sheets_processing`, open |
| Outstanding rescan, operator cancels the incomplete-results question | open; one finish call |
| Outstanding rescan, named acceptance | closed; `accepted == [rescan_outstanding]`; audit: reviewer, "incomplete results accepted", reason |
| No operator name | nothing closed; *Scan session not closed* warning |
| Clean continuous session | closed by one finish call; source reconciled |
| Finite workflow: one step closes and exports | closed through the service; final export *current* |
| A set that cannot export | service not called; set blocker and session blockers listed; open |
| Scan stage *Close scan session* with a held file | refused through the same policy, open |

Against the unfixed `ReportsPage` 9 of the 10 Reports cases fail (the tenth
checks the report-side pre-check, which the old code also had). The existing
`test_close_and_export_gui.py`, `test_reports_page.py` and
`test_scan_session_gui.py` pass unchanged.

**Documentation correction (`d64a623`).** §13 said the snapshot's read
transaction does not block the writer "in WAL mode". OMRFlow uses SQLite's
rollback journal, not WAL (`database/engine.py`, ADR-0002, unchanged); §13 now
says so and what bounds the contention. `docs/wiki/Project-Format.md` (older
than phase 7) also claimed WAL; corrected.

**Finding - mypy is not clean with a fresh cache.** The post-fix canonical
gate reports 2 errors in `services/scan_lifecycle.py` (lines 2433, 2452:
`int()` of a `Mapped[int | None]` column). Those lines date from `cf9b05b`
(2026-09-29) and are untouched by phase 7; the same 2 errors appear on the
main checkout (222 files) with a fresh `--cache-dir` and either environment's
mypy (2.3.1 / 2.4.0). The "mypy clean" results recorded in §14 for the
baseline and `80ee036` were therefore most likely produced from a warm cache
(not verified). Not fixed in that pass (pre-existing, outside its scope);
fixed afterwards in `e8d49a1` - §14c.

## 14b. Post-correction validation (tip `69b5e8d`, 2026-10-04)

Worktree `OMRflow-phase7` with its own `.venv` (mypy 2.4.0, ruff 0.16.10,
SQLAlchemy 2.1.3, pytest 9.1.1, PySide6 6.11.2).

| Check | Result |
|---|---|
| Targeted (finish policy, Reports / close-and-export, Scan session, reject-rescan GUI, coordinator GUI, `test_session_finish`, `test_coordinator_ownership`, phase 7 recovery and scenario, quality / rescan, reject-and-rescan, reports, quality-decision and rescan rules) | **302 passed** (22 min 05 s) |
| Canonical `pytest-ruff-mypy.ps1`, log `Scratch\Log\2026-10-04_104600` - pytest | **7,020 passed, 27 skipped, 7 deselected, 0 failed** (1 h 48 min 19 s) |
| ruff `check src tests tools scripts` | All checks passed |
| mypy `src` (233 files) | **2 errors**, both pre-existing in `scan_lifecycle.py` (§14a) - none in phase 7 or correction code; fixed in §14c |
| Stress `-m stress` | **7 passed** (14 min 47 s) |

* +13 passed vs `80ee036`: the 11 new tests, plus 2 fewer skips in this
  environment (27 vs 29). Not investigated further.
* **Native crash `0xC0000409` did not recur** in this full run. It remains
  unexplained; nothing in this pass addresses it and it is not claimed fixed.
  Evidence so far: one abort in run `2026-10-04_013209`; full runs
  `2026-10-04_022249` and `2026-10-04_104600` and the isolated GUI runs passed.
* An earlier attempt at this gate (`2026-10-04_095853`) stopped at about 31 %:
  it was launched under Windows PowerShell 5.1 with all output redirected
  (`*>`), which turned an OpenCV stderr warning (`PngDecoder::read_chunk`) into
  a terminating PowerShell error. A harness artefact, not a test failure or a
  native crash; that attempt also caught a ruff line-length error in the new
  test (fixed in `69b5e8d`).

## 14c. Inherited mypy errors fixed (`e8d49a1`)

The two errors were inherited from pre-phase-7 `main` (lines from `cf9b05b`,
2026-09-29; the same code is on `origin/main` at `scan_lifecycle.py`
2296 / 2315) and surfaced only because the final gate ran mypy with a fresh
cache. Exact diagnostics (mypy 2.4.0, fresh `--cache-dir`, at `d0d1949`):

```text
src\omr_scanner\services\scan_lifecycle.py:2433: error: Argument 1 to "int" has
incompatible type "int | None"; expected
"str | Buffer | SupportsInt | SupportsIndex | SupportsTrunc"  [arg-type]
src\omr_scanner\services\scan_lifecycle.py:2452: error: Argument 1 to "int" has
incompatible type "int | None"; expected
"str | Buffer | SupportsInt | SupportsIndex | SupportsTrunc"  [arg-type]
```

Fix: `adopted_replacements` and `counted_elsewhere` selected the nullable
`ScanRejection.replacement_scan_id` (`Mapped[int | None]`); they now select
`BatchScan.scan_id` (`Mapped[int]`, primary key). Both queries already
inner-join `batch_scan ON batch_scan.scan_id = scan_rejection.replacement_scan_id`,
so the two columns are equal on every returned row; the compiled FROM / JOIN /
WHERE is identical before and after (checked), only the first selected column
changes, and rows are unpacked by position. No `cast`, no `type: ignore`, no
configuration change; runtime behaviour unchanged, so the 7/7 stress result
(§14b) stands without a rerun.

Validation at `e8d49a1`:

| Check | Result |
|---|---|
| Focused lifecycle tests (`test_reject_and_rescan`, `_followup`, `test_set_scoped_reconciliation`, `test_multi_set_workflow`, `test_reject_rescan_rules`, `test_session_population`, `test_quality_rescan`) | **173 passed** |
| mypy `src`, fresh cache | **Success: no issues found in 233 source files** |
| Canonical `pytest-ruff-mypy.ps1` (project `.mypy_cache` deleted first), log `Scratch\Log\2026-10-04_132453` | **OVERALL PASS**: pytest **7,020 passed, 27 skipped, 7 deselected, 0 failed** (1 h 52 min); ruff all checks passed; mypy no issues (233 files); git state unchanged |
| Stress | 7/7 passed at `69b5e8d` (§14b), retained |

The handoff text in this section was added to the fix commit after that gate
run; the source code it validated is identical.

## 15. Known limitations

* **Headless.** No GUI for any of it (phase 8).
* **Quality defaults unvalidated.** The mapping, the suggested reasons, the
  caught-up allowance and the registration-alarm window are starting values,
  not calibrated on real scanners or paper.
* **Poison sheet:** a sheet that kills its worker every time is retried a
  bounded number of times per process start (retry counter ephemeral, §8);
  each restart repeats that bounded work. No quarantine.
* **Simulated damage only:** blank, displaced and erased sheets are synthetic;
  real folds, skew, clipping and scanner artefacts not exercised here.
* **Fake filesystem** in the scenario; no SMB / network share, no real
  scanner, no power loss (process kills only), no 10k campaign, no 100k
  qualification.
* The snapshot is consistent within one read transaction but uses data read
  just before it (sources, controls, linked sheets) - a change landing between
  the two is visible on the next poll.
* Coordinator lease is process-local by design (§7).

## 16. Phase 8 contract

The operational GUI may rely on (all headless, tested):

* `session_snapshot.take_snapshot(...) -> SessionSnapshot` - immutable;
  `partition` (sums to discovered), `recognition` / `conflicts` / `rescans`
  progress, `activity` + `activity.label`, per-source `SourceSnapshot`
  (reachability, last reconciliation, rate, registration alarm),
  `outstanding_suggestions`, `controls`. Cheap enough to poll about once a
  second (§13).
* `session_controls`: `get_controls`, `set_intake_paused`,
  `set_source_paused`, `pause_processing`, `resume_processing`,
  `request_finish_current`, `record_cancel_queued` - named, audited,
  persisted.
* Engine: `ContinuousEngine.start / step / shutdown`,
  `finish_current_and_stop`, `cancel_queued_and_stop`, `finish_session`,
  `controls()`, `status()`; `StartupReport`; `CoordinatorBusyError` (typed
  holder) from either coordinator.
* `quality_decisions`: `outstanding_suggestions` (paged), `count_outstanding`,
  `decisions_by_scan`, `decision_counts`, `confirm_suggestion`,
  `dismiss_suggestion`, `session_policy` (shows the pinned policy and its
  UNVALIDATED label).
* `scan_lifecycle.session_possible_rescans` (ranked, with provenance) plus the
  existing `confirm_replacement` / `remove_replacement`.
* `intake_decisions`: `pending_decisions`, `count_pending`, `decide_file`,
  `OPTIONS`.
* `session_finish`: `finish_blockers` (dry run), `finish_scan_session`
  (`FinishOutcome`: closed, or every typed blocker with counts),
  `IncompleteAcceptance`, `reopen_session`. The GUI renders blocker codes; the
  core returns no sentences.

The GUI must not: reject, replace or close on its own; treat *caught up* as
*complete*; bypass `finish_scan_session` for the continuous workflow; run the
finite Scan stage and the engine at once (the lease will refuse it).
