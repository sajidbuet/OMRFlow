# `0.1.1-alpha.0` roadmap — scan sessions, multi-source intake and set identity

> **The single authoritative plan for `0.1.1-alpha.0`.** Status: **in
> implementation.** Reconciled 2026-09-30. Revised phases 1-5 (set identity;
> scan sessions and finite batches; crash-safe persistence; session-level
> effective results; intake sources and ledger) are implemented, tested and
> merged into `main` (latest `59ba8df`; handoffs A-E). Revised phase 6 (the
> headless continuous-processing engine, roadmap E first part) is implemented
> and tested; merged as `141d703`
> ([PHASE_F_HANDOFF.md](PHASE_F_HANDOFF.md)). Phases 7-10 are not
> implemented; nothing is released. The implementation sequence is the revised
> ten-phase split in §5.1.
>
> Written against `main` at `128512d` (code as of `c30e809`, **schema 12**).
> It reconciles and replaces two earlier plans (§11). Canonical project status
> lives in [`docs/wiki/Development-Roadmap.md`](../../../docs/wiki/Development-Roadmap.md);
> this is the working detail for the release.

Companion documents: [ARCHITECTURE_NOTES.md](ARCHITECTURE_NOTES.md) (what
exists, the model, the reasons), [ACCEPTANCE_CRITERIA.md](ACCEPTANCE_CRITERIA.md)
(what "done" means), [prompts/](prompts/) (one implementation prompt per phase).

---

## 1. Release objective

Move OMRFlow from a *finite, single-folder, single-sitting* workflow to one
that stays correct when an examination is scanned **by several scanner
stations, over hours, with files arriving while processing runs, the
application restarted in between, and rejected scripts rescanned later** —
while fixing the set-code identity problems and the six latent defects found
during `0.1.0-alpha.2` validation.

```text
scanners write image files  ──►  OMRFlow discovers, registers and processes them robustly
```

OMRFlow does not control scanners, does not become a server, and does not let
anything but itself write its database.

---

## 2. Architecture

```text
Project → ScanSession → one or more finite ScanBatch objects → sheets
```

| Level | Role |
|---|---|
| **Project** | Persistent overall project/configuration container |
| **ScanSession** | Operational / examination-level **aggregation** unit. May stay open while further finite batches are created from one or more scanners, folders, computers, network shares or later rescans |
| **ScanBatch** | Finite **processing / provenance** unit: one import or intake unit from one source; membership fixed once sealed; resumable, fingerprint-checked, auditable — Phase 5 semantics kept. **Never an indefinitely growing container** |

- **Traditional finite scanning** — one session holding one batch, created
  implicitly. *Add Folder → Process All → Resolve → Attendance → Answer Key →
  Results → Reports* works exactly as today; the operator sees no session
  management unless they ask for it.
- **Continuous / multi-scanner scanning** — one session holding many sealed
  batches: per scanner, per intake unit, per manual import, per rescan run.
  New batches may be added while the session is open.
- **Results:** Attendance, reconciliation, scoring, Results and final Reports
  have one **authoritative session-level view** over the session's *effective
  scan set*. **Batch-level views** remain for provenance, debugging and
  operational monitoring, and are never presented as a multi-batch session's
  final result.

**Lifecycle invariants** (semantic; the eventual names may differ —
ARCHITECTURE_NOTES.md §§5.2, 6.1):

| | Invariant |
|---|---|
| `ScanBatch` OPEN | may receive members |
| `ScanBatch` SEALED | may not receive new members |
| `ScanBatch` SUPERSEDED | remains auditable; excluded from effective session aggregation |
| `ScanSession` OPEN | may receive new finite batches |
| `ScanSession` CLOSED | may not receive new batches or intake; all contributing batches sealed; final outputs represent its authoritative state |
| `ScanSession` REOPENED | explicitly returned to an editable, open state; **invalidates prior final-export status**; reopening and re-closing are audit events |

**Supersession is first-class** (ARCHITECTURE_NOTES.md §5.5): *Only effective,
non-superseded batch/sheet membership contributes to session-level attendance,
reconciliation, scoring, results and final reports. Superseded batches remain
retained for provenance and audit but do not contribute to effective totals.*
It covers *Reprocess All*, replacement/rescan batches where appropriate, future
algorithmic reprocessing, and reopening or recomputing a session. Nothing is
deleted because it is superseded.

**Crash-safe persistence and resume is required** (ARCHITECTURE_NOTES.md §13.2–13.8):
after any interruption — crash, forced termination, OS restart, power failure,
normal close — reopening the project preserves all durably completed Scan and
Resolve work, resumes the existing session and batch (never a new session, batch
or superseding batch), and reconstructs progress from committed records.
**Durable-completion invariant:** once OMRFlow reports a sheet as successfully
recognised/completed, its recognition result and all review/conflict state
needed for Resolve are already durably committed. Current `main` has the
persistence primitives but not the complete recovery workflow: defects **S1,
S2, S3 and R1 must be closed before `0.1.1-alpha.0` is released** (Phase B).

