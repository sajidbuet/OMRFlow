# The 100,000-sheet qualification campaign

Phase 10's final acceptance test. It runs for many hours, kills processes on
purpose, and needs nobody watching it.

Everything below is performed by one headless command,
`omr_scanner.tools.phase10_qualification`, driving the existing stress CLI
(`omr_scanner.tools.benchmark_stress`) as ordinary child processes. There is
no second stress framework, no injected results, and nothing written into
SQLite by anything other than the real recognition pipeline.

---

## 1. Why a whole harness rather than a long command

The earlier procedure was: start a 100,000-sheet run, watch a counter, kill
the process by hand at 1%, restart it, and repeat five times. That is
roughly a full day of somebody sitting beside a machine reading numbers,
and it is not a test anybody can re-run or audit. Worse, the most important
measurement in the whole exercise — *which sheets were durably committed at
the instant of the kill* — cannot be taken reliably by a human, because the
window is milliseconds wide and the evidence dies with the process.

So the campaign is run by a supervisor process that:

* decides the kill moment from the database's own committed count, not from
  a number on a screen;
* writes the pre-kill evidence **from outside the process it is about to
  kill**, so the evidence survives the kill;
* measures what the restarted run *submitted for recognition*, rather than
  inferring it from the final row counts;
* records every verdict to disk as it happens, so an interruption costs one
  run rather than the campaign.

## 2. What a campaign consists of

| Run | What happens |
|---|---|
| `warmup` | 200 sheets, discarded. Proves the template loads, the pool starts, the paths are writable — in twenty seconds rather than forty minutes. |
| `R0` | 100,000 sheets, uninterrupted. Becomes the **semantic reference**: every later run's per-sheet decision is compared against it. |
| `K01` | 100,000 sheets in a **fresh project**, coordinator force-killed once 1% is durably committed, then restarted. |
| `K25` | The same, at 25%. |
| `K50` | The same, at 50%. |
| `K75` | The same, at 75%. |
| `K99` | The same, at 99%. |

**Each K run gets its own project.** Killing one project at 1%, then 25%,
then 50% would only prove that recovery works *after recovery has already
worked* — the second kill starts from a database the first recovery
produced. Five independent projects, each killed once from the same
deterministic starting conditions, is what actually tests whether recovery
depends on where the failure happened.

The faster progressive shape is still available (`--mode progressive`) for
engineering work. It is never the release qualification, and the report
says so.

## 3. Running it

Check first. This changes nothing and takes a couple of seconds:

```powershell
.venv\Scripts\python.exe -m omr_scanner.tools.phase10_qualification preflight `
    --output-dir D:\OMRflow-qualification `
    --template examples\templates\100_question_4_choice_example.omrt
```

It prints the estimated runtime and peak disk, and verifies the template
loads, the output directory is writable, there is enough free space, and
the deterministic generator really is deterministic (it renders three
sample sheets twice and compares the bytes — if that failed, comparing a
kill run against the reference run would be meaningless).

Then start it and walk away:

```powershell
.venv\Scripts\python.exe -m omr_scanner.tools.phase10_qualification run `
    --output-dir D:\OMRflow-qualification `
    --template examples\templates\100_question_4_choice_example.omrt
```

Watch it from any other window, as often or as rarely as you like:

```powershell
.venv\Scripts\python.exe -m omr_scanner.tools.phase10_qualification status `
    --output-dir D:\OMRflow-qualification
```

`status` is read-only and cannot disturb the campaign.

If the campaign is interrupted — the window closed, the machine rebooted,
the orchestrator itself killed — continue it:

```powershell
.venv\Scripts\python.exe -m omr_scanner.tools.phase10_qualification resume `
    --output-dir D:\OMRflow-qualification
```

Runs already verified are skipped. A run that was *in flight* is discarded
and restarted from clean conditions, because a project that has already
survived one unplanned interruption is not the clean starting point the
campaign's premise requires.

### Before you start

