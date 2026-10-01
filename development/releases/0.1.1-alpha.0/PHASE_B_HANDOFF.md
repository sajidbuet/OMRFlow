# Revised phase 2 handoff — ScanSession + finite ScanBatch lifecycle

Branch `feat/0.1.1-phase2-scan-session-lifecycle`, from `main` at `ce3f082`.
**Not merged, not tagged, nothing released.** This is the *lifecycle* part of
roadmap phase 0.1.1-B ([prompts/02-scan-sessions.md](prompts/02-scan-sessions.md));
its crash-safety part moved to revised phase 3 (ROADMAP.md §5.1).

## Baseline

| | |
|---|---|
| Starting commit | `ce3f082` *Merge feat/0.1.1-phase1-set-identity into main* (the Phase 1 merge commit; Phase 1 tip `993acfa`) |
| Phase 1 present | Yes: `__version__ = 0.1.1-alpha.0`, migration 13, `PHASE_A_HANDOFF.md`, `domain/set_identity.py`, `services/set_identity.py`; `review_store.effective_set_codes` the only physical → logical translation |
| Version / schema | `0.1.1-alpha.0` / 13 (migration 14 free) |
| pytest | **6,168 passed, 16 skipped, 0 failed**, 4 `stress` deselected (46 min) |
| ruff / mypy | All checks passed / no issues in 203 source files |

## Schema

**Migration 14 → schema 14** (`_migration_014_scan_sessions`, structure only):

* table `scan_session` (`scan_session_id`, `project_id`, `name`, `state`,
  `origin`, pinned `template_id/template_name/geometry_fingerprint/recognition_fingerprint/engine_version`,
  `created_at/by`, `closed_at/by`, `reopened_at/by`, `reopen_count`,
  `final_outputs_stale_since`, `merged_into_session_id`, `notes`), index
  `ix_scan_session_state`;
* table `batch_supersession` (`superseded_batch_id`, `superseding_batch_id`,
  `reason`, `recorded_by/at`, `reversed_at/by`, `reversal_reason`), two indexes;
* `scan_batch.scan_session_id` (nullable, FK `RESTRICT`), `sealed_at`,
  `sealed_by`, `role` (default `scan`), each guarded by `PRAGMA table_info`;
  index `ix_scan_batch_session`. The ORM maps the four columns `deferred`.
* `project_setting` keys `active_scan_session`, `scan_session_backfill`.

## Lifecycle model

ADR: [docs/decisions/ADR-0005-scan-sessions-and-finite-batches.md](../../../docs/decisions/ADR-0005-scan-sessions-and-finite-batches.md).
Rules in `domain/scan_sessions.py`, service `services/scan_sessions.py`; every
transition is one transaction with its `audit_event`.

* **ScanSession** OPEN (receives batches) / CLOSED (receives none; close seals
  every open batch; refused while a batch is `running`). **Reopen**: explicit,
  audited, `reopen_count += 1`, `final_outputs_stale_since` set; sealed batches
  stay sealed.
* **ScanBatch** OPEN (`sealed_at` NULL) / SEALED. `add_scans_to_batch` raises
  `BatchSealedError` when a sealed batch is offered a non-member; resume of its
  own members still works. Sealing writes a manifest and is idempotent.
* **Roles** `scan`, `rescan`, `reprocess`, `legacy` (`BatchRole.is_primary` is
  false only for `rescan`).
* **Supersession** first-class; legal semantics: never self; same session;
  superseded batch sealed; at most one live superseder; no cycle (any length);
  chains allowed; reversal audited, record kept.
* **Template pinning**: the first batch pins the session. Scan stage: a
  different template asks first; continuing is audited (`template_ack`).
  Programmatic `batch_store.create_batch`: a different template (or a closed
  active session) starts a new implicit session.
* **Combine into this session**: operator-named, audited per batch; refused
  for a missing/foreign-project session, a closed target, a template-identity
  mismatch, or an effective sheet shared by path or content hash. A source
  session left empty is closed with `merged_into_session_id`.

## Backfill

Runs from `open_project` on every **writable** open (idempotent; only batches
with no session):

* each batch → its own one-batch `legacy` session, **sealed**;
* two batches share one session only when joined by confirmed cross-batch
  rescans (`scan_rejection.state = superseded_by_replacement`, original and
  replacement scans in different batches) and the relationship is
  **unambiguous**: exactly two batches connected, all links one direction,
  replacement batch newer, identical template identity. In such a pair a newer
  batch holding only confirmed replacements is labelled `rescan`;