Full model, invariants, and the reasons: ARCHITECTURE_NOTES.md §§1, 5, 6, 8, 13.

---

## 3. Baseline

**Released:** `v0.1.0-alpha.2` (tag, 2026-09-24).

**Working baseline:** `main`, unreleased work past that tag, all of which this
line preserves: Reject & Rescan (migration 11) and its cross-batch hardening,
the Attendance reconciliation workstation with per-set rosters and
set-scoped reconciliation, the Answer Key rework with answer-key provenance
(migration 12), solution-sheet and answer-key generation, synthetic write-ins,
the GUI fixes for answer-key verification and Calculate Results, the
zoomed-preview fix, and the Results **Dashboard** tab (2026-10-01; read-only
analytics over the Results rows, no schema change — implemented, tested,
scripted in the real window, not operator-validated). `CHANGELOG.md` `[Unreleased]` must describe all of it before
any release is cut.

**Schema version 12.** Migration numbers 1–12 are taken; the next is **13**
(ARCHITECTURE_NOTES.md §2.1).

**Latent `0.1.0-alpha.2` defects this release must address**
(ARCHITECTURE_NOTES.md §3): (1) late scans split the cohort; (2) what Results
reads can change silently; (3) duplicate Student IDs detected on machine
readings, per batch; (4) byte-identical images processed twice; (5) set-code
case mismatch between key storage and lookup; (6) `processing_manifest` never
written.

---

## 4. Release sequence and Phase 11B

```text
0.1.0-alpha.2     finite-batch Alpha baseline                     ← released 2026-09-24
      ↓
0.1.1-alpha.0     scan sessions, multi-source intake, set identity ← this plan
      ↓
0.1.1-alpha.1 …   hardening and fixes
```

**Phase 11B policy.** Phase 11B real-data qualification **continues alongside**
development of the `0.1.1` line. It is not replaced by `0.1.1`, and it is not
moved entirely before or after it. Real-data evidence gathered on
`0.1.0-alpha.2` remains valid for what this line does not change (recognition,
calibration, scan-quality evidence, scoring rules); evidence about intake,
sessions and session-level results can only come from `0.1.1` builds. Which
**Beta line (decided):** after the `0.1.1-alpha.x` line the corresponding
Beta line is **`v0.1.1-beta.x`**, unless a later deliberate versioning decision
changes the target release; `v0.1.0-beta.x` is not used as a successor to
`v0.1.1-alpha.0`. (`0.1.0-beta.x < 0.1.1-alpha.0` is ordinary semantic-version
ordering; finding F8 is the separate bug that ranks a Beta *below* the Alphas of
its own base version — ARCHITECTURE_NOTES.md F8, §16 Q10.)

**Gates before the first Beta** (in addition to Phase 11B's own criteria;
ACCEPTANCE_CRITERIA.md §9): a **fresh 100,000-sheet qualification on the
ScanSession / finite-ScanBatch / continuous-processing architecture**, and
**finding F8 fixed and tested** — no Beta tag is created before both. The Beta
is `v0.1.1-beta.x`; no other line is chosen merely to work around F8.

Step 7 (Answer Key) real-data validation also continues in parallel, not as a
feature stream: more real solution sheets, real blanks and double marks,
skewed/noisy/photographed samples, a blank real set field, operator acceptance.

---

## 5. Phase plan

Phase boundaries follow dependencies: set identity first (every stage keys on
it); then the session model (every later phase creates or reads sessions);
then session-level results, so that **no multi-batch session is ever silently
wrong** before intake starts producing many batches; then intake, continuous
processing, the operator GUI, and qualification.

| Phase | Title | Depends on | Proposed migration | Prompt | Status |
|---|---|---|---|---|---|
| **0.1.1-A** | Set identity (and the version bump) | — | 13 (taken) | [01](prompts/01-set-identity.md) | Implemented; tested — [handoff](PHASE_A_HANDOFF.md) |
| **0.1.1-B** | Scan sessions and finite batches | A | 14 (taken) | [02](prompts/02-scan-sessions.md) | Lifecycle part implemented; tested; merged (revised phase 2, [handoff](PHASE_B_HANDOFF.md)). Crash-safety part (S1–S3, R1) implemented; tested, incl. a real-process kill matrix; branch, not merged (revised phase 3, no migration, [handoff](PHASE_C_HANDOFF.md)) |
| **0.1.1-C** | Session-level review, reconciliation, scoring and reporting | B | 15 (taken) | [03](prompts/03-session-results.md) | Implemented; tested; merged (revised phase 4, [handoff](PHASE_D_HANDOFF.md)) |
| **0.1.1-D** | Intake sources and ledger | B | 16 (taken) | [04](prompts/04-intake-ledger.md) | Implemented; tested; merged (revised phase 5, [handoff](PHASE_E_HANDOFF.md)) |
| **0.1.1-E** | Continuous processing, quality decisions and session controls | C, D | none for the first part; 17 for the second | [05](prompts/05-continuous-processing.md) | First part (unit scheduler, writer strategy, restart sequence) implemented; tested; merged as `141d703` (revised phase 6, no migration, [handoff](PHASE_F_HANDOFF.md)). Second part (quality decisions, rescan suggestions, persisted controls, coordinator guard, incremental duplicates, session snapshot, finish-session) implemented; tested; branch `feat/0.1.1-phase7-quality-session-controls`, not merged (revised phase 7, migration 17, [handoff](PHASE_G_HANDOFF.md)) |
| **0.1.1-F** | Operational GUI | E | — | [06](prompts/06-operational-gui.md) | Pending |
| **0.1.1-G** | Qualification and Alpha release | A–F | — | [07](prompts/07-qualification-release.md) | Pending |

