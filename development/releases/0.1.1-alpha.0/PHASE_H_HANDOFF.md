> `PHASE_H_HANDOFF.md` is the handoff for revised Phase 8, implementing roadmap phase 0.1.1-F Operational GUI.

# Revised phase 8 handoff — operational GUI for continuous multi-source scanning (roadmap 0.1.1-F)

Date: 2026-10-05. Branch `feat/0.1.1-phase8-operational-gui` (worktree
`C:\Research\OMRflow-p8`), based on `main` at `9169933` (revised phase 7
merged). **Not merged. No migration: schema stays 17.** `PHASE_G_HANDOFF.md`
is not modified.

The phase's rule, kept throughout: **render and control the phase 7 services;
do not reimplement them in Qt.** The GUI computes no partition, no blocker, no
caught-up state, no quality decision and no rescan ranking; it shows what the
services return and calls them for every action.

## 1. Status

| Track | Status |
|---|---|
| Implementation | ✅ Complete for the brief's scope, with the limitations in §20 |
| Automated tests (unit / integration) | ✅ passing (§18) |
| GUI tests | ✅ passing - controls, finish / reopen, queues, live Resolve, shutdown order, real-kill reopen, 10,000-sheet responsiveness with arrivals, 100,000-row paging (§18) |
| Native / scripted GUI validation | 🟠 **scripted only**: native screenshots of 16 states at 175 % Windows scaling (1366×768, 1100×680; interface zoom 80–200 %) and one scripted local three-folder run with the production worker pool (§16, §17); pre-merge correction: native captures of Answer Key and Resolve provenance at 150 % (1100×680), DPR 1.395 (1366×768) and DPR 1.75 (largest window that fits), zoom 100 / 150 / 200 % (§19a). **No person operated the application.** |
| Network share | ⚪ not performed (UNC paths are accepted and an unreachable one is shown; no SMB test) |
| Real scanner | ⚪ not performed |
| Power loss | ⚪ not performed |
| Production qualification | ❌ not qualified |

## 2. Git state

| | |
|---|---|
| Baseline | `main` `9169933` (Merge feat/0.1.1-phase7-quality-session-controls) |
| Branch | `feat/0.1.1-phase8-operational-gui` |
| Tip | see §18 (the commit carrying this handoff) |
| Commits | 14 code / test / tooling commits, then documentation commits (listed below) |
| Pushed | see §18 |
| Merged | **no** - not merged, not tagged, not released |
| Working tree | clean after the documentation commit (generated screenshots live under the git-ignored `test-output/`) |

```
39abbbe feat(services): paged session sheet list and per-source snapshot counts
f1ea203 feat(gui): continuous-engine QThread runner, snapshot poller, finish/reopen adapter
6bdb805 feat(gui): session mode on the Scan stage
b5d01d3 feat(gui): Resolve while scanning - suggested rescans, waiting files, live refresh
eeb2912 feat(gui): window integration - shutdown order, navigation, session context
69b77bc test: fix two fixture defects found while building on phase 7
8311250 perf(gui): read the session sheet list off the GUI thread
de6a2f6 feat(gui): live Resolve refresh off the GUI thread; original file names
7a522a7 test(gui): finish/reopen, rescan and file queues, recovery, responsiveness
271d875 chore: three-folder scanner feeder and session-mode screenshot script
4652468 fix(gui): Resolve anchored by a live session re-reads sheets with the project template
50463b1 fix(gui): session panel wording for closed and not-running sessions
76f4726 chore(gui-testing): realistic capture rig and a scripted three-source production run
474dc9c fix(gui): build the session panel's progress lines only when a session is shown
5c7c920 docs: revised phase 8 operational GUI - handoff, status tracks, user docs
```

(+ the final commit recording the gate results in this handoff and the README.)

Baseline gate on pristine `main` `9169933` before any change (canonical
`pytest-ruff-mypy.ps1`): **7,030 passed, 16 skipped, 1 failed**; `ruff` clean;
`mypy` with a fresh cache clean (233 files). The one failure was a test defect
(§19, fixed on this branch).

## 3. UI architecture

```
             GUI thread                                worker threads
 ┌───────────────────────────────┐
 │ ScanPage                      │      SessionRunner ── EngineThread (QThread)
 │  ├ SessionPanel (render only) │ ◄──── signals ─────  ContinuousEngine (Qt-free, phase 6/7)
 │  ├ SessionSheetList (page)    │ ◄──── page_loaded ─  _PageWorker   (count + 1 page, SQL)
 │  └ SessionModeController ─────┼────── commands ───►  queue.SimpleQueue → engine thread
 │                               │ ◄──── SessionView ─  SessionPoller  (take_snapshot, 1 s)
 │ ResolvePage                   │ ◄──── LiveQueueData  LiveQueueReader (conflicts, summary,
 │  ├ SuggestionPanel / FilePanel│                       population, sources, session)
 │  └ live refresh (apply only)  │      _FinishThread  (finish_scan_session when no engine)
 └───────────────────────────────┘
```

* **Engine wrapper** (`gui/scan/session_runner.py`). `ContinuousEngine` stays
  Qt-free. `EngineThread` (a `QThread`) builds it through a factory *inside*
  the thread, starts it (the coordinator lease is taken there; a
  `CoordinatorBusyError` becomes the `busy` signal and a notice, never a
  crash), and loops `poll_intake` / `form_units` / `step`, woken by a
  `threading.Event`. The GUI never calls the engine directly: *Finish Current
  and Stop*, *Cancel Queued Work* and *Finish Scan Session* (while it runs) are
  commands put on a `queue.SimpleQueue` and executed by the engine's own
  thread. `SessionRunner.shutdown(timeout)` asks for the safe stop and waits.
