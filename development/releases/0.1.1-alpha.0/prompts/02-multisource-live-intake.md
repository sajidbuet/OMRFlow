# Prompt 02 — `0.1.1-B`: Multi-source live intake engine

You are working in the OMRFlow repository. This is **phase 0.1.1-B** of the
`0.1.1-alpha.0` line. Phase A (session/source/asset persistence, migration 10,
ADRs 0005–0007, version `0.1.1-alpha.0`) must already be merged; if
`development/releases/0.1.1-alpha.0/PHASE_A_HANDOFF.md` does not exist, stop
and say so. Branch: `feat/0.1.1-b-intake-engine` from current `main`.

Plan: `development/releases/0.1.1-alpha.0/ROADMAP.md`, `ARCHITECTURE_NOTES.md`
§§5–7, `ACCEPTANCE_CRITERIA.md` §2-B. The code is the fact; the plan is the
intent. Record discrepancies.

---

## 1. Inspect first

- `PHASE_A_HANDOFF.md`, the ADRs it produced, `docs/DATA_MODEL.md` (schema 10)
- the Phase A repository service and domain vocabulary
- `services/scan_import.py` (`collect_scan_files`, supported suffixes, natural
  sort), `services/scan_provenance.py` (`hash_file`, `duplicate_groups`,
  `check_availability`, `relink_scan`), `services/project_lock.py`,
  `database/engine.py`
- `gui/scan/worker.py` (how hashing is invoked today)
- `packaging/audit_dependencies.py`, `packaging/verify_frozen_imports.py`
  (if you consider adding any dependency)
- tests: `tests/unit/test_scan_import.py`, `test_scan_provenance.py`, the
  Phase A tests

Run the full suite before changing anything; record the baseline.

## 2. Preserve

Everything listed in prompt 01 §2, and specifically:

- `collect_scan_files` behaviour for the finite workflow.
- `hash_file` (SHA-256, 1 MiB streaming) — reuse it, do not re-implement.
- Originals are **never modified, moved or deleted** — sources are read-only
  to OMRFlow.
- Single writer; no transaction spans file I/O.
- No Qt in services; the engine must be runnable headlessly.

## 3. Scope

1. **Source reconciliation** (authoritative): list each enabled source
   (`os.scandir`; recursion as a per-source option if Phase A allowed it),
   filter by supported suffix and configured exclusions (temporary names such
   as `*.tmp`, `~*`), and diff against known assets by relative path and
   `(size, mtime_ns)`. Idempotent: running it twice changes nothing.
2. **Stabilisation state machine** (ARCHITECTURE_NOTES.md §6): DISCOVERED →
   STABILIZING → READY, with IGNORED, VANISHED, UNREADABLE and
   DUPLICATE_CONTENT. READY requires: quiet period (*K* observations over *T*
   seconds), non-empty, openable (a sharing violation means "not yet"), hashed
   **and fully decoded from the same read**, re-stat unchanged afterwards, and
   not a duplicate of an accepted asset. Stall ceiling surfaces a stalled
   asset. All parameters are configuration with documented defaults; **measure**
   them (§6) rather than guess.
   Decoding must be complete — a truncated JPEG/TIFF with a valid header must
   fail. Decide how multi-page TIFF is treated and document it.
3. **Collision rules** (§5.1): same filename across sources = distinct;
   same relative path with new content after acceptance = new asset flagged
   `path_reused`; identical hash = `duplicate_content` linked to the first,
   never submitted.
4. **Ingest** per ADR-0005: if copying, copy after READY into the project's
   managed location, verify the copy's hash, record both paths. The source
   file is untouched.
5. **Reachability**: a listing failure marks the source unreachable with time
   and error; never marks its assets missing; other sources continue; on
   return a full reconciliation runs.
6. **Restart**: on project open (non-read-only), assets in DISCOVERED /
   STABILIZING restart stabilisation; READY-but-unsubmitted assets are
   re-verified (stat + hash); then every enabled source is reconciled.