C and D both depend on B and may proceed in either order; if D merges first it
takes migration 15 and C takes 16. **Numbers are assigned at merge; a merged
migration is never renumbered.**

### 5.1 Revised implementation sequence (2026-10-01)

The architecture and the semantics above are unchanged. Only the
*implementation boundaries* are split finer, so that each phase is reviewable
and crash safety gets its own phase:

| Revised phase | Scope | From roadmap phase | Status |
|---|---|---|---|
| **1** Set Identity Foundation | Canonical set identity, physical marks, migration 13 | A | Implemented; tested; merged |
| **2** ScanSession + finite ScanBatch lifecycle | Sessions, sealing, roles, supersession, backfill, pinning, manifests, minimal menu, migration 14 | B (lifecycle) | Implemented; tested; merged |
| **3** Crash-safe Scan/Resolve persistence | S1, S2, S3, R1; durable-completion invariant; real-kill matrix (ACCEPTANCE §5.4) | **B (crash safety, moved here)** | Implemented; tested (cases 1–15 with real process kills at 40 sheets; 1/25/50/75/99 % also at 1,000); merged (`0e94d67`); no migration (ADR-0006, [handoff](PHASE_C_HANDOFF.md)) |
| **4** Session-level effective results | Effective scan set, session Resolve queue, reconciliation, scoring, Results, Reports, final export | C | Implemented; tested (unit, integration, GUI; 3-batch / 100+-script acceptance scenario; generated-cohort ground truth; golden one-batch regression against `main`; scripted GUI). Migration 15 (session scope, report scope, lookup indexes). Merged (`fd063f8`). Not operator-validated (ADR-0007, [handoff](PHASE_D_HANDOFF.md)) |
| **5** Intake sources + ledger | Persistent sources and session attachment, authoritative reconciliation, stabilisation with one-read hash + full decode, collision rules, reachability, restart recovery, verified copy ingest, registration API, manual convergence, migration 16 | D | Implemented; tested (fake-filesystem state machine, real temporary directories, real writer and engine process kills, three-source acceptance, 2,400-arrival soak, schema-15 upgrade). Merged (`59ba8df`). Headless. Not network-share or real-scanner validated (ADR-0008, [handoff](PHASE_E_HANDOFF.md)) |
| **6** Continuous-processing engine | Unit scheduler, writer strategy, restart sequence | E (first part) | Implemented; tested (deterministic kill-boundary matrix, real process kills at six boundaries, 1/25/50/75/99 % restart series, repeated-restart torture, finite-path and partitioning equivalence, Attendance/scoring compatibility, bounded backpressure, contention with Resolve decisions, a warm `spawn` pool, a 3,000-file endurance run). Merged as `141d703`. No migration. Headless. Not network-share, real-scanner or power-loss validated (ADR-0009, [handoff](PHASE_F_HANDOFF.md)) |
| **7** Quality / rescan / session controls | Quality decision layer, rescan suggestions, pause/finish semantics, session snapshot | E (second part) | Implemented; tested (pure policy table; real-sheet quality cases; persisted controls across restarts; coordinator lease incl. the finite Scan stage; incremental duplicate sync incl. a kill between commit and pass; snapshot partition / caught-up / alarm; finish-session blockers, close and reopen; Reports' and Scan's existing close actions routed through the finish policy before merge; real process kills at every new durable transition; a 341-sheet three-scanner scenario; contention; schema-16 upgrade). Migration 17. Branch, not merged. Headless. Not network-share, real-scanner or power-loss validated; quality defaults unvalidated (ADR-0010, [handoff](PHASE_G_HANDOFF.md)) |
| **8** Operational GUI | | F | Pending |
| **9** Automated qualification | Synthetic intake campaign, endurance, crash matrix at scale | G (first part) | Pending |
| **10** SMB / installed-build / Alpha release gate | Real network-share qualification, installed-build upgrade, release preparation | G (second part) | Pending |

