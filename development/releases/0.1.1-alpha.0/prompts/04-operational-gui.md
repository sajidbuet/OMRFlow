# Prompt 04 — `0.1.1-D`: Operational GUI

You are working in the OMRFlow repository. This is **phase 0.1.1-D** of the
`0.1.1-alpha.0` line. Phases A–C must be merged (their handoffs exist);
otherwise stop and say so. Branch: `feat/0.1.1-d-operational-gui`.

Plan: `development/releases/0.1.1-alpha.0/ARCHITECTURE_NOTES.md` §§14–15,
`ACCEPTANCE_CRITERIA.md` §2-D and §3. The code is the fact; record
discrepancies. **Use the `qtguitesting` skill** for this phase.

---

## 1. Inspect first

- Phase A–C handoffs; `docs/intake.md`, `docs/rescan.md`
- The session services from A–C: intake engine runner, unit scheduler,
  snapshot service, pause/resume, finish validation, rescan/replacement API
- `gui/scan/page.py`, `gui/scan/worker.py` (`BatchWorker`, `PreviewWorker`),
  the progress panel and its 200 ms snapshot timer
- `gui/review/page.py`, `gui/review/worker.py`
- `gui/main_window.py` (project adoption, `recover_interrupted` call, close
  handling, the `_prompt_*` vs testable-method split, and the
  modal-from-worker-callback hang fixed twice before — do not reintroduce it)
- `gui/theme/`, `gui/widgets/`, `gui/icons.py`, `docs/ui_reference/`
- `services/batch_progress.py` (the pull-snapshot pattern)
- tests: `tests/gui/test_scan_page.py`, `test_scan_persistence.py`,
  `test_batch_progress_gui.py`, `test_resolve_page.py`, `test_main_window.py`,
  `tests/gui/conftest.py`, `tests/unit/test_architecture.py`

Run the full suite first; record the baseline.

## 2. Preserve

Everything in prompt 01 §2, and specifically:

- **With no session open, the Scan stage is the `0.1.0-alpha.2` workflow**:
  Add Scan(s)/Add Folder → Process All/Selected → Resume/Retry → Export CSV,
  and every existing GUI test passes unchanged.
- The GUI never imports OpenCV, NumPy or SQLAlchemy; services never import Qt.
- Progress is pulled on a timer from immutable snapshots, never pushed per
  event.
- Modal dialogs live only in `_prompt_*` methods, never in testable commands
  or worker callbacks.
- The existing per-batch progress panel remains for finite batches and inside
  a session's current unit.

## 3. Scope

1. **Scan session mode** on the Scan stage: create/open a session; configure
   sources (label, folder incl. UNC path, kind, enabled); start; the runner
   (intake engine + unit scheduler) lives in a QThread wrapper around the
   Qt-free services.
2. **Live counts** (ARCHITECTURE_NOTES.md §14): Discovered, Stabilizing,
   Waiting, Processing, Accepted, Conflicts, Rescan required (and Duplicates /
   Superseded where useful) — from the snapshot service.
3. **Three separate progress lines** — Recognition (`3,476 / 3,527
   processed`, allowed to decrease when workload grows), Conflict resolution
   (`31 / 39 resolved · 8 remaining`), Rescan (`10 / 14 replaced · 4
   remaining`). **No combined percentage anywhere.**
4. **States**: Processing; "Caught up — watching for new scans" only under its
   definition; "Waiting for *Scanner B* (unreachable since …)"; "Processing
   paused"; Closed. Wording must never imply the examination is complete.
5. **Per-source status** — compact, secondary, collapsible (e.g. a small
   table under a disclosure): received, accepted, conflicts, rescan, duplicates,
   last file received, reachability, rate alarm. Must not dominate the
   workspace.
6. **Actions**: Pause Processing, Resume Processing, optionally pause/resume a
   source's watching, **Finish Scan Session** with a dialog listing every
   blocker (§3 of the acceptance criteria) and closing only when none remain;
   reopen a closed session (audited, confirms).
7. **Resolve while scanning**: the Resolve queue refreshes as conflicts appear
   (incrementally; no full reload per tick), scoped to the session, with
   cross-sheet duplicates navigable across units.
8. **Rescan queue**: outstanding items; suggested replacements; confirm /
   reject association with reviewer name; undo; unmatched replacements;
   evidence (quality reasons, affected regions) reusing existing overlays.
9. **Responsiveness at scale**: any list that grows with the session is a
   paged/lazy Qt model, not a `QTableWidget` of every row. Measure event-loop
   latency with 10,000 assets and ongoing intake.
10. **Close/exit** with a session running: stop the runner and wait for it,
    flush, then release the database (the existing batch close order).
11. Clear, non-alarming text for unvalidated quality defaults (a short note
    and a link to the docs, not a modal).

## 4. Out of scope

- Session-scoped Attendance/Results/Reports (E).
- New recognition or policy behaviour; new thresholds.
- Scanner control; multiple OMRFlow instances.
- Redesigning the application shell or unrelated pages.

## 5. Persistence expectations

The GUI stores no state of its own that matters: every count, state and
decision comes from services and survives restart. Per-viewer conveniences
(a collapsed panel, column widths) may use the existing user configuration.
Test that closing and reopening the project mid-session restores the same
view of the session.

## 6. Tests required

- **GUI tests (pytest-qt, offscreen)**: no-session Scan stage identical;
  source configuration; each state and its wording; progress lines including
  a decreasing recognition percentage; caught-up vs unreachable; pause/resume;
  each finish blocker individually and a clean finish; reopen; Resolve and
  Rescan queues updating during intake; confirm/undo replacement; close while
  running.
- **Responsiveness**: event-loop latency under a simulated 10,000-asset session
  with continuing arrivals; threshold recorded in the handoff.
- **Screenshots** via the `qtguitesting` workflow for every new state, stored
  per repository convention.
- **Regression**: entire existing suite unchanged, especially
  `test_scan_page.py`, `test_scan_persistence.py`, `test_batch_progress_gui.py`,
  `test_resolve_page.py`; `test_architecture.py` passes.

## 7. Verification

```powershell
.venv\Scripts\python.exe -m ruff check src tests tools scripts
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest -q tests/gui
```

Plus one **manual run from source** with three local folders written by a
script, and a short screen recording or screenshot sequence in the handoff.

## 8. Documentation updates

- `docs/scan_workflow.md`: a "Scan sessions" part; correct §14's stale "no
  manual correction" line while there.
- `docs/wiki/Scanning.md`, `Processing.md`, `Review-and-Resolution.md`,
  `User-Guide.md`, `Known-Limitations.md`: operator-facing session workflow.
- `docs/ui_reference/` for the new widgets.
- `PHASE_D_HANDOFF.md`, ROADMAP.md §5, `CURRENT_STATE.md`,
  `docs/wiki/Development-Roadmap.md`, `CHANGELOG.md`.
- **README Development status / Testing status**: `0.1.1-D` row; phases
  completed / under testing, automated (incl. GUI), synthetic, network-share,
  real-data status and pending work. Update the README screenshot only if the
  default (no-session) Scan view changed — it should not have.

## 9. Status reporting

Report separately: implementation complete; automated tests complete (incl.
GUI); synthetic validation (local manual run only — not the §4 campaign);
network-share validation not performed; real scanner validation not
performed; production qualification not performed. An operator has not used
it in a scanning room — say so. Passing GUI tests are not usability evidence.

Commit in small commits, push the branch, do not merge unless asked.