* **Snapshot polling** (`gui/scan/session_poller.py`). A worker object in its
  own `QThread` calls `session_snapshot.take_snapshot` (plus the session row
  and the source list) every `POLL_INTERVAL_MS = 1000`. **At most one read in
  flight and at most one owed**: a refresh asked for while one runs is
  remembered once, never queued. Results carry a generation; a stale one
  (after a project switch) is dropped.
* **Ownership / cleanup.** Every thread is owned by its page or controller and
  joined in a fixed order on project close, project switch and window close:
  **poller stop → sheet-list worker stop → runner shutdown (engine finishes
  current work, releases the coordinator lease) → finish thread wait →
  database closed by the window**. Superseded preview / sheet loaders are kept
  in lists until they finish (a running `QThread` destroyed with its parent
  aborts the process). A test enumerates the application's named threads
  after rapid project switches and after window close (none left).
* **Service adapters.** `gui/scan/session_mode.py` (the controller) maps each
  action to exactly one service call: `session_controls.pause_processing /
  resume_processing / set_intake_paused / set_source_paused`,
  `intake_service` source add / edit / enable / detach, `session_finish`
  through `gui/session_close.py` (`preview_blockers`, `reopen`),
  `quality_decisions.confirm_suggestion / dismiss_suggestion`,
  `intake_decisions.decide_file`, `scan_lifecycle.session_possible_rescans /
  confirm_replacement / remove_replacement`. Dialogs open only in `_prompt_*`
  methods, never from a worker callback (a finish outcome that needs a dialog
  is scheduled with `QTimer.singleShot(0, ...)`).
* **No duplicated business logic** - enforced by
  `tests/unit/test_operational_gui_rules.py` (AST): the new GUI modules import
  no OpenCV / NumPy / SQLAlchemy; services import no Qt; no GUI module calls
  `close_scan_session`; `session_finish` is called only through the close
  adapter. The one new service, `services/session_sheets.py`, is read-only
  SQL for the list (and original-name lookup); it decides nothing.

## 4. Finite mode

Unchanged, by construction and by test
(`test_session_scan_gui::TestFiniteModeIsUnchanged`, and every pre-existing
Scan / Resolve GUI test passing unmodified):

* session mode is **active only** when the active session has a watched
  source attached, or this window's engine runs; an implicit finite session
  never activates it;
* no session panel, no *List* chooser, the batch list as before, **no polling
  thread created**;
* *Add Scan(s) / Add Folder → Process All / Selected → Resume / Retry →
  Export CSV*, their tooltips and the finite worker are untouched;
* the only new finite-mode item is *Session → Add Scanner Source...* - the way
  in, never required.

While continuous scanning runs, the finite run buttons are disabled with a
tooltip saying why (one coordinator per project; the phase 7 lease would
refuse anyway).

## 5. Session mode

1. *Session → Add Scanner Source...* (or *Add Source...* on the panel):
   label, folder typed / pasted / browsed (UNC accepted; an unreachable path
   is kept), recursive. The active session is created only if none exists.
2. The panel appears above the workspace; *Start Continuous Scan* builds the
   production engine (`production_engine_factory`: a warm
   `ProcessRecogniser` pool and `IntakeService` on the real disk) in the
   runner thread.
3. The poller renders the snapshot every second: status line, three progress
   lines, counts, warnings, sources.
4. The operator reviews on Resolve meanwhile (§9-§11).
5. *Finish Scan Session...* → grouped blockers → clear them (navigation) →
   attempt → closed; or *Reopen Session...* later.

Closing OMRFlow / the project stops scanning safely and leaves the session
**open**; the exit question says so and defaults to *Cancel*.

## 6. Progress / status

* **Three lines, never one percentage**: *Recognition* `read / discovered`
  (it falls when files arrive - the bar's maximum is the current total),
  *Conflicts* `resolved / required` (or *none*), *Rescans* `replaced / needed`
  (or *none needed*). No combined figure is computed anywhere.
* **Counts**: Discovered, Stabilizing, Waiting (ready + queued, with the split
  in a tooltip), Being read, Accepted, Conflict, Rescan required; the other
  partition buckets (Duplicate, Superseded, Held, Unreadable / unsupported,
  Deferred, Excluded, Counted in another session, Vanished) only when non-zero.
  All values are the snapshot's.
* **Activity** is `SessionActivity.label` from the snapshot - *Processing*,
  *Caught up - watching for new scans*, *Waiting for Scanner B (unreachable
  since 10:42)* (the GUI only names the source and time the snapshot reports),
  *Processing paused*, *Intake paused*, *Checking scanner sources*. Two GUI
  qualifications, never a different state: *Processing (not running in this
  window)* when work is outstanding but no engine here reads it, and no
  activity word when it would only repeat *CLOSED*. **Caught up is never
  called complete**; a test asserts the word *complete* never appears.
* **Source status**: *Watching*, *Paused*, *Unreachable since HH:MM*,
  *Disabled*, *Checking*, with received / read / accepted / conflict / rescan /
  duplicate / last check · rate, and an alignment marker when that source's
  registration-failure alarm is raised (shown as an **uncalibrated warning**,
  not a diagnosis).

## 7. Sources

*Edit...* (label; recursive; the path is read-only after creation - a new path
is a new source), *Enable / Disable*, *Pause / Resume Source*, *Remove from
Session...* (ends future intake; nothing delivered is removed). Errors from the
intake service are shown in the dialog, which stays open. The table is a folded
*Sources (n)* section with a one-line summary (*3 source(s) · 1 unreachable*)
when folded. **Not exposed:** the stability policy (quiet period) per source -
the service default is used (§20).

## 8. Scan list

`gui/scan/session_table_model.py` over `services/session_sheets.py`:
`PAGE_SIZE = 200`; columns Status, Student ID, Set, Source, Batch, Original
file, Quality, Conflict, Rescan; filters status, source, batch, quality,
conflict state, rescan state; search by Student ID or **original** file name
(never the content-addressed copy); sort by a column header in SQL (Conflict
and Rescan keep the status order - sorting them would need a per-row subquery
over the whole session). Count and page are two bounded statements that join only the tables a
query needs; the page is read as ids first, then ≤200 rows decorated. All reads
run in a worker thread with generation coalescing; the table only applies.
The list refreshes from snapshot changes at most every
`SHEET_LIST_REFRESH_SECONDS = 2.0`.

## 9. Resolve while scanning

* The main window relays each `SessionView` to Resolve. Resolve anchors itself
  to the active session when it has nothing loaded (it now loads the project's
  template for that - §19), and refreshes only when the snapshot's queue
  signature changed, at most every `LIVE_MIN_INTERVAL_S = 2.0`, and **never
  under the operator**: a staged choice, the whole-field editor, focus in a
  text field or an open dialog defers it (caught up on the next tick or on
  show). Hidden → deferred until shown.
* The reads (conflict page, summary counts, session population, sources,
  session row, batch summary, original names) run in `LiveQueueReader`'s
  thread; applying them touches no database. The selected conflict is kept by
  id; a conflict decided elsewhere is replaced deliberately.
* Filters: **Batch** (all batches of the session or one) and **Source**
  (watched scanners). Each sheet's provenance (scanner · batch · original name
  · arrival) is shown beside the evidence tabs, with the stored copy's name in
  the tooltip. Every row, candidate and caption names watched-source sheets by
  the name they arrived with.
