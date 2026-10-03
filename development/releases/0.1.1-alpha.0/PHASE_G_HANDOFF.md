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
  performance, then the documentation commit(s). Tip: see the final report.
* Push / merge: see the final report. **Not merged** into `main`; nothing
  tagged or released.
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
until released; closing again re-runs every check. `close_scan_session` keeps
exactly its Phase C checks (the finite *Close session and generate final
export* path is unchanged - §15).

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
* The snapshot holds a SQLite read transaction for its duration (tens to
  ~150 ms at 100k rows); the writer is not blocked in WAL mode.

## 14. Full test results

Baseline (`main` `e8200b4`, canonical `pytest-ruff-mypy.ps1`): 6,919 passed,
16 skipped, 7 deselected; ruff clean; mypy clean (222 files).

Phase 7 tip: filled in from the final run (below).

## 15. Known limitations

* **Headless.** No GUI for any of it (phase 8).
* **Quality defaults unvalidated.** The mapping, the suggested reasons, the
  caught-up allowance and the registration-alarm window are starting values,
  not calibrated on real scanners or paper.
* **Finite one-step close** (*Close session and generate final export*,
  Reports) still uses `close_scan_session`'s Phase C checks, not
  `finish_scan_session` - deliberate, to leave the finite workflow unchanged;
  phase 8 should route the operator's close through `finish_scan_session`.
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