The phase letters A–G, the prompts and the acceptance criteria keep their
meaning; a revised phase cites the roadmap phase it draws from.

### 0.1.1-A — Set identity

**Goal.** One canonical, case-insensitive set identity with an optional
physical mark (ARCHITECTURE_NOTES.md §7). Closes defect 5.

**Scope.** Version bump as the first commit (§9). `services/set_identity.py`;
migration 13 (`project_set.canonical_code`, `project_set.physical_mark`);
collision handling for existing `A`/`a` projects (reported, never merged
automatically); every set-code comparison routed through `set_identity`
(registry, Resolve, reconciliation placement, rejection's declared set,
scoring and key lookup, verified keys, report associations, readiness, Answer
Key checks); the physical mark in Project Configuration → Sets (*Printed on
sheet as*), Resolve (*Set 10 (A on sheet)*), `check_sheet_set`, the synthetic
generator, and CSV exports (*Set (as read)*, *Set*).

**Non-goals.** Sessions (B). Automatic merging of colliding sets.

**Risks.** A missed comparison reintroduces the defect — hence the architecture
test. Reverses the documented exact-match decision in `find_conflicting_set`.

**Tests.** Canonical form (case, whitespace, NFKC, digits); physical ↔ logical
round trip; `can_print` validation; collision migration from a schema-12
fixture; a lower-case logical set and a mapped `10 → A` set driven end to end
(definition → recognition → Resolve → Attendance → Answer Key → Results →
Reports); architecture test forbidding direct `set_code` comparisons.

**Likely files.** `database/models.py`, `database/migrations.py`,
`domain/exam_sets.py`, `services/project_sets.py`, `services/review_store.py`,
`services/reconciliation_store.py`, `services/scoring.py`,
`services/scoring_store.py`, `services/report_store.py`,
`services/report_readiness.py`, `services/scan_lifecycle.py`,
`services/answer_key.py`, `gui/project_config_dialog.py`, `gui/review/…`,
`evaluation/answer_keys.py`, `evaluation/attendance_dataset.py`.

### 0.1.1-B — Scan sessions and finite batches

**Goal.** The `Project → ScanSession → ScanBatch` model exists, every batch
belongs to a session, and batches are genuinely finite (ARCHITECTURE_NOTES.md
§§5–6, findings F1, F2). Closes defect 6; removes the *cause* of defects 1–2
(C removes their effect downstream). Closes the crash-safety gaps that exist
today in the finite workflow (ARCHITECTURE_NOTES.md §13.5 S1–S5, S7, R1–R3).

**Scope.** ADR for the session model (numbering continues from ADR-0004).
Migration 14: `scan_session`; `scan_batch.scan_session_id`, membership state
(OPEN / SEALED), `role` (`scan` / `rescan` / `reprocess` / `legacy`); a
first-class batch-supersession record (reason, by, when, audited reversal).
A Qt-free `services/scan_sessions.py`: create/close/reopen, the **active
session** pointer in `project_setting`, implicit creation on first *Process
All*, sealing, *Reprocess All* as a superseding `reprocess` batch,
batch-to-session assignment, *Combine into one session* (same project only,
explicit, audited, validated against duplicate effective sheets and
contradictory supersession). Upgrade backfill (ARCHITECTURE_NOTES.md §6.4,
decided): each existing batch its own one-batch session; batches joined by an
unambiguous confirmed replacement share one; unrelated batches never combined
automatically. Session CLOSED / REOPENED semantics (§6.1), with close sealing
every batch and reopen marking final outputs stale. Template
pinning per session (§16 Q3). The Scan page re-adopts the active session on
project open, and a later *Process All* creates a new batch **in the same
session**; rescans for sealed batches go into a new `rescan` batch.
`processing_manifest` written at seal / run boundaries. Every session and seal
transition audited. **Crash safety for the finite workflow — closes the required pre-Alpha defects
S1, S2, S3 and R1**: on project open, discover interrupted session/batch state
from persisted data and restore the workflow context in Scan **and** Resolve —
completed sheets visible immediately, unfinished work available for Resume,
Resolve loading the persisted session/batch directly without a visit to Scan
(S2, R1); counts reconstructed from committed rows (S4); the completed count
and "done" marks reflect committed state only (S3); recognition and its
required conflict state as one crash-consistent work unit — one transaction,
or a recorded step that recovery deterministically completes before the sheet
is Resolve-ready (S1); **measure per-sheet commits** and prefer them if
practical (ARCHITECTURE_NOTES.md §13.8); recovery preserves OPEN/SEALED state
and never creates a session, batch or superseding batch.

**Non-goals.** Downstream aggregation (C). Watched folders (D). Any visible
session management beyond a session name in the Scan stage header and a
minimal *Scan session* menu (new / rename / close / reopen / combine).

