# Phase 7 handoff — Candidate & Attendance Reconciliation

**Implemented:** 2026-09-19
**Version:** 0.1.0.dev0
**Environment verified on:** Windows 11, Python 3.12.7, PySide6 6.11.2, OpenCV
5.0.0, NumPy 2.5.3, SQLAlchemy 2.x, openpyxl 3.1.5, 16 logical CPUs

Read alongside `docs/reconciliation.md` (the operator-facing description),
`docs/DATA_MODEL.md` (the six new tables and the migration) and
`docs/ARCHITECTURE.md`.

**Phase 7 accounts for scripts and candidates. It does not score anything, and
it does not verify that a reconciliation decision was right** — only that a
named person made it, for a stated reason, and that it can be traced and
reversed.

---

## 1. What already existed

| Capability | Where | Verdict |
|---|---|---|
| Durable per-scan rows with the recognised identifier | `batch_store` / `batch_scan` (Phase 5) | Read, never written |
| Multiprocessing pool, cancellation, resume, progress | `parallel_batch`, `batch_processor` (Phase 5) | **Untouched** |
| The effective value of a reviewed field | `review_store` (Phase 6) | Extended by one function (§4) |
| Append-only audit ledger with database triggers | `audit_event` (Phase 6) | **Extended, not duplicated** (§5) |
| Named-reviewer requirement and reason codes | `review_store.validate_reviewer` (Phase 6) | Pattern followed exactly |
| Forward-only migration framework | `database/migrations.py` | Followed exactly |
| `WorkflowPage` + `on_project_changed` | `gui/pages/base_page.py` | Reused |
| Background-worker pattern (`QThread` + one signal) | `gui/scan`, `gui/review` | Reused |
| `importlib.resources` packaging for bundled assets | `gui/icons.py` | Reused (§6) |
| Privacy rule for logs | `docs/ARCHITECTURE.md` | Made executable (§8) |
| `openpyxl` as a declared dependency | `pyproject.toml` | Used; **pandas not used** |

**Nothing was rewritten.** No second audit mechanism, no second batch pipeline,
no second definition of what a sheet's candidate ID is.

## 2. What was missing

1. No way to say who was *supposed* to sit the paper. Everything before this
   phase reasons about sheets; nothing reasoned about people.
2. No way to notice that a script belongs to nobody, or that a candidate
   handed nothing in. Phase 6 can tell you a roll number was hard to read; it
   cannot tell you it is not on the list.
3. No import path for a spreadsheet at all.
4. No audit vocabulary for anything other than a recognition conflict.

## 3. What was built

### `domain/reconciliation.py` — the vocabulary

Pure. `AttendanceState`, `ReconciliationStatus` (7), `ReconciliationIssue` (5),
`ResolutionState`, `ScriptAssignment`, `ReconciliationAction` (9),
`ReconciliationReason` (9), plus the value objects (`CandidateRecord`,
`ScriptRecord`, `ScriptDecision`, `CandidateDecision`, `ScriptView`,
`ReconciliationEntry`, `ReconciliationCounts`) and the absence-token rules.

`primary_status()` and `_STATUS_PRECEDENCE` live here because the precedence is
a *policy*, and burying it in a service would make it a thing to rediscover.

### `services/candidate_import.py` — reading a roster

CSV via `csv` + `utf-8-sig`; XLSX via `openpyxl` in read-only mode. **pandas is
not used**, although it is a declared dependency: this is one pass over a
spreadsheet producing plain values, and pulling a dataframe library into the
path would add memory and a second set of type coercions to fight — the
`15000001.0` problem is exactly that kind of coercion.

`normalise_candidate_id` is the single place identifier normalisation happens.
`suggest_mapping` returns *ambiguity* as a first-class outcome rather than a
best guess.

### `services/reconciliation.py` — the rules

One pure function over value objects. Same inputs, same output, including
order. No clock, no config, no database.

### `services/reconciliation_store.py` — persistence and decisions

Roster import (one transaction, refuses an invalid roster), the reconciliation
run, the queue reads, and the six operator actions — each recording a decision
and appending an audit event in one transaction, then re-reconciling.

### Migration 4

Six tables, plus `entity_type`/`entity_id` on `audit_event`.

### `gui/attendance/`

`page.py` (roster bar, summary, table, detail, decisions), `import_dialog.py`
(file, worksheet, preview, mapping, validation) and `worker.py` (two
`QThread`s).

### Elsewhere

- `review_store.effective_identifiers()` + `EffectiveIdentifier` — §4.
- `main_window`: page wiring, operator-name broadcast, `reconcile_batch()`,
  and a `shutdown()` in `closeEvent`.
