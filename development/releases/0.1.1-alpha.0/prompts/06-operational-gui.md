# Prompt 06 — `0.1.1-F`: Operational GUI

You are working in the OMRFlow repository. This is **phase 0.1.1-F** of the
`0.1.1-alpha.0` line. Phases A–E must be merged (their handoffs exist);
otherwise stop and say so. Branch: `feat/0.1.1-f-operational-gui`. **Use the
`qtguitesting` skill** for this phase.

Plan: `ROADMAP.md` §2 and §5 F; `ARCHITECTURE_NOTES.md` §§6.2, 8.2, 14;
`ACCEPTANCE_CRITERIA.md` §2 F, §3, §4. The code is the fact; record
discrepancies.

The finite workflow — one implicit session, one batch — must look and behave as
it does today. Session management appears only when an operator uses it.

---

## 1. Inspect first

- Phase A–E handoffs; `docs/intake.md`, the rescan documentation
- The session services: `services/scan_sessions.py`, `services/intake.py`, the
  unit scheduler / runner, the session snapshot, controls, finish validation,
  suggested rejections, the effective-set service
- `gui/scan/page.py`, `gui/scan/table_model.py`, `gui/scan/worker.py`, the
  progress panel and its 200 ms snapshot timer
- `gui/review/page.py`, `gui/review/rescan.py`, `gui/review/worker.py`
- `gui/main_window.py` (project adoption, `recover_interrupted`, close handling,
  the `_prompt_*` vs testable-method split, and the modal-from-worker-callback
  hang fixed twice before — do not reintroduce it)
- `gui/theme/`, `gui/widgets/`, `gui/icons.py`, `docs/ui_reference/`
- tests: `tests/gui/test_scan_page.py`, `test_scan_persistence.py`,
  `test_batch_progress_gui.py`, `test_resolve_page.py`, `test_main_window.py`,
  `tests/gui/conftest.py`, `tests/unit/test_architecture.py`

Run the full suite first; record the baseline.

## 2. Preserve

- With an implicit single-batch session, the Scan stage is the
  `0.1.0-alpha.2` workflow (Add Scan(s)/Add Folder → Process All/Selected →
  Resume/Retry → Export CSV) and every existing GUI test passes unchanged.
- The GUI imports no OpenCV, NumPy or SQLAlchemy; services import no Qt.
- Progress is pulled on a timer from immutable snapshots, never pushed.
- Modal dialogs only in `_prompt_*` methods, never in testable commands or
  worker callbacks.
- The per-batch progress panel remains for the running unit.
- Everything in `prompts/README.md` "Rules every prompt repeats".

## 3. Scope — exactly this

1. **Session mode on the Scan stage**: create/open a session; configure sources
   (label, folder incl. UNC, kind, enabled); start; the runner in a QThread
   wrapper around the Qt-free services.
2. **Live counts** from the snapshot: Discovered, Stabilizing, Waiting,
   Processing, Accepted, Conflicts, Rescan required (Duplicates / Superseded
   where useful).
3. **Three separate progress lines** — Recognition (may decrease when the
   workload grows), Conflict resolution, Rescan. **No combined percentage
   anywhere.**
4. **States and wording**: Processing; "Caught up — watching for new scans"
   only under its definition; "Waiting for *Scanner B* (unreachable since …)";
   "Processing paused"; Closed. Nothing may imply the examination is complete.
5. **Per-source status** — compact, secondary, collapsible: received, accepted,
   conflicts, rescan, duplicates, last file, reachability, rate alarm.
6. **Actions**: Pause/Resume Processing; pause/resume a source; Finish current
   and stop; **Finish scan session** with a dialog listing every blocker; reopen
   (audited, confirmed).
7. **Scan list** SQL-paged (a session makes it unbounded); *Source* and *Batch*
   columns and filters; held / duplicate / unreadable lists.
8. **Resolve while scanning**: the session queue refreshes incrementally as
   conflicts appear; cross-batch duplicates navigable; batch/source filter.
9. **Rescan queue**: outstanding items; **suggested rejections** from the
   quality decision layer to confirm or dismiss with reviewer name; suggested
   replacements to confirm; undo; unmatched replacements; evidence using the
   existing overlays.
10. **Session named in every stage header**; provisional labels from C visible
    where results are shown; final outputs made stale by a reopen shown as
    stale.
10a. **Reopening interrupted work**: the Scan stage and Resolve show the
    reconstructed state (per session, batch and source) from committed rows
    before any Resume is pressed; a Resume action where appropriate; no stored
    progress-bar value is used (ARCHITECTURE_NOTES.md §13.7).
11. **Close/exit with a session running**: stop the runner, wait, flush, then
    release the database (the existing batch close order).
12. Short, non-modal notes where quality-decision defaults are unvalidated,
    linking to the docs.

## 4. Out of scope

- New recognition, policy or threshold behaviour; scanner control; multiple
  OMRFlow instances; charts or remote dashboards; redesign of unrelated pages.

## 5. Persistence expectations

The GUI stores no state that matters: every count, state and decision comes
from services and survives restart. Per-viewer conveniences (a collapsed panel,
column widths) may use the existing user configuration. Test that closing and
reopening the project mid-session restores the same view.

## 6. Tests required

- **GUI (pytest-qt, offscreen)**: single-batch Scan stage identical; source
  configuration; every state and wording; decreasing recognition line;
  caught-up vs unreachable; pause/resume; each finish blocker and a clean
  finish; reopen; Resolve and Rescan queues updating during intake; confirm and
  dismiss a suggested rejection; confirm/undo replacement; close while running;
  reopening a project after a **real kill** mid-session shows the reconstructed
  counts before Resume.
- **Responsiveness**: event-loop latency with 10,000 files and continuing
  arrivals; threshold recorded in the handoff; 100k-row scan-list paging.
- **Screenshots** via `qtguitesting` for every new state, at 1366×768 and 175 %
  scaling.
- Whole suite unchanged, especially the Scan, persistence, progress and Resolve
  GUI tests; `test_architecture.py` passes.

## 7. Verification

```powershell
.venv\Scripts\python.exe -m ruff check src tests tools scripts
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest -q tests/gui
```

Plus one **manual run from source** with three local folders written by a
script, with a screenshot sequence in the handoff.

## 8. Documentation updates

`docs/scan_workflow.md` ("Scan sessions" part); `docs/wiki/Scanning.md`,
`Processing.md`, `Review-and-Resolution.md`, `User-Guide.md`,
`Known-Limitations.md`; `docs/ui_reference/`; `ROADMAP.md` §7; new
`PHASE_F_HANDOFF.md`; `CURRENT_STATE.md`; README Development/Testing status (the
README screenshot changes only if the default Scan view changed — it should
not); `docs/wiki/Development-Roadmap.md`; `CHANGELOG.md`.

## 9. Status reporting

Separately: implemented; tested (incl. GUI); synthetic validation — local manual
run only, not the campaign; network share not performed; real scanners not
performed; production not performed. No operator has used it in a scanning
room — say so. Passing GUI tests are not usability evidence.

Commit in small commits, push the branch, do not merge unless asked.