* Undo controls are cached against `review_store.history_watermark` (the
  newest audit event id) instead of recomputed on every selection (measured
  934 → 36 ms per selection change at 10k before / after).

## 10. Rescan queue

* **Suggested rescans** view: `quality_decisions.outstanding_suggestions`;
  details show evidence, suggested reason, scanner, batch, original name and
  the policy, marked **UNVALIDATED DEFAULT**, with the fixed note *Rescan
  suggestions use an unvalidated default quality policy. Operator confirmation
  is always required.* *Confirm Rescan Required...* (reason prefilled; Cancel
  default) → `confirm_suggestion`; *Dismiss Suggestion...* → 
  `dismiss_suggestion`; both need the reviewer name. Nothing is rejected or
  replaced by the GUI on its own.
* **Rejected / Rescan**: in a live session candidates come from
  `session_possible_rescans` (any scanner, ranked by the service), each line
  with its provenance (*from Scanner b; arrived after the rejection*) and a
  tooltip saying *a suggestion ranked by Student ID, set and arrival - not a
  certainty*. *Use as replacement* → `confirm_replacement`; *Remove
  replacement link...* (asks first) → `remove_replacement`. Tested across
  scanners including undo.

## 11. Held-file decisions

*Files awaiting decision* view: `intake_decisions.pending_decisions`; the
buttons offered are exactly `intake_decisions.OPTIONS[state]`; each calls
`decide_file` for the named reviewer. A file **held** because it arrived for a
closed session shows *Release* disabled with *Reopen the session first*; after
reopen it can be released. Tested with an unreadable file, a dismiss, a retry
and a closed-session arrival that adds nothing to the closed population.

## 12. Controls

| Control | Service / command | Notes |
|---|---|---|
| *Start Continuous Scan* | runner start (engine built in its thread) | Refused with the reason while a finite run holds the lease |
| *Pause / Resume Processing* | `session_controls.pause_processing` / `resume_processing` | Persisted; caption follows the stored intent from the next snapshot; resume starts the engine here if it is not running |
| *Stop → Finish Current and Stop* | command to the engine thread (`finish_current_and_stop`) | The normal safe stop |
| *Stop → Cancel Queued Work...* | command (`cancel_queued_and_stop`), named operator | Asks first (Cancel default); nothing deleted |
| *Pause / Resume Intake* | `set_intake_paused` (session) / `set_source_paused` (source) | A paused source is not an unreachable one |
| *Add Source...* | intake service | §7 |

Captions and enablement are re-derived from the snapshot and the runner state,
never flipped optimistically by a click.

## 13. Finish / reopen

*Finish Scan Session...* opens `FinishSessionDialog` with the read-only
`session_close.preview_blockers`, **every** typed blocker grouped (Processing,
Sources, Review, Rescans, Files awaiting a decision, Attendance, Session) with
*Go to ...* buttons
(`navigate_to` → the Resolve view, Attendance, the Sources section).
Acknowledgement is offered only when **all** remaining blockers are of the
three acceptable kinds (outstanding rescans, unmatched replacements, deferred
sheets), as the named operator. The attempt runs `finish_scan_session` through
the one closure policy - in the engine's thread while it runs (its final
reconciliation is the coordinator's), otherwise in `_FinishThread`. A refusal
re-lists what blocks. Closed: *Reopen Session...* (named, reason, confirm) →
`session_close.reopen`; results become provisional and the panel, Results and
Reports say final exports made while closed are stale. Re-closing runs every
check again (tested).

## 14. Crash / reopen behaviour

`test_session_recovery_gui` kills a real engine child process (the phase 7
crash harness, `TerminateProcess` inside a commit), then opens the project in
the real main window: the panel shows the committed counts (`read` equal to
the rows counted in the database), *Processing: running* (the persisted
intent), *Continuous scanning is not running in this window* and *Processing
(not running in this window)*, **before** anything starts; no session or
batch is created; Resolve has its queue without visiting Scan. *Start* then
completes the same session (10 / 10). A persisted pause is shown after reopen
(*Processing paused*, *Resume Processing*, *Resume Intake*).

## 15. Responsiveness

