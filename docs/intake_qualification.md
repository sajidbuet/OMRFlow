# The automated intake qualification campaign (revised phase 9)

`0.1.1-alpha.0`, roadmap phase 0.1.1-G, first half. A headless, supervised
campaign that answers one question with machine-verifiable evidence:

> Can the integrated ScanSession + finite ScanBatch + multi-source intake +
> continuous engine + Resolve/rescan + session-level scoring and reporting
> architecture process a large, adversarial, continuously arriving
> **synthetic** examination correctly, survive forced termination, and
> produce results exactly equal to independently known ground truth?

It is **additional to** the Phase 10 finite 100,000-sheet harness
(`docs/phase10_qualification.md`, `omr_scanner.tools.phase10_qualification`),
which is unchanged and keeps qualifying deterministic finite processing.

## 1. What it proves - and what it does not

It proves automated **source-build** behaviour under a large synthetic
multi-source intake campaign with controlled failures and exact ground truth:
separate scanner writer processes on local folders, real forced termination
of the OMRFlow coordinator, restarts into the same project and session, a
scripted operator using the production services, closure through the
authoritative finish policy, and Results / report cell values compared with
an independent reference.

It does **not** prove:

* genuine SMB / network-share behaviour - the "source outage" removes and
  restores a local folder link (a Windows junction); revised phase 10 owns the
  real share (`ACCEPTANCE_CRITERIA.md` §6);
* real scanner behaviour - the images are synthetic, written by processes;
* power-loss durability - `TerminateProcess` is not power removal;
* real-paper calibration of the quality policy - it is the **unvalidated
  default**, and the campaign tests the decision *workflow*, not thresholds;
* installed-build behaviour - it runs from source;
* recognition accuracy on real scans, or production readiness.

## 2. Running it

From the repository root, with the project's virtual environment:

```powershell
# The harness self-test: minutes; every mechanism once; never QUALIFIED.
.venv\Scripts\python.exe -m omr_scanner.tools.intake_qualification --self-test `
    --output Scratch\Qualification\phase9

# The release-scale campaign (>= 3 sources, >= 10,000 arrivals): hours.
.venv\Scripts\python.exe -m omr_scanner.tools.intake_qualification --release-scale `
    --output Scratch\Qualification\phase9
```

Exactly one of `--self-test`, `--release-scale` or `--config FILE.json` is
required - a release qualification is never started by default settings, and
a custom configuration is never the release mode. The mode and the scale are
printed before anything runs. Other options: `--seed`, `--timing-seed`,
`--workers`, `--template`, `--no-control`, `--no-finite` (the last two make
the campaign unable to pass, by design).

Every campaign gets a new folder under `--output`
(`p9-<mode>-<UTC stamp>-<seed>`); nothing from an earlier campaign is reused,
and every evidence line carries the campaign id - a reader refuses a line of
another campaign. Run it from a **clean, committed** tree: the report records
`git rev-parse HEAD` and whether the tree was clean.

Exit codes: `0` the requested campaign passed (`QUALIFIED` for
`--release-scale`; `ALL RUNS PASSED — NOT THE RELEASE QUALIFICATION` for a
self-test or custom campaign); `1` `FAILED`; `2` bad arguments; `3`
`--release-scale` passed below the measured release scale (never success);
`130` interrupted. Ctrl+C stops every child process the harness owns, keeps
the evidence and writes a `FAILED` report.

Output (all under `Scratch/`, which is never committed): `report.json`,
`report.md`, `manifest.json` (the ground truth), `supervisor.jsonl`, the
rendered image pool, and per run (`interrupted/`, `control/`, `finite/`) the
project, the scanner folders, the attendance workbooks, the exports and the
evidence logs.

## 3. Architecture

```text
 supervisor (the CLI process - never killed)
   |  plans the cohort (cohort.py) and renders the images (render.py) into a pool
   |  builds the project through production services (project.py)
   |  writes its own append-only log; reads the database read-only, from outside
   |
   +-- writer A  ---\                          (separate processes; copy pool bytes
   +-- writer B  ----+--> disk/Scanner_X         into their folder by write pattern;
   +-- writer C  ---/          ^                 log write_started / write_completed
   |                           | junction        with size and SHA-256, fsynced)
   |                     share/Scanner_X  <-- the watched source root
   |
   +-- coordinator (the application under test - killed with TerminateProcess)
         ContinuousEngine + ProcessRecogniser (real worker processes, Job Object)
         IntakeService on the real disk
         scripted operator thread (operator.py) - production services only
         logs: claimed (before submission), committed, decisions, heartbeat
         commands from the supervisor: hold / release, arm / disarm, close,
         finish, reopen, release_held, downstream, reprocess, stop
```