- `catalog.py`: the Attendance stage is no longer a placeholder.
- The packaged sample moved into the package — §6.
- **A pre-existing privacy defect fixed** in `batch_processor` — §8.

## 4. Why `effective_identifiers` exists

Phase 7 must reconcile against the candidate ID a sheet is *now* believed to
carry, which is the machine's reading unless Phase 6 changed it. Three ways to
get that, two of them wrong:

1. Read `BatchScan.identifier_value`. **Wrong** — that is the machine's own
   reading, and using it would silently ignore every correction a reviewer
   made. It is the Phase 6 defect in a new costume: an interface showing a
   value different from the agreed one.
2. Reimplement the fold over audit events in `reconciliation_store`. **Wrong**
   — a second implementation of "what is this field's value now" is a second
   thing to drift, and Phase 6 exists precisely to have one.
3. Ask `review_store`, which already owns that question.

Three it is. `effective_identifiers(database, batch_id)` returns one record per
scan carrying the machine value, the effective value, who changed it, and
whether the identifier is **still unknown**.

That last flag is why `UNRESOLVED_CANDIDATE_ID` exists as a state. It
deliberately **excludes** `IDENTIFIER_DUPLICATE`: a duplicate is a perfectly
legible ID that two sheets share, which is a reconciliation problem, and
treating it as "not yet resolved" would hide the duplication Phase 7 is for.

## 5. Why the audit ledger was extended rather than duplicated

The brief says to reuse Phase 6's `AuditEvent`. The table had no
`entity_type`/`entity_id`, so a decision about a *candidate* could not be
addressed. Two options:

- A `reconciliation_audit` table. Rejected: it would need its own triggers, its
  own append-only argument and its own review — and "the audit trail" would
  become two things that could disagree.
- Add the two columns. Taken.

**The interesting part is the backfill that did not happen.** Populating the
new columns for existing rows means `UPDATE audit_event`, which the Phase 6
triggers abort — correctly, and that is their whole purpose. So the default was
chosen to be *already true* for every pre-existing row (`entity_type='conflict'`),
and `ADD COLUMN` (a schema change, which does not fire a DML trigger) was
enough. Nothing had to be rewritten, so nothing was.

`audit_event` having **no foreign key** is what made this cheap. That Phase 6
decision paid for itself one phase later.

## 6. Why the sample template moved

`pyproject.toml` says outright that the top-level `resources/` directory "is
dev/test fixtures only and is never packaged". A sample the *application*
offers to save therefore cannot live there — it would work in a checkout and
fail in a wheel.

It now lives at `src/omr_scanner/resources/templates/`, is declared in
`package-data`, and is read through `importlib.resources` — the arrangement
`gui/icons.py` established for the toolbar icons. The supplied workbook was
moved there byte-for-byte (SHA-256 verified before and after).

## 7. Why classification is a cache and decisions are not

`reconcile()` is pure, so its output is reproducible from its inputs. The entry
and script rows are therefore **disposable**, and `reconcile_batch` deletes and
rewrites them wholesale.

That is not an optimisation, it is the correctness argument. Merging new
classifications into old rows is how a candidate who *was* an exception and no
longer is keeps a row saying otherwise — and the brief rules out exactly that:
"Do not retain stale exception classifications as though they were still
current."

Operator decisions are the **input** to that function. They live in their own
table, survive every rewrite, and are what makes re-running safe rather than
destructive.

They are stored rather than folded from the ledger — unlike Phase 6's effective
value — because they are genuinely *state*, not a derivation: reconciliation
consumes them. A test asserts the ledger records every one of them, so the
history is still authoritative about what happened.

## 8. Privacy

Made executable rather than merely documented:

- `tests/unit/test_candidate_privacy.py` (18 tests) drives import,
  reconciliation, unknown-ID handling, duplicate handling, refusals and errors
  with `SECRET-ID-123` / `PRIVATE CANDIDATE`, and asserts none of it reaches
  captured logging.
- One of those tests is a **grep**: it fails if any Phase 7 module formats
  `candidate_id`, `display_name` or `imported_value` into a `_LOGGER` call. The
  leak this phase most has to prevent is one line of well-meaning debugging,
  and it looks the same every time.
- A `qtguitesting` smoke check captures the root logger through a whole
  import-reconcile-assign cycle.
- Even an unexpected exception is logged by **type only** in the import path: a
  third-party library's message can quote the cell it choked on.

