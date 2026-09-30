# `0.1.1-alpha.0` — acceptance criteria

> **Planning document.** Nothing here has been run. Every criterion is a
> requirement for later work.

Rules that apply to every section:

- A criterion is met only by **evidence recorded in the repository** (a test,
  a committed report, a handoff entry with the command and its output) — not
  by a statement that it was checked.
- `pytest`, `ruff check src tests tools scripts` and `mypy` (strict, as
  configured in `pyproject.toml`) pass at the end of every phase.
- Every pre-existing test passes **unchanged**. A test may be edited only if
  the phase's handoff explains why its old expectation was wrong, not merely
  inconvenient.
- **Passing unit tests does not complete a phase.** Status is reported on the
  six tracks of ROADMAP.md §5, separately.
- No real candidate data is committed. Real scans stay in the git-ignored
  `private_test_data/` or `local_test_data/`.

---

## 1. Cross-cutting invariants (must hold after every phase)

| # | Invariant | Checked by |
|---|---|---|
| X1 | A finite batch outside a session behaves exactly as in `0.1.0-alpha.2`: registration, resume, retry, per-batch duplicate IDs, conflicts, reconciliation, scoring, reports | Existing suites, unchanged; plus a golden comparison of a finite project processed by both builds |
| X2 | Workers never open the database; only the coordinator writes | `tests/unit/test_architecture.py` (extended if needed) and code review |
| X3 | Services contain no Qt; the GUI imports no OpenCV/NumPy/SQLAlchemy | `tests/unit/test_architecture.py` |
| X4 | Nothing is ever deleted to express a decision: rejected, superseded and duplicate assets and their history persist | Tests on every transition |
| X5 | Original source files are never modified, moved or deleted | Hash-before/hash-after tests on every intake path |
| X6 | `audit_event` and `batch_scan_history` remain append-only | Existing trigger tests |
| X7 | Migrations are additive and forward-only; a schema-9 project upgrades and answers every earlier query identically | Upgrade test from a committed schema-9 fixture |
| X8 | The Phase 10 harness and its 15 assertions are unchanged and still pass their small-scale self-test | `tests/unit/test_qualification.py`, `tests/integration/test_stress_*` |
| X9 | Answer ambiguity never becomes a conflict | Existing `test_conflict_policy.py` cases |

---

## 2. Per-phase exit criteria

### A — Architecture & persistence

- A1. `__version__` is `0.1.1-alpha.0` in `_version.py` only; version tests
  pass; `CHANGELOG.md` `[Unreleased]` notes the line has begun.
- A2. ADR-0005 (ingest vs reference), a writer-strategy ADR and a
  template-change ADR are committed and referenced from `docs/ARCHITECTURE.md`.
- A3. Migration 10 creates the session, source and asset representation and
  the optional links; `DATA_MODEL.md` documents each field.
- A4. Repository-layer tests: create/open/close session; add/enable/disable
  source; register asset; link asset to `BatchScan`; record duplicate and
  supersession relationships; all transitions audited.
- A5. Upgrade test from schema 9 (X7).
- A6. The late-added-scan defect (ARCHITECTURE_NOTES.md §17) has a test that
  demonstrates it; it is fixed or explicitly deferred with the test marked
  `xfail(strict=True)` and a reason.
- A7. No GUI change beyond what the version bump implies.

### B — Intake engine

- B1. Every stable file in every enabled source is registered **exactly once**,
  including files present before the engine started.
- B2. No asset becomes READY while its file is still being written — tested
  with writers that grow a file in steps, pause, hold it open, and write a
  valid header before the body.
- B3. Same filenames in different sources are distinct assets; same relative
  path with new content in one source is a new, flagged asset.
- B4. Byte-identical content is `duplicate_content`, linked to the first, and
  never submitted for recognition.
- B5. A source that becomes unreachable is marked so without affecting other
  sources or marking its assets missing; on return every file created meanwhile
  is discovered.
- B6. After a restart, files created while OMRFlow was closed are discovered;
  in-flight stabilisation is redone; READY assets are re-verified.
- B7. Correctness holds with notifications disabled (reconciliation only).
- B8. Stabilisation parameters are configuration, with the measured basis for
  their defaults recorded in the handoff.
- B9. Manual import and watched sources produce identical asset records for
  identical files.

### C — Incremental processing, review & rescan