7. **Manual intake convergence**: "import this folder into the session" is a
   `manual` source reconciled once on demand, going through the same states.
8. **Scheduling loop**: a Qt-free engine object with `tick(now)` (pure step,
   injectable clock and filesystem adapter for tests) and a runner that ticks
   on an interval in a background thread of the coordinator. It emits
   nothing Qt; the GUI (phase D) will wrap it.
9. **Optional notifications**: only if cheap and dependency-audited; used
   solely to trigger an earlier reconciliation. Correctness must hold with
   them off. It is acceptable — and the default expectation — to ship
   reconciliation only and document notifications as future work.

## 4. Out of scope

- Creating processing units or running recognition (phase C). READY is the
  end of this phase's pipeline.
- Conflict sync, quality decisions, rescan/replacement (C).
- Any GUI (D).
- Changing the finite Scan-page workflow.
- The real SMB qualification (F) — but design for it.

## 5. Persistence expectations

- Use Phase A's schema. If a new column is genuinely required, add migration
  11 (additive), with its own upgrade test, and justify it in the handoff.
- Asset state transitions are durable before the next step relies on them;
  a kill at any point leaves a state from which restart recovery (item 6)
  proceeds correctly.
- Writes are batched per reconciliation pass (bounded transaction size) and
  never held open across hashing or listing.

## 6. Tests required

- **Unit**, with a fake clock and fake filesystem: every state transition;
  quiet-period edge cases; files that grow in steps, pause longer than *T*,
  then grow again; files held open; header-first writes; zero-byte files;
  rename-into-place (`scan.tmp` → `scan.jpg`); deletion before ready;
  reappearance; path reuse; duplicates within and across sources;
  exclusions; unreachable → reachable; restart mid-stabilisation.
- **Integration**, with real temporary directories and real writer threads/
  processes: three sources writing `000001.jpg…` concurrently; a writer
  process killed mid-file; the engine stopped for a period while writers
  continue, then restarted — every file discovered exactly once, no partial
  file READY; original hashes unchanged after intake.
- **Property/soak** (marked `stress` if long): randomised arrival schedules
  over ≥ 2,000 files; assert exactly-once discovery and no premature READY.
- **Measurement**: record listing time for 1k/10k-file directories locally,
  and stabilisation latency distribution, in the handoff. These justify the
  default parameters.
- **Regression**: entire existing suite unchanged; Phase 10 harness self-test
  passes.

## 7. Verification

```powershell
.venv\Scripts\python.exe -m ruff check src tests tools scripts
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest -q -m stress -k intake   # the soak tests you add
```

No GUI tests are expected; the existing GUI suite must still pass.

## 8. Documentation updates

- New `docs/intake.md`: sources, states, stabilisation rules and parameters,
  collision rules, reachability, restart, what is and is not guaranteed.
- `docs/ARCHITECTURE.md` intake section; `docs/DATA_MODEL.md` if schema moved.
- `development/releases/0.1.1-alpha.0/PHASE_B_HANDOFF.md`, ROADMAP.md §5 status.
- `development/CURRENT_STATE.md`, `docs/wiki/Development-Roadmap.md`.
- **README Development status / Testing status**: a `0.1.1-B` row — phases
  completed, phases under testing, automated-test status, synthetic-testing
  status, **network-share status: not yet validated**, real-data status,
  pending phases.
- `CHANGELOG.md` `[Unreleased]`.

## 9. Status reporting

Report separately: implementation complete; automated tests complete;
synthetic validation (local temporary-directory soak only — say so; the full
campaign is phase F); **network-share validation: not performed** (the SMB
qualification is phase F); real scanner validation: not performed; production
qualification: not performed. Local-disk tests say nothing about SMB `mtime`
caching or share disconnects — state that explicitly. Passing tests do not
complete the phase's validation tracks.

Commit in small commits, push the branch, do not merge unless asked.