**A pre-existing defect was found and fixed.** `batch_processor` logged the
*destination* file name when copying a renamed scan — and with renaming on,
that name is the candidate's roll number. Two log lines now omit it.

**A pre-existing exposure was found and left, documented.** The recognition
pipeline logs the *source* file name of each scan, which is the only way to
tell which sheet failed. An office whose scans are named by roll number
therefore has roll numbers in its log, put there by Phase 3. Removing it costs
the diagnostic for headless runs; the trade-off has not been made, and
`docs/reconciliation.md` §9 plus a pinning test record the decision rather than
letting the next person discover it.

## 9. A crash worth knowing about

The GUI tests aborted with exit code 9 and no traceback. The cause was a
`QThread` superseded but never joined: the import dialog starts a reader each
time a column dropdown changes, and dropping the reference to the previous one
leaves a running thread whose parent is later destroyed.

Fixed by tracking every worker and joining all of them in `shutdown()`, not
just the most recent. **The same latent bug was in Phase 6's `ResolvePage`** —
it supersedes loaders rarely enough never to have shown — and was fixed there
too.

## 10. Testing

| Suite | Count | What it holds |
|---|---|---|
| `tests/unit/test_candidate_import.py` | 87 | Parsing, normalisation, column detection, every refusal; real CSV fixtures and real generated workbooks |
| `tests/unit/test_reconciliation.py` | 44 | The classification rules as a table; determinism; co-occurrence; scale |
| `tests/unit/test_reconciliation_store.py` | 55 | Roster storage, idempotence, the six decisions, the ledger, persistence, the migration |
| `tests/unit/test_candidate_privacy.py` | 18 | The mandatory privacy criterion |
| `tests/integration/test_reconciliation_workflow.py` | 19 | The acceptance scenario with **real recognition and real sheets**, Phase 6 integration, and a 1-vs-4-worker comparison |
| `tests/gui/test_attendance_page.py` | 56 | The real page and dialog with a real project |
| **Total new** | **279** | |

Plus 5 new `qtguitesting` smoke checks (**43/43** passing).

**Full suite: 2,690 passed, 1 skipped.** `ruff check .` clean. `mypy` clean
(120 source files).

### The four assertions that matter most

```python
assert entry.candidate.imported_attendance is AttendanceState.ABSENT  # the file
assert script.machine_candidate_id == "999999"                        # the engine
assert entry.effective_attendance is AttendanceState.PRESENT          # the decision
assert history[0].reviewer == "Dr. Rahman"                            # who is answerable
```

An implementation that overwrote either of the first two would pass the third
and fail the phase.

## 11. What is not done

- **No reconciliation of a real cohort against a real roster.** Everything is
  synthetic. A genuine examination roster has its own column names, its own
  spelling of absence and candidates who really are missing; none of that has
  been seen.
- **No examination-scale run.** Matching is asserted at 10,000 candidates
  against 10,000 scripts, but the largest *real* batch in this project remains
  48 scans.
- **Scan file names can still leak a roll number into the log** (§8).
- **Reconciliation is per batch.** No project-wide view, and a cohort split
  across two batches must be reconciled twice. This is the most likely first
  request from a real user.
- **The operator's identity is a name, not an account** — as in Phase 6.
- **`.xls` is not supported**, by decision.
- **Leading zeros in a numeric Excel cell cannot be recovered.** Documented;
  the column must be formatted as Text.
- Windows only, as for every phase since 3.

## 12. For whoever picks this up

**Phase 8 (answer keys and scoring)** is next, and two things here are meant
for it:

- `reconciliation_script.is_primary` already records which of a candidate's
  scripts is the working one. Nothing reads it yet. **Scoring should**, rather
  than inventing its own rule for duplicates.
- `reconciliation_script.excluded` marks a script set aside from downstream
  processing. Scoring must honour it — and must not *delete* anything on the
  strength of it.
- The audit ledger takes any `entity_type`. A score override belongs there, not
  in a third history table.

**If you add a classification**, add it to `ReconciliationStatus` *and*
`ReconciliationIssue` (they are parallel by design, so an issue maps to a
headline), give it a place in `_STATUS_PRECEDENCE`, and raise it from
`reconcile()`. The GUI builds its filters from the enum and needs no change.

**If you touch the importer**, the two rules to keep are: never coerce an
identifier in a way that could merge two candidates, and never resolve an
ambiguity the operator could resolve better. Both have tests that will tell
you.

**The one thing not to do** is make a reconciliation decision write to
`registered_candidate`, to `machine_candidate_id`, or to the roster file. All
three are reachable; all three end the phase's guarantee.