- C1. READY assets are processed in finite units that satisfy every Phase 5
  invariant; a unit interrupted by a forced kill resumes without
  resubmitting committed sheets (measured, as Phase 10 does).
- C2. Sheet-local conflicts appear after each unit commit, not at session end.
- C3. Cross-sheet duplicate IDs span units: the 10:03 / 10:47 scenario creates
  conflicts on **both** sheets; a correction that dissolves the group withdraws
  the untouched one; superseded and duplicate-content assets never participate.
- C4. Duplicate grouping by (set, identifier) exists as a policy option with
  the default unchanged, documented as an office decision.
- C5. The quality decision layer is pure, policy-driven, fingerprinted and
  pinned per session; its default mapping is labelled unvalidated in code and
  docs; no new geometric threshold is introduced.
- C6. Rescan lifecycle: RESCAN_REQUIRED → replacement suggested → confirmed by a
  named operator → original SUPERSEDED, replacement EFFECTIVE; undo restores;
  chains work; everything survives restart; nothing is deleted.
- C7. The session snapshot's counts partition (ARCHITECTURE_NOTES.md §14) in
  every test that produces one.
- C8. Concurrent writes (intake + unit recording + a review decision) complete
  without `database is locked` failures in a contention test, and the GUI
  thread's write latency is bounded.
- C9. `ScanJobStatus.PROCESSING` is actually written while a sheet is in a
  worker, and recovered as today.

### D — Operational GUI

- D1. With no session open, the Scan stage is the `0.1.0-alpha.2` workflow;
  existing GUI tests pass unchanged.
- D2. Session mode: configure sources, start, pause/resume processing, and
  Finish with a blocker list — each blocker in §3 below tested
  individually.
- D3. Three separate progress lines; recognition progress may decrease when
  the workload grows; "Caught up — watching for new scans" appears only under
  its definition; an unreachable source prevents "caught up".
- D4. Per-source status is compact and collapsible, not the dominant element.
- D5. Resolve and Rescan queues update while intake runs.
- D6. At 10,000 assets the GUI thread stays responsive (event-loop latency
  measured in a test, threshold recorded in the handoff); growing lists are
  model-based and paged, not item widgets.
- D7. Screenshots via the `qtguitesting` workflow for each new state.

### E — Session-scoped reconciliation, scoring & reporting

- E1. Reconciliation, scoring and reports accept a session's effective set;
  superseded, duplicate-content and rescan-outstanding assets are excluded by
  construction.
- E2. Per-batch results for finite projects are byte-identical to
  `0.1.0-alpha.2` on a golden project.
- E3. Results on an open session are labelled provisional; Final Export
  requires a closed session.
- E4. Attendance, Results and Reports select a session or batch explicitly;
  "latest batch" is no longer an implicit choice when a session exists.