| Measurement | Result |
|---|---|
| Snapshot refresh interval | 1 s, one read in flight, at most one owed |
| Snapshot at 10,200 rows (phase 7 scale fixture, now with conflicts) | ≈ 98 ms, 11 statements |
| 10k scenario (`test_operational_gui_responsiveness`) | 10,000 metadata sheets in one session (no images, no recognition) **plus** a thread committing new arrivals throughout; the real main window drives Scan and Resolve actions (page turns, filters, view switches, decisions); a 20 ms heartbeat timer measures event-loop lateness |
| Thresholds (asserted) | median < 30 ms, p95 < 150 ms, worst < 2,000 ms |
| Measured at the tip (alone, idle machine, 2026-10-05) | 10,000 sheets + 168 arrivals in 12 s; 19 snapshot polls; heartbeat 20 ms × 527: lateness **median 0.0 ms, p95 8.0 ms, worst 442 ms**; 28 operator actions: median 0.5 ms, worst 51.5 ms; last list read 182 ms and last live Resolve read 576 ms (both in worker threads) |
| Measured during development (same test, often under load) | p95 fell 144 → 92 → 81 → 70 → 15 ms as reads moved off the GUI thread; worst ≈ 1.0–1.2 s |
| Scripted production run (§17) | 1,560 samples of a 50 ms timer: median 12 ms, p95 23 ms, worst 597 ms (repeat: worst 555 ms); the driver's own 10 ms sleeps are included in these numbers |
| 100k paging (`test_scan_paging_gui`, 100,000 metadata rows) | end to end in the worker (request → read → table), at the tip, alone: **first page 269 ms** (GUI-side peak traced allocation 1.5 MiB), next page 81 ms, status filter 70 ms, quality filter 115 ms, sort 184 ms, search 332 ms. Earlier, under suite load: first page ≈ 394 ms, sort ≈ 319 ms, search ≈ 554 ms. Ceiling 2,000 ms per operation; the model holds one page |
| `scripts/benchmark_session_list.py` at 100k | count / page statements 30–190 ms |

**The worst case (0.4 s alone, ~1 s under load)** is not GUI work: it is SQLite rollback-journal
locking - a GUI-thread write (an operator decision) waiting for the engine's
writer, or a read blocked by a pending lock. The journal mode is deliberately
not changed (no WAL). Resolve view switches at 10k take 0.5–3 s because the
session population is recomputed per call (phase 4 design, ≈ 0.16–0.25 s each,
several per switch) - recorded as a limitation, not hidden by a threshold.

## 16. GUI scaling

All on this machine's **native platform at 175 % Windows scaling**
(`devicePixelRatio` 1.75; Segoe UI metrics), by
`.claude/skills/qtguitesting/scripts/capture_session_states.py`; output and a
geometry report in `test-output/gui/session_states/` (git-ignored).

* **Window sizes**: 1366×768 for all 16 states; 1100×680 for the dense ones
  (finite unchanged, processing, unreachable, Resolve conflicts / suggestions
  / replacement candidate, finish blockers after an attempt, recovered).
* **States**: 01 finite unchanged, 02 idle open, 03 processing, 04 caught up,
  05 processing paused, 06 intake paused, 07 source unreachable, 08
  registration alarm, 09 Resolve conflicts live, 10 suggested rescans, 11
  replacement candidate, 12 file awaiting decision, 13 finish blockers
  (preview and after attempt), 14 closed, 15 reopened (Scan, Results,
  Reports stale), 16 recovered before Start.
* **Interface zoom**: 80, 100, 120, 150, 200 % at 1366×768; 100 and 200 % at
  1100×680 - Scan, Resolve and the finish dialog.
* **Findings fixed**: Resolve queue summary clipped (now wraps); *CLOSED
  Closed*; an orphan separator and blank line in the closed panel; the session
  heading repeating *Scan session*; the scanner named twice in provenance; the
  image caption naming the content-addressed copy; Resolve blank in a live
  session (a defect, §19).
* **Remaining, reported by the geometry check**: at **200 %** zoom the
  *Finish Scan Session...* button is outside the window at both sizes (the
  page scrolls horizontally; *Session → Close Session* reaches the same
  action); Qt's toolbar extension button is 4–5 px short of its hint at 150 /
  200 %. In state 09 (a Student ID conflict with its sheet loaded) the
  check reports Resolve's machine / manual / effective value labels 22 px
  tall against a 32 px hint (and at 1100×680 one choice button narrower than
  its hint): the decision panel gets 190 px of a 239 px minimum. **This is
  pre-existing**: the same scenario measured with `main`'s code at 1366×768 /
  175 % gives the identical 190 / 239 px and 22 / 32 px (scratch script,
  `resolve_panel_geometry.py`, both checkouts). At 1100×680 the finite Scan
  stage's *Recognised values* table header is truncated (pre-existing finite
  layout, not changed by this phase).

## 17. Manual / source GUI run

**No person operated the application.** What was run, scripted, from source,
on this machine (`.claude/skills/qtguitesting/scripts/drive_three_source_run.py`):

1. `scripts/dev_feed_scanner_folders.py prepare` - a project with the
   synthetic sheets' template; folders *Scanner_A / B / C* on the local disk.
2. The real `MainWindow` (1366×768, 175 %), operator *Dr. Operator*; three
   sources added through the controller's `add_source` (the dialog's call).
3. *Start Continuous Scan* with the **production** factory (warm
   `ProcessRecogniser` worker processes, real disk).
4. The feeder, in a separate process: 90 sheets round-robin at 3 / s, every
   10th written in three partial steps, *Scanner B*'s folder moved away for
   15 s mid-run (unreachable) and back, then 3 damaged sheets at A and their
   rescans at C.
5. During feeding: *Pause Processing* (≈ 5 s) → *Resume*; *Pause Intake*
   (4 s) → *Resume*.