* **Writers** know nothing about OMRFlow; the only thing OMRFlow can see is
  the folder. Write patterns: atomic, stepped growth, header first, held open
  while growing, one pause longer than the stability quiet period, and written
  under a temporary `.part` name then renamed. Arrival times come from a
  seeded per-source intensity profile with bursts, normal stretches and idle
  gaps, ending in a burst. A writer writes **one file at a time**, as a
  scanner does, so a planned time is the earliest a file starts: the pauses
  of the partial-write patterns add up (about 11,400 s for the busiest
  writer of the release plan against a 2,400 s schedule), and the real
  arrival period is longer than the configured duration. The report quotes
  the measured `arrival_seconds` (first to last completed write - which
  includes the late script written in the endgame), not the configured
  duration.
* **Source outage**: the watched root is a directory junction to the writer's
  folder. Removing the junction makes the source unreachable to OMRFlow while
  the "scanner" keeps writing to its own disk; restoring it brings the files
  written meanwhile back into view. A local simulation, never called SMB.
* **Kill points are chosen from state, never from wall time**: a forced kill
  when the committed fraction reaches a planned percentage *and* the database
  shows at least two sheets `processing` (a kill whose in-flight work
  committed in the milliseconds before termination is kept as a restart and
  repeated, at most twice; only a kill that landed on work in flight counts
  for its point); from 95 % on - where the last sheets finish within one
  poll - the coordinator itself holds the state: a pause point blocks right
  after a commit that brings the session to the target while other sheets are
  still in a worker, and the supervisor kills it there; pause points the coordinator blocks
  at on request - right after a work-unit commit, after a commit whose
  duplicate-ID pass is still owed (the sheets' identities collide with a
  committed sheet), and after the operator's n-th decision - which it reports
  before blocking; and a clean application close with work in flight. After
  every kill the supervisor rolls back a hot journal exactly as the next open
  would, reads the durable state, runs `quick_check`, `integrity_check`,
  `foreign_key_check` and the Project Health full check, waits until the
  writers have completed files while the coordinator is down, and restarts it
  into the same project (`force_lock`) and session. The restarted
  coordinator runs its restart sequence and reports the Resolve queue it can
  show **before Scan is visited**; the supervisor compares the recovered state
  with the state at the kill before allowing any new work.
* **Checkpoints** (`hold`): the coordinator stops at a step boundary and the
  operator between decisions; the supervisor reads every durable fact in one
  read transaction, takes the production session snapshot and population,
  and evaluates them; then `release`.

## 4. One campaign

| Run | What it is |
|---|---|
| `interrupted` | The qualification run: kills, clean close, outage, *Reprocess All*, checkpoints, endgame |
| `control` | The same logical cohort and operator decisions, arrival times re-drawn on a shorter schedule (the writers' own pauses still bound it), **no** interruption |
| `finite` | The same cohort as one finite batch through the finite Scan stage's own service sequence (coordinator lease, one batch, manual ledger and hashes, exact-duplicate rule, `BatchRecorder`, `process_batch` with workers, batch-scope review pass, finalise), then the same operator and endgame, the late script as a second batch after a reopen |
| `golden` | The committed golden one-batch regression (`tests/golden_one_batch.py` against `tests/fixtures/golden_one_batch/golden.json`) on this build |

The **endgame** of each continuous run: once every written file is
registered (or a byte copy) and nothing remains to read, a consistent
checkpoint; *Finish scan session* is attempted while planned decisions remain
and must be **refused** listing every blocker; the operator answers the rest;
*Finish* closes through `finish_scan_session` (the one closure policy);
attendance, scoring and every set's final report; a late file (a missing
script found after closing) arrives and must be **held**, not counted; the
session is reopened and every final export must read **stale**; the held
file is released, read, and the session closed again; reports regenerated.

## 5. The cohort and its ground truth

`cohort.py` plans everything from two seeds - `seed` (who sits which set, what
they marked, which papers are folded, scanned twice, wrongly bubbled, which
scanner each image arrives at under which name, what the operator will
decide) and `timing_seed` (when, and how each file is written). Nothing in the
plan consults OMRFlow. The manifest (`manifest.json`) records all of it.