* Leave the machine on reliable power and disable automatic sleep for the
  duration. **OMRFlow does not change any Windows power setting**, and it
  never will — the forced process terminations in this campaign are
  deliberate; a machine losing power is not part of the test.
* Do not run other substantial workloads alongside it. Throughput measured
  on a contended machine is not comparable to anything.
* Expect tens of gigabytes of database growth. The preflight tells you how
  much, with a safety margin.

## 4. What is measured, and how

### The release-blocking assertions

Every run must satisfy all fifteen. There is no severity ladder, and no
assertion is ever demoted to a warning to let the phase finish. The
authoritative list is
`omr_scanner.evaluation.qualification.RELEASE_BLOCKING_ASSERTIONS`, and a
test asserts that the evaluator emits exactly that set - so an assertion
added to the list but never evaluated, or the reverse, is caught.

| Assertion | What it means |
|---|---|
| `logical_sheet_count` | Exactly 100,000 rows, every one in a terminal state. |
| `duplicate_active_recognition_results` | No sheet holds two active results. Backed by the database's own `(batch_id, source_path)` uniqueness constraint, which a duplicate would have to have violated. |
| `kill_reached_requested_checkpoint` | The kill landed within 80% of the requested percentage, with real committed work to protect. **This is the anti-vacuity assertion** — see §5. |
| `previously_completed_jobs_rescheduled_for_recognition` | The restarted run never submitted a sheet the first run had already committed. **Measured**, not inferred. |
| `lost_committed_recognition_results` | Every sheet committed before the kill is still committed afterwards. |
| `changed_previously_committed_results` | No pre-kill result was re-decided by the resume. |
| `orphan_workers_after_forced_kill` | No worker process outlived the coordinator it belonged to. |
| `quick_check` / `integrity_check` / `foreign_key_check` | SQLite's own structural checks, on the real multi-gigabyte file. |
| `application_invariants` | `project_health.full_check` reports no error-level or critical issue. |
| `semantic_reference_match` | Every sheet's recorded decision is identical to the uninterrupted reference run's. |
| `unexpected_pending_after_completion` / `unexpected_running_after_completion` | Nothing left `pending`, `queued` or `processing`. |
| `exit_code` | The final run exits 0. |

### Measuring rather than inferring the no-reprocessing property

A resumed run that wrongly re-read an already-committed sheet would still
produce a correct-looking final database: the same 100,000 rows, the same
results, just some of the work done twice. Final row counts cannot
distinguish that from a correct resume.

So `benchmark_stress` gained a `--submission-log`: a file to which it
appends the `batch_index` of every sheet it hands to recognition, flushed
per chunk precisely because the process writing it is about to be killed.
The assertion is then the intersection of

* the committed set captured by the supervisor immediately before the kill,
  and
* the set the restarted run actually submitted.

That intersection must be empty, and both sets must be non-empty.

### The semantic digest

`result_json` contains `timings` and `elapsed_seconds`, which legitimately
differ between two runs of the same sheet. Comparing them would make every
comparison fail for a reason that has nothing to do with correctness. The
digest therefore covers only the row's status plus `outcome`,
`registration`, `warnings`, `fields` and `answers` — every part that is a
*decision* the pipeline made — hashed over a canonically-ordered JSON
serialisation so key ordering cannot create a spurious difference.

Everything is keyed by **`batch_index`**, never `scan_id`. `scan_id` is an
autoincrement surrogate that means nothing across two independently created
projects; `batch_index` *is* the sheet's identity in the deterministic
dataset, so sheet 41,207 is the same sheet in R0 and in K75.

### The orphan check

Only the *coordinator* is killed, deliberately. Killing the whole tree
ourselves would prove nothing — the property under test is that
`services/process_containment.py`'s Windows Job Object takes the worker
pool down with the coordinator without anyone asking it to, which is what
stops a crashed run leaving eight recognition processes each chewing a CPU.
The supervisor captures the coordinator's live descendants *before* the
kill (afterwards there is nothing left to ask), then checks whether any
survived. Any orphan is recorded as a release-blocking failure **and then
cleaned up**, because leaving it running would corrupt every later run's
measurements too.