* anything else stays separate, listed in `project_setting['scan_session_backfill']`
  and by Project Health (`SCAN_SESSION_BACKFILL_AMBIGUOUS`);
* unrelated batches are never combined;
* the session of the most recently **created** batch becomes active.

**Read-only:** a schema-13 project opened read-only is not migrated or
backfilled; reads work because the new columns are deferred, and each batch is
presented as a virtual one-batch session (`virtual:<batch_id>`, never stored).

## Scan workflow

* **Seal trigger** (derived from `main`'s Scan page, where a batch takes
  members exactly while it is the page's current batch): a batch is sealed when
  another batch starts in its session, when the session closes, when *Reprocess
  All* supersedes it, or by the upgrade backfill. Finishing a run does not seal.
* **First Process All**: implicit session (exam name + date), active, no
  question asked.
* **Second Process All** (after clearing the list or reopening the project):
  new batch in the same session; the previous batch sealed.
* **Rescan**: into the page's open batch as before; for a sealed batch, a new
  `rescan` batch of the same session (`ScanPage._import_rescans_into_new_batch`).
  Confirmation on Resolve and counting in the original's batch unchanged.
* **Reprocess All**: `start_reprocess_batch` — seals the original, creates a
  `reprocess` batch over the same files in the same session, records the
  supersession; the original stays whole.
* **Session menu** (Scan stage *Session* button): New, Rename, Close, Reopen,
  Combine; the header names the active session.

## Temporary downstream semantics (until revised phase 4)

> **Multi-batch session aggregation is not yet implemented.** A session with
> several batches is not yet authoritative for scoring or results.

Attendance, Results and Reports read **one** batch:
`scan_sessions.downstream_batch_id` — the active session's newest batch **by
creation** whose role is `scan`/`legacy`/`reprocess`, not superseded, and not
`new`/`running`. `list_batches(limit=1)` / `updated_at` no longer choose it, so
a retry or `recover_interrupted` cannot change what is read. The main window
routes a finished run to that batch (a rescan batch's run leaves them on the
original). Their batch label carries the session in its tooltip and, for a
multi-batch session, *"one batch of several"*. Resolve still loads the batch it
is given (Scan's *Review Conflicts*, the Rescan view). The Results Dashboard is
unchanged and still analyses exactly the Results table's rows.

## Tests

| File | Tests | Kind |
|---|---|---|
| `tests/unit/test_scan_session_rules.py` | 24 | unit: states, transitions, membership, roles, supersession rules (self, cross-session, unsealed, double, 2- and 3-cycles, chain), backfill grouping (one, unrelated, unambiguous pair, chain of 3, both ways, older replacement, template difference, unknown link) |
| `tests/integration/test_scan_sessions.py` | 28 | service: lifecycle and illegal transitions, running blocks close, implicit creation and naming, pointer persistence, replacement, project isolation, open/sealed membership, idempotent seal, later batch in same session after reopen, close seals and refuses, rescan batch, template pin refuse/ack/new-session, Reprocess supersession, illegal supersessions, reversal, downstream rule (retry / `recover_interrupted` do not move it; rescan, superseded, running skipped), Combine (moves + audits, operator required, closed target, duplicate sheet, template, foreign project), manifests, health clean and violations |
| `tests/integration/test_scan_session_migration.py` | 11 | migration + backfill from schema-13 projects written by the schema-13 build (`tests/fixtures/schema13/`): cases A–E, idempotence, health, read-only virtual sessions, older-build refusal (simulated) |
| `tests/gui/test_scan_session_gui.py` | 10 | Scan stage: implicit session with no modal, same session after reopen, New/Rename/Close/Reopen, Combine, closed session refuses a run, menu disabled without a project, downstream wiring through the main window (rescan batch, reopen), Reprocess, rescan of a sealed batch, rescan into the open batch |
| **Total new** | **73** | |

**Existing tests edited (one file, three tests)** —
`tests/integration/test_set_identity_migration.py` (Phase 1) pinned facts that
were true only while 13 was the newest schema:

* `test_it_upgrades_to_schema_13_without_losing_anything`: `SCHEMA_VERSION == 13`
  → `SCHEMA_VERSION >= 13` (the upgrade now continues to 14);
* `test_a_backup_is_taken_before_migrating`: backup name `before-migration-12-to-13`
  → `before-migration-12-to-{SCHEMA_VERSION}` (one backup per upgrade run, named
  for its end point);
* the preserved-row snapshot (used by it and `test_the_collision_is_kept_and_never_merged`)
  now counts `audit_event` rows excluding `scan_session`/`scan_batch` entities:
  the backfill appends lifecycle events; no historical row is changed or lost.

No assertion about preserved data was weakened. Two other full-suite failures
were fixed in code, not in tests: migration 14's description contained
`scan_`, tripping `test_reject_and_rescan_followup::test_no_schema_change_was_needed`
(reworded); and `downstream_batch_id` returned nothing for batches with no
session and no active pointer (a project adopted without the open-time
backfill) — it now falls back to the newest eligible unassigned batch.

Full suite and gates (main checkout, where the local real-sheet tests run):

| | Result |
|---|---|
| pytest (full) | **6,241 passed, 16 skipped, 0 failed**, 4 `stress` deselected (59 min 00 s), main checkout at `272fdd5`. +73 passed against the baseline (the new tests); same 16 skips (environment / env-gated) |
| ruff | `ruff check .`: All checks passed |
| mypy | `mypy src`: no issues in 206 source files |

## Validation status

```text
Implemented:               Yes - branch feat/0.1.1-phase2-scan-session-lifecycle, not merged.
Automated tests:           Passing (above).
Synthetic validation:      n/a for this phase.
Network-share validation:  n/a for this phase.
Real-scanner validation:   Not performed.
Production qualification:  Not performed.
```

The Scan stage was driven by tests and its header inspected in one rendered
screenshot at 1366×768; no operator has used it.

## Deferred to revised phase 3

```text
S1  recognition committed but its conflicts not yet generated (crash between the two)
S2  interrupted session/batch not re-adopted (Scan) on reopen
S3  buffered (uncommitted) sheets counted as done
R1  Resolve cannot load persisted state directly on reopen
real process kill/restart matrix (ACCEPTANCE_CRITERIA §5.4, incl. 1/25/50/75/99 %)
durable-completion invariant under abnormal termination
progress reconstruction after crash
per-sheet commit measurement and the commit-granularity decision (§13.8)
```

`recover_interrupted`, grouped commits, Resume and `_generate_conflicts` are
unchanged. Recovery today does not touch lifecycle state (it neither seals,
nor creates sessions, batches or supersessions); phase 3 must keep that.

## Known limitations

* Downstream reads one batch; a multi-batch session is not aggregated (phase 4).
  After an upgrade, "newest" is by creation time rather than `updated_at`.
* A late folder processed into a `rescan` batch (the page keeps the rescan
  batch as current) is not read downstream; such unlinked scripts count only
  when session aggregation exists.
* *Combine* has no reversal command; each moved batch's audit event records its
  previous session, which a reversal would need.
* Combine's duplicate check is by file path and content hash only; duplicate
  candidate identity across sessions is phase 4's effective-set work.
* `final_outputs_stale_since` is a session-level timestamp; tying individual
  `generated_report` rows to a session is phase 4.
* Closure checks are minimal (refused while running); the full checklist of
  ACCEPTANCE_CRITERIA §3 is later work.
* `list_scan_sessions` counts batches with one query per session (fine for
  tens of sessions; not optimised).
* The schema-13 fixtures record `written_by_version = 0.1.1-alpha.0`, the same
  label as this build; they are distinguished by schema version.
* Read-only virtual sessions are display-only; nothing can be changed there.

## Roadmap / code discrepancies

1. The old prompt asks for schema-**12** fixtures; this brief (and the current
   code) needs schema-**13** ones. Schema-13 fixtures were written by the
   schema-13 build.
2. `ProcessingManifest`'s docstring names `services.processing_manifest.build_manifest`,
   which did not exist; the module was created (`build_payload`/`record`).
3. ARCHITECTURE_NOTES §6.1 offers "acknowledge or start a new session" for a
   template change; the ADR splits it: GUI asks (acknowledge or cancel), the
   programmatic path starts a new implicit session.
4. A replacement-only batch grouped by the backfill is labelled `rescan`, not
   `legacy` (§6.4 says `legacy` for every backfilled batch) — otherwise the
   downstream single-batch rule would read an empty cohort.
5. Backfilled batches are sealed (not stated in the plan; they are history).
6. `CHANGELOG.md`: Phase 1 had inserted a `### Fixed` heading above the existing
   *Added* entries, mis-filing them; restored here.

## Phase 3 starting point

> Phase 3 must start from this lifecycle model and add crash-safe Scan/Resolve
> persistence without changing the Project → ScanSession → finite ScanBatch
> architecture.

It starts from this branch once merged (schema 14; next migration **15**),
keeps `scan_sessions` as the only place lifecycle state changes, and must
preserve OPEN/SEALED on recovery.