Four sets (A-D), a verified key each, Set B with one withdrawn question
(full credit), a fixed negative-marking policy (+1, -1/4 for a wrong answer,
-1/4 for a double mark, 0 for a blank, clamped at 0). Planted cases, per set
and in proportion to its size (at least one each):

| Case | Images | What the operator does (production service) |
|---|---|---|
| Normal candidate | one clean script (some blanks, some double marks) | - |
| Absent | none; `ABSENT` on the list | - |
| Missing script | none; present on the list | corrects the attendance list |
| Script for an absentee | a script; `ABSENT` on the list | corrects the attendance list |
| Late script | arrives after closing (one per campaign) | releases the held file after a reopen; corrects the list |
| Same paper scanned twice | two scans, other bytes | accepts both duplicate-ID conflicts; sets the second scan aside (`set_script_excluded`) |
| Wrong Student ID bubbled | bubbles another present candidate's roll | corrects the duplicate-ID conflict to the written ID |
| Blank / double-bubbled ID | the ID field blank, or one column with two marks | corrects the ID conflict |
| Folded paper | a corner fold covering a registration marker | confirms the suggested rescan (declared ID); confirms the rescan - often from **another** scanner |
| Replacement chain | folded original -> rescan 1 -> rescan 2 | confirms rescan 1, rejects it as smudged, confirms rescan 2 |
| Unknown candidate | a script whose roll is on no list | dismisses the reconciliation entry |
| Stray blank page | a white page (a suggested rescan) | dismisses the suggestion; acknowledges the sheet conflict |

Plus byte copies of about 2 % of the files, within one scanner and across
scanners (some under the original's file name), and every scanner naming its
files with its own counter, so the same names arrive at every source.

Operator timing: each review decision is `live` (a seeded delay after it is
visible, while files keep arriving), `late` (once 60 % of the run is
committed) or `final` (only in the endgame, after the refused closure).

Expected readings are **checked, not assumed**: a clean script must read
exactly; a blank or double-bubbled ID column gives `identifier_blank` /
`identifier_multiple`; a marker-covering fold gives `registration_failed` and
the default quality policy's `RESCAN_REQUIRED`. Before writing a sheet the
renderer re-derives its bubbled roll, set and answers and refuses a sheet
that disagrees with the plan (a harness defect, never a recognition result).

The independent reference (`reference.py`) - a deliberately plain scorer,
the attendance lists after the office's corrections, ranks as `RANK.EQ`
computes them, expected reconciliation statuses before and after the
decisions, the expected open conflicts for any committed population and
committed decisions, and every compared report cell - never calls an
OMRFlow scoring, reconciliation, population or report function.

**Report cells compared**: on each set's Rollwise sheet and on `meritwise`,
the header labels and every data row's Sl.No., Roll No., Name, Total (exact
to 1e-9, or `ABSENT`) and Merit (`---`, or the value its `RANK.EQ` formula
evaluates to), the row after the last (empty); on the Answer Key sheet every
question's answer and withdrawn flag. The Summary and Processing Log sheets
carry generation timestamps and are not compared, as in the golden
regression.

## 6. The release-blocking assertions

Defined in `assertions.REQUIRED_ASSERTIONS` and always all emitted (a test
fails if one disappears). Each records what it checked and a minimum: an
unexercised case is `not_exercised`, which fails like a failed check.