**Risks.** Changing when a batch is created touches the most-used page; the
legacy backfill must reproduce today's results exactly, including cross-batch
replacements. Until C lands, downstream stages still read one batch — B must
not make that worse: they read the **active session's most recent batch**
explicitly (not `updated_at`), and say so in their header.

**Tests.** Session lifecycle and illegal transitions; sealing rules; reprocess
supersession; backfill from a schema-12 fixture with (a) one batch, (b) two
batches of one exam (defect 1), (c) batches linked by a cross-batch
replacement; reopen-and-add-scans lands in the same session; retrying an old
batch and `recover_interrupted` do not change what downstream reads (defect
2); `processing_manifest` rows written; crash-safety cases 1–10 and 12 of
ACCEPTANCE_CRITERIA.md §5.4 for the finite workflow, with **real process
kills**; the Phase 10 harness self-test passes unchanged.

**Likely files.** `database/models.py`, `database/migrations.py`, new
`services/scan_sessions.py`, `domain/` vocabulary, `services/batch_store.py`,
`services/scan_lifecycle.py`, `services/project_service.py`,
`gui/scan/page.py`, `gui/main_window.py`, `gui/attendance/page.py`,
`gui/results/page.py`, `gui/reports/page.py`, `docs/decisions/`.

### 0.1.1-C — Session-level review, reconciliation, scoring and reporting

**Goal.** The authoritative session-level view (ARCHITECTURE_NOTES.md §§8–9).
Closes defects 1–4 in effect.

**Scope.** ADR on the representation (ARCHITECTURE_NOTES.md §8.3, finding F4)
and migration 15. The effective-scan-set service. Resolve: one session queue,
batch/source filters. Session-wide duplicate-ID conflicts on **effective**
identifiers, incremental and bounded; the (set, identifier) grouping option,
default unchanged. Content hash at **registration** for manual adds and
session-wide exact-duplicate handling. Reconciliation, scoring and reports over
the session; `generated_report` records its scope; Attendance, Results and
Reports select a session explicitly (default: active session) and never pick
"the latest batch"; batch-level diagnostic views. Provisional labelling while
open; Final Export requires a CLOSED session, offered as one step, "Close
session and generate final export" (decided, §16 Q4); final outputs recorded
against the close they came from and marked stale on reopen. The effective
set enforces the supersession invariant for every cause. Reject & Rescan in session terms: counted once in the
session (F3); new cross-session replacements refused. Renamed-copy export over
the effective set for multi-batch sessions.

**Non-goals.** Intake (D); changes to scoring rules, reconciliation
classification, ranking or report layout.

**Carried dependency.** The Results *Dashboard* tab analyses exactly the rows
the Results table lists (`ResultsPage.state.every_result`, via
`services/result_analytics.py`). When Results reads a session instead of one
batch, the dashboard must be fed the session's rows through that same
attribute — never a second read — so its counts keep reconciling with the
Results summary.

**Risks.** The largest migration of the line (table rebuilds or parallel
tables); byte-identical per-batch behaviour for single-batch sessions; bounded
duplicate detection at 100k sheets.

**Tests.** Effective-set rules (rejection states, supersession, reprocess,
exact duplicates, chains, undo); the 10:03/10:47/11:05 duplicate scenario
across batches; a synthetic **multi-set** session spread over several batches
with replacements and duplicates matching the generator's ground truth; the
same cohort as one batch gives identical marks; **golden regression**: a
single-batch project produces byte-identical reconciliation rows and workbook
cell values to `main` before this phase; Final Export blocked while open.

**Likely files.** `database/models.py`, `database/migrations.py`,
`services/reconciliation_store.py`, `services/reconciliation.py`,
`services/scoring_store.py`, `services/report_store.py`,
`services/report_readiness.py`, `services/review_store.py`,
`services/conflict_policy.py`, `services/scan_lifecycle.py`,
`services/scan_provenance.py`, `gui/review/page.py`,
`gui/attendance/page.py`, `gui/results/page.py`, `gui/reports/page.py`.

### 0.1.1-D — Intake sources and ledger

**Goal.** Files from named sources are discovered, stabilised, hashed and
registered exactly once into the session, surviving restart
(ARCHITECTURE_NOTES.md §10). Headless.

**Scope.** ADR on ingest-by-copy vs reference-in-place. Migration 16:
`intake_source`, `intake_file`, `batch_scan.intake_file_id`, `registered_at`,
`scan_batch.source_id`. `services/intake.py`: source CRUD; authoritative
periodic reconciliation (`os.scandir`); the stabilisation state machine with
injectable clock and filesystem; hash-and-decode from one read; collision
rules; reachability; restart re-verification; manual intake converging on the
same path (*Add Folder* records into the built-in manual source). Registration
into sealed batches of the session is E's; D ends at `ready` + registration
API.

**Non-goals.** Continuous processing (E); GUI (F); notifications unless cheap
and dependency-audited.