- E5. A synthetic multi-set session with a roster and answer key produces
  results matching independently computed expectations (the existing
  synthetic dataset generator's ground truth).

### F — Qualification & release

See §§4–7.

---

## 3. Finish Scan Session — closure checks

Closure is refused, listing each blocker, unless all hold after a final
reconciliation of every configured source:

- no asset stabilising, ready, queued or processing;
- no unresolved required conflict (sheet-local or cross-sheet);
- no outstanding rescan item;
- no unmatched replacement;
- every enabled source reachable for the final reconciliation; disabled
  sources listed by name.

Temporary absence of new files never closes a session.

---

## 4. Synthetic intake qualification campaign

**Additional to, and independent of, the Phase 10 100,000-sheet campaign**,
which keeps validating deterministic finite processing, persistence and forced
kill/resume.

### 4.1 Workload

- ≥ 3 simulated scanner sources, ≥ 10,000 incoming images in total, generated
  with the existing template-driven synthetic generator so ground truth is
  exact.
- The **same filenames** from different sources.
- Files written gradually and partially (stepped growth, held-open, header
  first), with random inter-arrival times.
- Duplicate content (byte copies) within and across sources.
- Temporary source loss and reconnection.
- Forced application kill and restart while sources keep producing.
- New duplicate-ID conflicts arising while a scripted operator resolves
  earlier ones.
- Physically "bad" sheets (the existing fold generator) triggering
  RESCAN_REQUIRED, followed by replacement files — including from a different
  source, and chains.

The campaign is driven by a supervisor in the Phase 10 style: evidence is
written from outside the process being killed, and kill points are chosen
from committed state, not wall time.

### 4.2 Release-blocking assertions

| Assertion | Requirement |
|---|---|
| `stable_files_discovered_exactly_once` | Every stable file ↔ exactly one asset |
| `no_incomplete_file_processed` | No asset was submitted before its writer finished (writer logs vs submission log) |
| `source_provenance_retained` | Every asset's source, path and filename match the generator's manifest |
| `duplicate_content_identified` | Every planted byte copy, and nothing else, is `duplicate_content` |
| `independent_filenames_do_not_collide` | Same-named files from different sources are distinct and all processed |
| `no_accepted_image_lost` | Every accepted asset is still present and effective after all restarts |
| `no_completed_scan_rerecognised` | Measured via the submission log: no committed asset resubmitted |
| `offline_arrivals_discovered` | Files created while OMRFlow was down are all discovered after restart |
| `conflict_counts_correct_as_population_grows` | At each checkpoint, open duplicate-ID conflicts equal the ground-truth count for the population seen so far |
| `rescan_relationships_survive_restart` | Every confirmed association and supersession is intact after each restart |
| `aggregate_counts_consistent` | The session snapshot partitions at every checkpoint and equals a recount from raw rows |
| `sqlite_integrity` | `quick_check`, `integrity_check`, `foreign_key_check` |
| `application_invariants` | `project_health.full_check` reports no error-level or critical issue |
| `finite_mode_regression` | A finite batch run in the same build matches its golden result |

No assertion may be demoted to a warning to let the campaign pass. A smaller
run is reported as "ALL RUNS PASSED — NOT THE RELEASE QUALIFICATION", as
Phase 10 does.

---

## 5. Network-share (SMB) qualification

Phase 5 explicitly did **not** establish behaviour on a genuine network share.
Smaller than §4, on real infrastructure:

- ≥ 2 Windows machines: sources on genuine SMB shares (`\\host\share\…`),
  written by a process on the remote machine, not by the OMRFlow machine.
- ≥ 1,000 files per source, including the partial-write patterns.
- Share disconnected (cable/adapter/service stopped) and restored mid-write.
- OMRFlow restarted during intake.
- Record measured listing cost per reconciliation and the stabilisation
  latency distribution; confirm or adjust the Phase B defaults.
- The project database itself stays on a local disk (unless a separate ADR
  says otherwise); state which in the report.

Result committed under `docs/release/validation/` with machine descriptions
and OS/SMB versions. Required for `0.1.1-alpha.0`.

---

## 6. Real scanning-room qualification (after `0.1.1-alpha.0`; gates Beta)

Not required for `0.1.1-alpha.0`. Performed on a later `0.1.1` Alpha, and it
is what the Beta decision rests on:

- two or more real scanner workstations where practical, scanning continuously;
- the same filenames from different scanner PCs;
- network interruption and reconnection;
- OMRFlow restart during intake;
- operators resolving conflicts while scanning continues;
- real scan-quality rejects, physical rescans and replacement of rejected
  images;
- final session closure;
- **independently verified result correctness** — stored results compared with
  an independently computed expectation, never approved by visual inspection
  (the existing Phase 11B rule);
- the quality-decision defaults set from these real rejects, and the evidence
  recorded.

---

## 7. `0.1.1-alpha.0` release gate

All of:

1. Phases A–E: implementation complete and automated tests complete.
2. §4 synthetic intake campaign: QUALIFIED at full scale.
3. §5 SMB qualification: passed and recorded.
4. X1–X9 hold; the Phase 10 harness self-test passes.
5. Packaged application: the existing packaging smoke and installer checks
   pass, plus a scripted live-intake smoke run **on the installed build**
   (two local sources, a few hundred files, one restart).
6. Upgrade from a `0.1.0-alpha.2` project verified on the installed build,
   with the forward-only consequence stated in the release notes.
7. Documentation: user guide for scan sessions, updated Scanning,
   Processing, Review-and-Resolution, Known-Limitations and
   Upgrade-Compatibility wiki pages; README development and testing status.
8. Release notes state plainly: real scanning-room qualification **not yet
   performed**; quality-decision defaults **unvalidated**.
9. `docs/release/RELEASE_CHECKLIST.md` followed; published as a GitHub
   pre-release.

Not required for `0.1.1-alpha.0`: §6, real-data Beta qualification, code
signing, the full Phase 10 100,000-sheet run (still optional at Alpha per the
canonical qualification matrix).