### Telemetry

One `telemetry.csv` for the whole campaign, sampled every five seconds from
the supervisor and flushed after every row. Deliberately external, for two
reasons: an in-process sampler would lose its own history at the moment of
the kill, and it could only ever see the coordinator — where almost none of
the CPU time is spent. Sampling from outside captures the whole process
tree.

Columns: timestamp, run, attempt, elapsed, committed/pending/failed,
sheets-per-second, coordinator CPU and RSS, whole-tree CPU and RSS, process
count, database size and free disk.

## 5. The vacuous-pass trap, and how it is closed

During this harness's own validation, a real defect made a kill-run's
recovery assertions pass against an **empty set**. The telemetry sampler's
`latest` counts were not cleared between runs, so K50's kill condition was
satisfied by the *previous* run's final count the instant K50 started. K50
was killed before it had committed a single sheet, the pre-kill committed
set was empty, and "0 already-committed sheets were re-submitted" was
therefore trivially true. Every assertion reported PASS.

A vacuous pass is worse than a failure, because it looks like evidence.
Three things now prevent it:

1. `TelemetryWriter.start()` clears `latest`, so a checkpoint can only be
   reached by this run's own measurements.
2. `kill_reached_requested_checkpoint` fails unless the kill landed within
   80% of the requested percentage with a non-empty committed set.
3. `previously_completed_jobs_rescheduled_for_recognition` requires both
   the committed set *and* the restarted run's submission set to be
   non-empty, not merely disjoint.

The lesson generalises: every assertion of the form "no X happened" needs a
companion assertion that there was an opportunity for X to happen.

## 6. Output

Everything lives under one directory, and nothing outside it is ever
created or removed:

```text
D:\OMRflow-qualification\
    qualification_config.json     what was asked for
    qualification_state.json      progress, rewritten atomically per stage
    environment.json              machine, versions, CPU, memory
    preflight.md / .json          the go/no-go, with estimates
    telemetry.csv                 every sample from every run
    events.jsonl                  a flushed append-only campaign log
    qualification_summary.json    the machine-readable result
    qualification_summary.md      the report a release decision cites
    logs\R0.log, K01.log, ...     each child run's own stdout/stderr
    evidence\R0\
        submitted_attempt1.txt    every batch_index this run submitted
        pre_kill_committed.json   the committed set and digest at the kill
        kill.json                 pids, orphans, lock state, timing
        semantic_digest.json      per-sheet decision digest
        outcome.json              every assertion and its verdict
        benchmark_attempt1.json   benchmark_stress's own report
    projects\R0\                  the real project, with its database
```

### Retention

A K run's multi-gigabyte project directory is removed once it has **passed
and its evidence has been extracted**; `--retain-passed-projects` keeps it.
R0's project is always kept, because it is the semantic reference for
everything after it.

**A failed run's project is never removed**, whatever the retention setting
says. It is the only remaining copy of the evidence for why it failed, and
nothing is cleaned up for you.

## 7. Reading the verdict

The report's headline is one of three things, and the distinction matters:

* **QUALIFIED** — 100,000 sheets, `full` mode, all five mandated
  checkpoints, every assertion passed. Only this qualifies the release.
* **ALL RUNS PASSED — NOT THE RELEASE QUALIFICATION** — everything passed,
  but the campaign was smaller or reference-only. The report names exactly
  why, so a convenient smaller run cannot be cited later as the
  qualification.
* **FAILED** — at least one assertion failed. The campaign stops at the
  first failing run rather than spending another six hours producing more
  evidence for a conclusion already reached, and the CLI exits non-zero.

### What the campaign does not establish

Stated in every report, because it is not obvious:

* **Nothing about power loss, disk failure or filesystem corruption.** The
  terminations are process kills; the storage layer is never interrupted
  mid-write by the OS.
* **Nothing about throughput on other hardware.** Every figure is the
  machine's own.
* **Nothing about recognition accuracy.** The dataset is synthetic and
  deterministic; this is a test of software correctness, recoverability and
  reproducibility.