| Assertion | Measured as |
|---|---|
| `stable_files_discovered_exactly_once` | every written file has exactly one ledger row with its final hash, registered or a byte copy; no unplanned row; no sheet from two rows; no content effective twice; no ledger row ever seen `vanished` (the supervisor samples them with every progress read - the plan never removes a file, so "vanished, then reappeared" is a file discovered twice even when the final ledger is right) |
| `no_incomplete_file_processed` | per registered file: first `claimed` (logged before submission) and `registered_at` after the writer's `write_completed`, and the sheet's bytes are the completed file's |
| `source_provenance_retained` | source, observed path, relative path and name equal the arrival; sheet and unit point back; the name the Scan list shows (`session_sheets.original_names`) is an arrival name; *Reprocess All* sheets traced to the watched arrival behind them |
| `duplicate_content_identified` | each planted copy group: one registered, the rest `duplicate_content` linked to it; nothing else flagged; copies within **and** across sources |
| `independent_filenames_do_not_collide` | every name written to several sources: one ledger row each; distinct bytes all registered |
| `batches_finite` | every batch in the session, sealed at the end, members = total; membership digests unchanged since first seen sealed, across checkpoints and restarts |
| `no_accepted_image_lost` | final effective contents = the reference's, exactly; every committed sheet still committed after each restart |
| `no_completed_scan_rerecognised` | for every kill: no sheet committed before it is claimed by a later incarnation; no sheet claimed twice by one incarnation |
| `offline_arrivals_discovered` | files completed while the coordinator was down, or while the source was unreachable, all discovered; the outage recorded as unreachable |
| `conflict_counts_correct_as_population_grows` | at every checkpoint: the set of open (sheet, conflict type) equals the reference's for the committed population and committed decisions |
| `rescan_relationships_survive_restart` | every planned link: original superseded by exactly its replacement, excluded; history rejected -> replaced; chain and cross-source links; unchanged by every restart |
| `aggregate_counts_consistent` | at every checkpoint and at the end: partition total = discovered; every bucket = an independent row-by-row recount (§14.1); effective = accepted + conflict + suggested |
| `session_results_match_ground_truth` | statuses before / after decisions, stored results (status, mark, counts), every compared report cell - for the first close and after the reopen; effective Student IDs; effective population |
| `sqlite_integrity` | `quick_check`, `integrity_check`, `foreign_key_check` clean after every kill and at the end of every run |
| `application_invariants` | `project_health.full_check`: no error or critical issue (warnings recorded) |
| `finite_mode_regression` | the golden regression matches its fixture; the finite control's statuses, results and cells equal the reference; its stored readings equal the session's, content by content |

Beside them, the **crash matrix** (`ACCEPTANCE_CRITERIA.md` §5.4, cases
1-15) and the **endurance cases** (§5.3, A-E) are evaluated from the same
evidence, with stable ids (`case_01_...`, `endurance_a_...`).

**Verdict** (generated from the machine-readable results, never typed):
`FAILED` if any assertion, crash case or endurance case is not `pass` (the
reprocess case may be left to the targeted stress test only when no plain
unit occurred); otherwise `QUALIFIED` only when the *measured* campaign meets
the release scale (>= 3 sources, >= 10,000 arrivals written, >= 2 sets, real
images, every required workload feature exercised, every assertion
evaluated, release mode); otherwise `ALL RUNS PASSED — NOT THE RELEASE
QUALIFICATION`.

## 7. Limitations of the method

* The 1 / 25 / 50 / 75 / 99 % resume series is **progressive within one
  large run** (each kill lands on a database earlier recoveries produced),
  not five independent projects as the Phase 10 harness does; every kill is
  evaluated on its own. The pre-Beta 100,000-sheet gate may choose the
  independent shape.
* The operator is scripted and decides from the paper's truth; no person
  operated the application, and the operational GUI is not driven (phase 8
  covered it mechanically).
* *Reprocess All* is exercised on one finished unit whose sheets carry no
  planned decision - when such a unit occurs; otherwise the targeted stress
  test covers it.
* Recognition, quality and stability thresholds are not tuned: the quality
  policy is the unvalidated default, the stability policy the production
  local-folder default at release scale (faster values in the self-test,
  recorded in the report).
* Windows only for the release run (junctions, `TerminateProcess`, Job
  Objects).
* A junction removed by the supervisor fails reads as *file not found*; a
  real share drops with other errors and timings (revised phase 10).

## 8. Tests of the harness itself

* `tests/unit/test_intake_qualification.py` - the registry equals
  `ACCEPTANCE_CRITERIA.md` §5.2; every assertion is emitted even when the
  evaluator breaks; an unexercised case, a planted failure, a missing
  assertion, a failed crash case or an incomplete campaign never passes; a
  small campaign never qualifies; plan determinism; the reference scorer;
  recovery comparison; integrity and health failures; torn and stale
  evidence lines; reports; CLI exit codes and a malformed configuration.
* `tests/integration/test_intake_qualification_failure_paths.py` - a scanner
  writer that crashes on a malformed schedule, a coordinator that never
  starts, a hung stage (timeout with diagnostics; every child stopped), a
  planned 99 % kill that never landed on work in flight, an operator
  interrupt, a harness defect and a broken evaluator: each ends `FAILED`
  with a full report.
* `tests/integration/test_intake_qualification_campaign.py` - a small real
  campaign with one real kill (must pass what it exercises and still be
  `FAILED`); `stress`: the self-test and a several-minute endurance campaign
  with the 1 / 25 / 50 / 75 / 99 % series.