**Risks.** Hashing large files over SMB; long/UNC paths; `mtime` granularity
and caching on network shares (only G's real SMB run settles it).

**Tests.** Unit, fake clock + fake filesystem: every transition; stepped
growth, held-open, header-first, zero-byte, rename-into-place, deletion before
ready, reappearance, path reuse, duplicates within and across sources,
exclusions, unreachable → reachable, restart mid-stabilisation. Integration,
real temporary directories: three sources writing `000001.jpg…`; a writer
killed mid-file; engine stopped while writers continue, then restarted —
every file exactly once, no partial file ready, source hashes unchanged.
Measurements of listing cost and stabilisation latency recorded.

**Likely files.** `database/models.py`, `database/migrations.py`,
`database/engine.py`, new `services/intake.py`, `services/scan_provenance.py`,
`services/scan_import.py`, `domain/` vocabulary.

### 0.1.1-E — Continuous processing, quality decisions and session controls

**Goal.** Processing runs while files arrive; pause, resume and finish are
safe; the quality decision layer feeds Reject & Rescan
(ARCHITECTURE_NOTES.md §§11–14).

**Scope.** ADR on the writer strategy and `busy_timeout`. The unit scheduler
creating sealed, per-source batches in the session through the unchanged
`process_batch`; warm-pool / unit-size measurement. `ScanJobStatus.PROCESSING`
actually written. Conflict sync moved out of `ScanPage._generate_conflicts`
into a headless service called after each unit commit (finite GUI path
identical). The quality decision layer (pure, versioned, pinned, labelled
unvalidated) producing **suggested** rejections confirmed through the existing
`reject_scan`. Rescan suggestions by source and arrival time. Held files for
closed sessions. Pause / finish-current / cancel-queued semantics; persisted
intake and processing pause flags. The session snapshot (partitioning counts,
three progress lines, caught-up predicate, per-source counts and rate alarm).
Finish-session validation with its blocker list. Restart sequence:
`recover_interrupted` → conflict-sync recovery pass → reconcile every source →
resume if it was running; crash-safe persistence holds under continuous
processing (per-unit commits, sheet-local conflicts committed with or
recoverably after each result, `PROCESSING` written).

**Non-goals.** GUI (F). Automatic rejection or replacement. Scheduling across
machines.

**Risks.** Reworking the batch loop Phase 10 hardened — extend, keep the
fixed-list path; incremental conflict sync at 100k; single-writer throughput.

**Tests.** Scheduler ordering and sizing; quality mapping rows; outcome
projection; snapshot partition; each finish blocker; a three-source session of
~500 synthetic sheets with folds, late duplicates, a replacement from a
different source, undo and a chain; real process kills mid-unit with a
submission log proving no resubmission; contention test for concurrent writes;
finite GUI conflict flow unchanged.

**Likely files.** `gui/scan/worker.py`, `services/batch_processor.py`,
`services/parallel_batch.py`, `services/batch_store.py`,
`services/batch_progress.py`, `services/intake.py`,
`services/scan_sessions.py`, `services/review_store.py`,
`services/scan_lifecycle.py`, new `services/scan_quality_decision.py`,
`database/engine.py`, `docs/decisions/`.

### 0.1.1-F — Operational GUI

**Goal.** An operator can run a multi-scanner session from the Scan stage and
review while scanning continues, without a dashboard; the finite Scan stage is
unchanged (ARCHITECTURE_NOTES.md §14).

**Scope.** Session mode on the Scan stage (sources, start, pause/resume,
finish with blockers, reopen); live counts and the three progress lines;
states and their exact wording; a compact, collapsible per-source table;
*Source* column and filter; the scan list SQL-paged (continuous intake makes it
unbounded); held / duplicate / unreadable lists; Resolve and the Rescan queue
updating during intake; suggested rejections to confirm; session named in
every stage header; clean close/exit while a session runs; unvalidated-default
notes without modals.

**Non-goals.** Charts, remote dashboards, per-operator accounts, redesign of
unrelated pages.

**Risks.** Scan page complexity; 1366×768 fit; modal-from-worker-callback
hangs (fixed twice before).

**Tests.** `qtguitesting` workflow; no-session / single-batch Scan stage
identical; every state and wording; decreasing recognition line; caught-up vs
unreachable; each finish blocker; queues updating during intake; close while
running; event-loop latency at 10,000 files with ongoing arrivals; 1366×768 and
175 % scaling; 100k-row paging.

**Likely files.** `gui/scan/page.py`, `gui/scan/table_model.py`, new
`gui/scan/session_panel.py` (or similar), `gui/review/page.py`,
`gui/review/rescan.py`, `gui/main_window.py`, `gui/theme/`.

### 0.1.1-G — Qualification and Alpha release

**Goal.** Evidence that the release does what it claims
(ACCEPTANCE_CRITERIA.md §§4–8).

**Scope.** The synthetic intake campaign (≥ 3 sources, ≥ 10,000 images,
release-blocking assertions) as a harness **additional** to the unchanged
Phase 10 harness; the real SMB network-share qualification (two real machines,
a person); finite-mode regression; packaged-build smoke run with live intake;
upgrade of a schema-12 project on the installed build; **targeted endurance
tests** (multiple batches, continuous intake, supersession/reprocessing,
restart/resume, session-level aggregation) and the crash-safety matrix
including interruption at ≈ 1 / 25 / 50 / 75 / 99 % of a large run;
documentation; release preparation up to, not including, tagging. A fresh
100,000-sheet run is **optional for Alpha** (decided, §16 Q9) — it becomes a
mandatory gate before the first Beta.

**Non-goals.** Real scanning-room qualification (later `0.1.1` Alpha, part of
Phase 11B's evidence); code signing; new features.

---

## 6. Priority classification

| Item | Priority |
|---|---|
| Canonical set identity (case-insensitive, one function) | **P0** |
| Logical ↔ physical set mapping | **P0** |
| `ScanSession` model; every batch in a session; active-session pointer | **P0** |
| Finite batches: sealing; reprocess supersession | **P0** |
| Late scans join the session; no silent change of what is scored (defects 1–2) | **P0** |
| Session-level effective scan set, reconciliation, scoring, reports | **P0** |
| Duplicate Student ID on effective values, session-wide (defect 3) | **P0** |
| Exact-duplicate image handling, session-wide (defect 4) | **P0** |
| `processing_manifest` written (defect 6) | P1 |
| Persistent intake sources; ledger with hash-at-discovery; idempotent registration | **P0** |
| Stable-file detection | **P0** |
| Continuous processing through finite units | **P0** |
| Restart recovery including files arriving while stopped | **P0** |
| Crash-safe persistence and resume of Scan and Resolve (hard termination, not only graceful close) | **P0** |
| Close crash-recovery defects S1, S2, S3, R1 (conflicts lost after a crash; interrupted batch not re-adopted; uncommitted sheets shown as done; Resolve unreachable after reopen) | **P0 — required before `0.1.1-alpha.0`** |
| Durable-completion invariant: operator-visible completed work is committed work | **P0** |
| First-class supersession; effective totals exclude superseded membership | **P0** |
| Session CLOSED / REOPENED semantics; reopening stales final outputs | **P0** |
| Single DB-writer coordination + `busy_timeout` | **P0** |
| Provisional vs final results; Final Export requires a closed session | **P0** |
| Scan-quality decision layer → suggested rejections | P1 |
| Pause / finish current / resume semantics | P1 |
| Source health and network-failure states | P1 |
| Session panel, per-source table, source column/filter | P1 |
| SQL-paged scan list for unbounded intake | P1 |
| Rescan suggestions by source/time; re-import at registration | P1 |
| Backlog / rate telemetry; "caught up" instead of ETA | P1 |
| Held files after session close | P1 |
| (set, identifier) duplicate grouping option | P1 |
| Filesystem-notification hints | P2 |
| Answer-similarity hint for same-sheet-twice | P2 |
| Per-source operator/scanner metadata beyond a note | P2 |
| Automatic rejection or replacement | Deferred |
| Distributed DB / server, cloud sync, remote dashboard | Deferred |
| Scanner drivers (TWAIN/WIA/ISIS), network discovery | Deferred |
| Multi-user concurrent editing, several OMRFlow instances on one project | Deferred |

---

## 7. Status vocabulary and tracking

The repository's terms are **not synonyms**: *implemented*, *automated tests
passing*, *synthetically validated*, *real-scan validated*, *operationally
qualified*, *released*. Each phase is tracked on separate tracks:

| Track | Meaning |
|---|---|
| Implemented | The phase's scope exists in code |
| Tested | Its unit, integration and GUI tests pass, with `ruff` and `mypy` |
| Synthetically validated | The relevant part of the intake campaign (ACCEPTANCE_CRITERIA.md §5) ran and passed |
| Network-share validated | The SMB qualification (§6 there) ran and passed on real infrastructure |
| Real-scanner validated | Real scanner workstations, real paper (§7 there) |
| Production qualified | Operationally qualified for a real examination |

**Passing tests never complete a phase by themselves.** A phase may read
"Implemented; tested" and remain pending on every validation track.

| Phase | Implemented | Tested | Synthetic | Network share | Real scanners | Production |
|---|---|---|---|---|---|---|
| A (revised 1) | **Done** (merged `ce3f082`) | **Passing** | n/a | n/a | Pending | Pending |
| B lifecycle (revised 2) | **Done** (merged `1798e84`) | **Passing** | n/a | n/a | Pending | Pending |
| B crash safety (revised 3) | **Done** (merged `0e94d67`) | **Passing** | n/a | n/a | Pending | Pending |
| C (revised 4) | **Done** (merged `fd063f8`) | **Passing** | Pending (the intake campaign is Phase G's) | n/a | Pending | Pending |
| D (revised 5) | **Done** (merged `59ba8df`) | **Passing** | Pending (local temp-directory soak only; the campaign is Phase G's) | **Not performed** | Pending | Pending |
| E first part (revised 6) | **Done** (merged `141d703`) | **Passing** | Pending (local / fake-filesystem endurance only; the campaign is Phase G's) | **Not performed** | Pending | Pending |
| E second part (revised 7) | **Done** (branch, not merged; migration 17) | **Passing** | Pending (local / fake-filesystem scenario only; the campaign is Phase G's) | **Not performed** | Pending | Pending |
| F | Pending | Pending | Pending | Pending | Pending | Pending |
| G | Pending | Pending | Pending | Pending | Not required for `alpha.0` | Not required for `alpha.0` |

---

## 8. Out of scope for `0.1.1-alpha.0`

- Changing recognition, alignment, scoring rules or the conflict policy's
  meaning (answer ambiguity stays a result, never a conflict).
- Redesigning the Phase 10 100,000-sheet harness or its assertions; the intake
  campaign is **additional**.
- Several OMRFlow instances writing one project; scanner control; PDF input;
  authentication (reviewer identity stays a name).
- Automatic, unconfirmed rejection or replacement.
- Validated quality-decision defaults — those come from real-data
  qualification.

---

## 9. Version-bump and release rules

- `src/omr_scanner/_version.py` is the **single source of truth**; no second
  version constant anywhere.
- It stays `0.1.0-alpha.2` while this plan is reviewed and merged.
- It becomes `0.1.1-alpha.0` **only when implementation begins**, as the first
  commit of prompt 01, alone: `chore: begin the 0.1.1-alpha.0 development line`.
  Run the version and release-automation tests; add a `CHANGELOG.md`
  `[Unreleased]` note that the line has begun; no `[0.1.1-alpha.0]` heading
  until it is released.
- Consequence accepted knowingly: between the bump and the release a source
  build reports `0.1.1-alpha.0` with the feature incomplete — as
  `0.1.0-alpha.2` was developed; the About dialog's Alpha notice covers it.
- Nothing is tagged, built for release or published by automation. Releasing
  follows `docs/release/RELEASE_CHECKLIST.md` after the gate in
  ACCEPTANCE_CRITERIA.md §8, as a GitHub pre-release, on the owner's explicit
  instruction.
- **Release gate:** the installer/version-ordering defect F8 must be fixed and
  tested before any Beta tag is created (ARCHITECTURE_NOTES.md F8, §16 Q10).

---

## 10. Documentation rule for every phase

Each phase updates at least: §7's status table here;
`development/releases/0.1.1-alpha.0/PHASE_<X>_HANDOFF.md` (new);
`development/CURRENT_STATE.md`; the README **Development status** and **Testing
status**; the `0.1.1` entry in `docs/wiki/Development-Roadmap.md`;
`CHANGELOG.md` `[Unreleased]`; `docs/DATA_MODEL.md` for any schema change; and
the user/developer documents its scope touches.

---

## 11. Superseded plans

This plan reconciles and replaces:

| Document | Was | Now |
|---|---|---|
| `development/ROADMAP_v0.1.1-alpha.0.md` (commit `073a95e`, 2026-09-29, against `e9dd813`, schema 12) | "Single active batch" plan; ingest session deferred; cross-batch aggregation deferred | **Superseded.** Kept as a short pointer. Carried forward: baseline facts, the six latent defects, set identity and logical ↔ physical mapping, intake ledger, stabilisation, duplicate table, pause/resume semantics, priorities, acceptance matrix, performance targets, migration discipline. **Not** carried forward: the single growing active batch, and deferral of session-level aggregation |
| `development/releases/0.1.1-alpha.0/` as merged from `roadmap/0.1.1-alpha.0-live-intake` (commit `a8b191e`, 2026-09-27, against `2d18ccb`, schema 9) | Scan session over finite processing units; six phases A–F; prompt pack | **Rewritten in place** into this plan. Carried forward: `ScanSession` over finite `ScanBatch` units, session-level results, SMB qualification, the quality decision layer, the status tracks, version-bump rules, the prompt-pack structure. **Not** carried forward: its schema-9 "current state", "migration 10", the claims that batch membership is fixed and the late-added-scan defect (both no longer true, ARCHITECTURE_NOTES.md F9–F10), and its proposal to run Phase 11B only on the `0.1.1` line. `MERGE_NOTES.md` removed — the merge it described is done |

## 12. Relationship to the canonical roadmap

`docs/wiki/Development-Roadmap.md` stays canonical for project status and
Phases 0–11. It carries one short `0.1.1-alpha.0` entry pointing here, and
Phase 11B's policy as stated in §4. Phase 11B's own definition and target are
unchanged by this plan.