* **Not end-to-end result-workbook generation at this scale.** The stress
  dataset has no attendance roster, answer key or result template, and
  inventing them would measure a fabricated scenario. The stored queries
  that Review, Results and Reports actually depend on are timed on the real
  100,000-row batch instead, and are reported as exactly that.

## 8. Validating the harness itself

The campaign machinery is exercised at small scale in a few minutes,
including a real kill:

```powershell
.venv\Scripts\python.exe -m omr_scanner.tools.phase10_qualification run `
    --output-dir C:\temp\qual-smoke `
    --template examples\templates\100_question_4_choice_example.omrt `
    --sheets 2000 --checkpoints 50 --warmup-sheets 100 --workers 6
```

and unit-tested at no scale at all (`tests/unit/test_qualification.py`),
where the assertion logic, the digest, the state machine and the
anti-vacuity guards are checked without killing anything.

## 9. The GUI is a launcher, not a second implementation

**Tools → Developer / Testing → Run 100,000-Sheet Stress Test...** starts
the same CLI as a detached child process and then reads the files above. It
contains no stress-test logic, no assertion logic and no reporting logic.

### The launch dialog

Asks for an output folder, a template, and one of two campaign shapes:

* **Full qualification (recommended)** — the reference run plus the five
  independent forced-kill runs. Labelled, on screen, as the only mode that
  qualifies the release.
* **Single 100,000-sheet run** — `--mode reference`. Labelled, on screen, as
  **not** the release qualification.

`--mode progressive` is deliberately not offered: a menu item is exactly
where somebody would pick it by accident.

It then runs `phase10_qualification preflight` and displays that tool's own
`preflight.json` — every check with PASS/FAIL/WARN, the estimated runtime,
the estimated peak disk and the free space. Changing the mode or the folder
re-runs the preflight, because the estimates for `full` are roughly six
times those for `reference` and showing one while the other is selected
would be worse than showing nothing. **Start stays disabled** while any
blocking check fails or the disk is short, and the reason is named,
including the shortfall.

**Cancel is the default button.** Enter gets pressed by accident; a
multi-hour campaign that force-kills processes is not something Enter should
be able to start.

### The monitor

Read-only. It polls `qualification_state.json` and the tail of
`telemetry.csv` and shows the overall status, whether the orchestrator is
alive, a per-stage table, the latest throughput and resource figures, and
the campaign's notes. On completion it states the verdict — qualified,
passed-but-not-the-qualification, failed (naming the failing assertions), or
stopped.

Its controls:

* **Stop Safely** — writes a `stop_requested` file. The orchestrator checks
  for it *between* runs, records that it was asked, removes it, and exits
  with a resumable campaign. **This is never recorded as a failure**, and a
  run in progress is never interrupted, so a 100,000-sheet run may take
  hours to reach the stopping point. This is the only thing the monitor
  writes.
* **Force Kill Current Processing Run** — kills the orchestrator and its
  children now. Confirmed loudly, because it is *not* one of the campaign's
  own deliberate kills: those capture the committed work first and are
  measured; this one does not, so the run in progress will almost certainly
  be recorded as failed with incomplete evidence.
* **Resume Campaign**, **Open Report**, **Open Results Folder**, **Copy
  Summary**, and **Close** — which stops the polling timer and nothing else.

### Consequences that follow, and are intended

* **Closing the GUI does not stop the campaign.** That is the point: it runs
  in its own detached process group with its output redirected into the
  campaign directory.
* Reopening the menu action while a campaign is running reconnects to its
  monitor rather than offering to start a second one in the same directory,
  which would have two orchestrators overwriting each other's evidence. If
  the campaign there stopped before finishing, it offers to resume instead.
* The GUI can never reach a different verdict from the command line, because
  it does not compute one. It also never imports the evaluation layer — that
  module reaches into SQLAlchemy and the database package, which the
  architecture test forbids the GUI to depend on, so the handful of file
  names it needs are restated beside a pointer at the source of truth and
  pinned by a test that compares the two.