6. Caught up: **93 / 93 read**; activities seen: *Processing*, *Waiting for
   Scanner B (unreachable since 01:09)*, *Caught up - watching for new scans*.
7. Resolve: 20 conflict rows, **0 named by a content hash**; 3 suggested
   rescans; evidence shown (screenshot).
8. *Finish Scan Session* attempted: refused with `unresolved_conflicts`,
   `rescan_suggested` (expected). *Finish Current and Stop* → engine stopped
   in < 1 s; session left open; window closed; **no child process left**.
9. Run twice; the same outcome both times. Screenshots and `report.json` in
   `test-output/gui/three_source_run/`.

Not exercised by the script: the source dialogs by hand, dragging / keyboard
navigation, a real network share, a real scanner, long sessions.

## 18. Full validation

All on this machine (Windows 11, Python from the main checkout's `.venv`,
`PYTHONPATH` = the worktree's `src`), in the worktree.

| Check | Result |
|---|---|
| Targeted (the 10 new phase 8 test files: 65 tests) | 65 passed (inside the full run below; also run in groups throughout development) |
| `tests/gui` alone (commit `5c7c920`) | **1,984 passed, 2 skipped, 0 failed**, 1 `stress` deselected (40 min 39 s) |
| Full pytest (canonical `pytest-ruff-mypy.ps1`, commit `5c7c920`, 2026-10-05) | **7,083 passed, 29 skipped, 0 failed**, 7 `stress` tests deselected by the default configuration (1 h 37 min) |
| ruff (`src tests tools scripts`) | clean |
| mypy, fresh cache (`.mypy_cache` deleted first) | clean - 242 source files |
| Working tree changed by the run | no |
| Native crash (`0xC0000409` or other) | none in any run this phase |

The first full run (commit `76f4726`) had **1 failure**:
`test_batch_progress_gui::TestETenThousandScans::test_the_interface_creates_no_widget_per_scan`
found four `QProgressBar`s on a **finite** Scan page - the session panel's
three were created even in finite mode. Fixed in `474dc9c` (built on the
first session view; the existing test is unchanged and passes; a finite-mode
assertion was added). Then the run above. The skips are the usual
environment ones (real-sheet fixtures absent from the worktree, LibreOffice
not installed, no symlink privilege, the schema-12 checkout variable unset,
one window-manager geometry skip).

Pushed: `origin/feat/0.1.1-phase8-operational-gui` (see the final commit list
with `git log main..origin/feat/0.1.1-phase8-operational-gui`). The commit
after `5c7c920` changes only this handoff and the README testing line.

### 18a. Validation of the pre-merge correction (§19a), commit `f19a250`

Second machine (Windows 11, display 1920×1080 at 150 %), worktree
`OMRflow-phase8` with its own `.venv` (editable install of the worktree).

| Check | Result |
|---|---|
| New tests | 11 Answer Key context + 14 provenance layout + 3 session-view consistency = 28, all passing |
| Targeted (16 files: Answer Key ×4, Resolve ×3, session ×5, close / export, main window, zoom, operational rules) | 417 passed, 1 skipped, **1 failed** → the torn session view (§19a item 3), fixed; its file then 26 / 26 with the new tests |
| `tests/gui`, run 1 (`07b121f`) | **native abort** `0xC0000409` after ≈ 45 min (§19a item 4) |
| `tests/gui`, diagnostic rerun (`07b121f`) | same abort at the same test (48 min) |
| `tests/gui` on unmodified `a637619`, same machine | 1,985 passed, 1 skipped, 1 deselected, 0 failed (47 min 17 s) |
| `tests/gui`, run 3 (`3e23192`) | no abort; **2,012 passed, 1 skipped, 1 deselected, 1 failed** (42 min 30 s) → the window-position test (§19a item 5), fixed in `f19a250` |
| **Full pytest** (canonical `pytest-ruff-mypy.ps1` under **PowerShell 7**, commit `f19a250`, 2026-10-05) | **7,113 passed, 27 skipped, 0 failed**, 7 `stress` deselected (1 h 55 min 05 s); includes all of `tests/gui` |
| ruff | clean |
| mypy, fresh cache (`.mypy_cache` deleted first) | clean - 244 source files |
| Git state changed during testing (gate's own check) | False |
| Native crash after the fixes | none (gate run and the capture runs) |

A first gate attempt at `f19a250` was started with **Windows PowerShell
5.1** (`powershell -File ...`) and stopped after 48 min 55 s: the script's
`$ErrorActionPreference = "Stop"` turns a native command's stderr line into a
terminating error under 5.1, so the first OpenCV warning a test prints
(`cv::PngDecoder::read_chunk user chunk data is too large`) killed the run.
Verified with a three-line probe (5.1 stops, PowerShell 7.6 continues). Not a
test failure; the gate must be run with `pwsh`. The script is unchanged.
No separate `tests/gui` run was made at `f19a250`: the gate's pytest includes
it. The phase 7 stress suite was not rerun (no engine, intake, persistence or
lifecycle service changed; §19a).

## 19. Defects found

1. **Resolve stayed blank in a live session** (this phase, fixed before
   commit `4652468`): when Resolve anchored itself to the session from the
   relayed snapshot no template came with it, so `_load_sheet_for` never
   re-read the sheet. It now falls back to the project's active template.
   Regression test: `test_session_scan_gui::...cross_scanner_duplicate`
   (asserts the evidence loads; failed before the fix, passes after).
2. **Pre-existing - phase 7 snapshot-scale fixture** (`tests/snapshot_population.py`)
   wrote conflicts of type `identifier_ambiguous`, which does not exist; phase
   7's 10,200-row timing therefore had no conflicts. Fixed
   (`identifier_multiple`); the snapshot remains ≈ 98 ms with them.
3. **Pre-existing - `test_kill_between_a_commit_and_its_duplicate_pass`**
   took the restarted child's pid as `max(pid)`; Windows pids are not
   monotonic (the baseline failure on `main`). Fixed: the last *started*
   event's pid.
4. Found and fixed during development (no release ever carried them):
   `SessionPanel.render` and `SessionRunner.thread` shadowed Qt methods
   (renamed); `StrEnum` combo data returned as plain `str`; search matched
   content-addressed copy names; quadratic `QTableWidget` fills with
   `ResizeToContents`; undo controls recomputed per selection; GUI-thread
   reads in the list and the live apply.

## 19a. Pre-merge correction (after `a637619`)

A final review found two phase-8-owned GUI inconsistencies. Both are fixed;
validating them exposed a timing-dependent phase 8 defect (item 3) and two
pre-existing test defects - one that aborted `tests/gui` natively (item 4),
one that depended on the window's screen position (item 5) - all fixed.
Nothing else was changed. Engine, intake, quality policy, lifecycle,
recognition and schema are untouched, so the phase 7 stress suite was not
rerun (its evidence stands).

Done in a separate worktree (`OMRflow-phase8`) on a second machine with its
own `.venv` (editable install of the worktree), display 1920×1080 at 150 %
Windows scaling.

1. **Answer Key named no scan session** (was a §20 limitation). The page now
   shows the one-line context Resolve uses - `Scan session <name> · open ·
   results provisional`, `· closed`, or `· open (reopened) · results
   provisional` - from a new shared helper, `gui/session_context.py`, which
   Resolve now uses too, so the two cannot drift apart. The session is the
   one the main window hands every downstream stage
   (`scan_sessions.downstream_session_id` when a project opens;
   `set_session` when the active session changes or a batch finishes, next
   to Attendance / Results / Reports); the page selects nothing of its own,
   and never by latest batch or `updated_at`. It re-reads the session when
   shown, so a close made from Reports appears. It is **context only**: the
   label's tooltip says keys belong to the project's sets; no schema,
   persistence or key ownership changed. Finite mode: an implicit session
   gets the same single wrapped line (no panel, no new step); a project
   with no session, or a batch outside any session, shows nothing.
   Tests: `tests/gui/test_answer_key_session_context.py` (11 - open, closed,
   reopened, close made elsewhere, no project, project close, project
   switch, follows the window like Results, keys unchanged, implicit finite
   session, batch outside any session).
2. **Resolve provenance clipped - and, measured, did worse** (was a §20
   limitation). The corner `QLabel` sized itself to its whole line; a tab
   widget sizes its corner from the corner's size hint, so a long line
   squeezed the evidence tabs and raised the page's minimum width. Measured
   on `a637619`, native 150 %, 1100×680, the long values below: at 100 %
   zoom the evidence **tab bar was 0 px wide** (hint 319); Resolve's minimum
   width was 2,193 px at 150 % and 2,897 px at 200 %. Now
   `gui/review/provenance_label.py` (`ProvenanceLabel`) asks for no more than
   the room beside the tabs and has no minimum width; it shortens in a fixed
   order - the scanner name first (to a stem), then the file name from the
   middle (start and extension kept), then the scanner name altogether - the
   batch and arrival time always whole; the tooltip lists every value
   complete on its own line (`Source`, `Batch`, `Original file`, `Stored as`,
   `Arrived`). The name shown is the arrival name; the content-addressed copy
   appears only in the tooltip as *Stored as*. Same measurement after the
   fix: tab bar 319 / 319 at every zoom, Resolve minimum 857 / 1,577 / 2,104 px
   at 100 / 150 / 200 % (the remainder is other Resolve content, see §20) -
   identical with a short or a long provenance line.
   Tests: `tests/gui/test_resolve_provenance_gui.py` (14 - in the real
   window at 1366×768 and 1100×680 × 100 / 150 / 200 %: inside the tab
   widget right of the last tab, tab bar keeps its hint, evidence width > 0,
   window and page minimum widths unchanged by a long line, shown text fits,
   tooltip complete, stored name never shown; the shortening order; short
   line whole; no sheet hides it). These tests configure the application
   (stylesheet, font) as start-up does, so they measure the same fonts alone
   and inside `tests/gui`, and assert layout invariants, not pixel values.

3. **Found while validating: a torn session view** (phase 8 code, fixed).
   The first targeted run of the correction (16 files: Answer Key, Resolve,
   session, close / export, main window, zoom, operational rules; 419 tests)
   gave 417 passed, 1 skipped, **1 failed**:
   `test_session_finish_gui::TestReopen::test_reopen_is_named_audited_and_marks_final_outputs_stale`
   - `assert 'CLOSED' == 'OPEN · REOPENED'` on the session panel's lifecycle
   label. It passed alone (3 / 3) and touches no code changed by items 1–2.
   Cause: `session_poller.read_session_view` took the snapshot and the
   session record in two separate reads on the poller thread; a poll in
   flight while the reopen committed paired a *closed* snapshot with the
   reopened record, the panel (lifecycle from the snapshot) said *CLOSED*,
   and the test's wait condition (`reopen_count >= 1`) was already true for
   that torn view. Reproduced deterministically by committing the reopen -
   and, separately, a close - exactly between the two reads (both failed
   before the fix). Fix (`07b121f`): the record is read on both sides of the
   snapshot and the pair is retaken (at most three snapshots) when the
   lifecycle (state, reopen count, closed at, stale since) moved in between;
   a quiet session costs one extra single-row read per poll. The existing
   test is unchanged. Regression tests:
   `tests/gui/test_session_view_consistency.py` (3). This is the GUI's read
   adapter - engine, intake, persistence and lifecycle services are
   unchanged - so the phase 7 stress suite was not rerun; the 10k
   responsiveness test runs inside `tests/gui`.

4. **Found while validating: a native abort in `tests/gui`** (pre-existing
   test defect, fixed in the test). The first `tests/gui` run after items
   1–3 died after 45 min with no summary; rerun under PowerShell with
   `--capture=sys --no-qt-log`, `-X faulthandler` and a per-test live-`QThread`
   report, it died at the **same position**: exit `0xC0000409`, no Qt or
   Python message, during
   `test_stress_qualification_gui::test_b_the_dialog_form_produces_the_request_it_shows`
   (the file passes alone: 37 / 37). Cause: the test switches the
   qualification dialog's mode, which re-measures the campaign by starting a
   `PreflightWorker` `QThread` (running the fixture's must-not-run runner,
   which raised unseen inside the thread). The dialog joins that thread only
   in `done()`; the GUI conftest hides and deletes windows directly, so a
   worker still running at teardown is a running `QThread` destroyed - a
   fatal abort. Proved with a scratch copy whose runner blocks 2 s: exit
   `0xC0000409` at teardown every time, no message. Fix (`3e23192`): the test
   records the preflight request instead of starting a thread and asserts no
   worker exists; the dialog is unchanged. The file dates from the phase 10
   harness (2026-09-21; last touched 2026-10-03), not phase 8. **Honest
   caveat**: unmodified `a637619` ran `tests/gui` cleanly on this machine
   (1,985 passed, 1 skipped, 1 deselected, 47 min 17 s), so the race - real,
   and now removed - surfaced only with this correction's changes in place;
   which change shifted the timing was not determined (no new test leaves a
   `QThread` alive - checked after every test of the three new files).

5. **Found while validating: a window-position-dependent test** (pre-existing
   test defect, fixed in the test). With item 4 fixed, `tests/gui` completed
   with no abort: **2,012 passed, 1 skipped, 1 deselected, 1 failed** (42 min
   30 s). The failure:
   `test_developer_tools::TestTheDialogFitsSmallScreens::test_tabbing_never_leaves_the_focused_control_off_screen`
   - `AssertionError: datasetScrollArea`, with the Qt warning
   `QWidget::mapTo(): parent must be in parent hierarchy`; it passed alone
   (3 / 3) and with its file (93 / 93). This is the "small-screen tab focus"
   failure the README recorded as unexplained flakiness on 2026-09-30.
   Cause: the scroll area itself is a Tab stop (`QScrollArea` defaults to
   `StrongFocus`) and `isAncestorOf` counts a widget as its own ancestor, so
   the test checked the container against its own viewport - a mapping
   that walks past the window and adds the window's **screen position**.
   Proved: the check on the scroll area is true with the dialog at (0, 0) or
   (200, 150) and false at (250, 520). Fix (`f19a250`): the container is
   skipped; every control inside the form is still checked, and that check
   passes with the dialog at (250, 520).

Commits: `f66888b` (Answer Key session context), `46b8308` (provenance),
`07b121f` (session view), `3e23192` (preflight-thread test), `f19a250`
(tab-focus test), then this documentation.

Values used: source *Scanner Station - Electrical Machines Laboratory North
Wing*; original file
*2026-10-05_Final_Examination_EEE_415_Section_A_Student_1000001_rescan_02.png*.

**Native layout evidence** (scratch scripts `capture_p8_corrections.py`,
`measure_widths.py`; output in git-ignored `test-output/gui/p8_corrections/`).
This display is 1280×720 logical at 150 %, so Windows/Qt clamp a window to
the screen: a native 1366×768 window does not fit, and neither size fits at a
true 175 %. Qt's offscreen platform has no fonts on this machine (0 families)
and an offscreen screen configuration file aborted Qt at start-up, so neither
was used. What was run, on the native platform with Segoe UI:

| Run | Scaling | Window | Interface zoom |
|---|---|---|---|
| `native150` | 150 % (the machine's own) | 1100×680 | 100, 150, 200 % |
| `native140_1366` | `QT_SCALE_FACTOR=0.93` → DPR 1.395 | 1366×768 and 1100×680 | 100, 150, 200 % |
| `native175_max` | `QT_SCALE_FACTOR=1.1667` → DPR 1.75 | largest that fits: 917×567 at 100 % | 100, 150, 200 % |

States: Answer Key with the session open, closed and reopened; Resolve with
the long provenance. Results: the session line is one line at every size and
zoom with the right wording in each state, and leaves the Answer Key page's
minimum width unchanged (409 / 606 / 809 px at 100 / 150 / 200 %, the same as
`a637619`). The provenance line fits beside the full-width tab bar
everywhere: at 1366×768 / 100 % `Scanner… · Batch 2 ·
2026-10-05_Final_Examination_EEE_415…tion_A_Student_1000001_rescan_02.png ·
arrived 10:42`; at 917×567 / 175 % `Batch 2 · 2026-10…_02.png · arrived
10:42`. No native crash in any capture run (the `tests/gui` abort is item 4). **Not done**: a native 1366×768 or
1100×680 window at a true 175 % (the first machine's captures, §16, remain
the 175 % evidence for the rest of the GUI).

## 20. Known limitations

* **No operator use, no network share, no real scanner, no power loss** -
  every claim here is automated or scripted.
* **Write contention stalls**: an operator action can wait up to ≈ 1 s while
  the engine writes (rollback journal; WAL deliberately not used).
* **Resolve view switch at 10k**: 0.5–3 s (session population recomputation,
  phase 4 design).
* **200 % interface zoom** at 1366×768: *Finish Scan Session...* needs a
  horizontal scroll (or the Session menu).
* **Resolve is wider than the window at 150 / 200 % interface zoom**: its
  minimum width is 1,577 / 2,104 px (measured §19a; 2,193 / 2,897 px before
  the provenance fix), so at 1366 or 1100 px the stages scroll horizontally.
  The remainder is Resolve's own toolbar / decision content, not provenance -
  not changed here.
* **Resolve decision panel** bottom row partly cut at 1366×768 / 175 % for a
  Student ID conflict (§16) - pre-existing, measured identical on `main`; not
  fixed here (outside this phase's scope; the splitter can be dragged).
* **Stability policy** (quiet period) per source is not exposed in the source
  dialog; the service default applies.
* **Quality policy and registration-failure alarm** are uncalibrated defaults
  (labelled so in the GUI).
* The 10k responsiveness and 100k paging populations are **metadata only**
  (no images, no recognition); they measure the GUI and its reads, not
  recognition throughput.
* The finite Scan stage's *Recognised values* table header is truncated at
  1100×680 (pre-existing finite layout, §16).

Fixed by the pre-merge correction (§19a), no longer limitations: the Answer
Key stage now names the scan session; long provenance beside the evidence
tabs is shortened to fit, with the tooltip complete, and no longer squeezes
the tabs or widens the page.

## 21. Phase 9 contract / readiness

Phase 9 (automated qualification) may rely on:

* `ContinuousEngine` running unmodified under `SessionRunner`; the GUI adds no
  state of its own - every count it shows is `take_snapshot`'s, so a headless
  campaign can assert on the snapshot and finish-session outcome directly.
* `services/session_sheets.py` for paged / filtered listing of a session at
  scale (bounded statements; `original_names` / `with_original_names` for
  display names).
* `scripts/dev_feed_scanner_folders.py` (three folders, partial writes,
  outage, damage + cross-scanner rescans) and
  `.claude/skills/qtguitesting/scripts/drive_three_source_run.py` as a seed
  for an end-to-end driver; `capture_session_states.py` for visual regression.
* The shutdown order and thread inventory test as the contract for "no orphan
  thread / process".

**Phase 9 readiness: READY** - nothing in this phase blocks an automated
qualification campaign; it needs no GUI. Two conditions that are the owner's
decisions, not blockers: this branch is **not merged** (phase 9 should start
from `main` after a merge decision), and the known limits in §20 (contention
stalls, view-switch cost at 10k) are worth measuring in the campaign rather
than assuming.

## Completion table

| Item | Status | Evidence |
|---|---|---|
| F1 Finite Scan unchanged | ✅ | `TestFiniteModeIsUnchanged`; every pre-existing Scan / Resolve GUI test unchanged and passing in the full run; `test_batch_progress_gui` caught and verified the progress-bar fix (§18) |
| F2 Session-mode operations | ✅ | `test_session_controls_gui`, `test_session_finish_gui`, `test_session_scan_gui` (sources incl. UNC; start, pause/resume, finish-current, cancel-queued, intake pause, finish with blockers, reopen) |
| F3 Three progress lines / status semantics | ✅ | `test_session_controls_gui` (caught up never *complete*), `TestWordsForValues` (recognition falls, no percentage), unreachable wording; screenshots 03–08 |
| F4 Compact source UI | ✅ | folded by default (`test_session_scan_gui`); screenshots 03 / 07 |
| F5 Live Resolve + Rescan | ✅ | `test_rescan_queue_gui`, `test_intake_decisions_gui`, cross-scanner duplicate reaching Resolve live with evidence; scripted run (20 conflicts, 0 hash names) |
| F6 10k GUI responsiveness + paged list | ✅ | `test_operational_gui_responsiveness` (median 0 / p95 8 / worst 442 ms at the tip), `test_scan_paging_gui` (100k: 81–332 ms per operation, first page 269 ms) |
| F7 High-DPI screenshots | ✅ with limitations | 16 states at 1366×768 (+ 1100×680) on native 175 %; findings in §16 (200 % zoom needs scroll for *Finish*; pre-existing Resolve decision-panel height); correction captures §19a (no true-175 % 1366×768 window possible on the second machine's display) |
| F8 Interrupted-session reconstruction | ✅ | `test_session_recovery_gui` (real kill → counts before Start; persisted pause shown) |
| Global UI zoom compatibility | ✅ with limitation | zoom matrix 80–200 % (§16); 200 % at 1366×768 puts *Finish Scan Session* behind a horizontal scroll |
| Coordinator-busy handling | ✅ | `test_session_controls_gui::...refuses_start_with_its_message` (busy signal → notice; nothing reset); finite run buttons disabled while continuous runs |
| Finish/reopen authoritative policy | ✅ | only `session_close` / `finish_scan_session`; AST rule forbids `close_scan_session` in the GUI; `test_session_finish_gui` |
| Native GUI stability | ✅ | no native abort in two full runs, `tests/gui` alone, the screenshot runs or the scripted production runs; thread inventory clean after switches and close. Pre-merge correction: one `tests/gui` abort found (a pre-existing test left a `QThread` running, §19a item 4), proved and fixed; none in the final gate |
| Pre-merge correction (§19a) | ✅ | Answer Key names the session; provenance fits and no longer squeezes the tabs; torn session view and two pre-existing test defects fixed; gate at `f19a250`: 7,113 passed, 0 failed, ruff / mypy clean (§18a) |
| Network-share qualification | Deferred | phase 10 |
| Real scanner qualification | Deferred | not performed |

## Verdict

**COMPLETE WITH NON-BLOCKING LIMITATIONS**

The operational GUI is implemented, tested and scripted-validated as the
brief scoped it; the limitations in §20 are recorded, none blocks phase 9.
It is **not** operator-, network-share-, scanner- or production-validated.

Pre-merge correction (§19a, §18a): both review findings fixed, three further
defects found while validating and fixed, canonical gate green at `f19a250`.
The correction is on `wip/0.1.1-phase8-premerge-correction`; moving it onto
`feat/0.1.1-phase8-operational-gui` and merging are the owner's decisions.
