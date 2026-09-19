# Phase 6 handoff — Conflict Detection & Human Resolution

**Implemented:** 2026-09-19
**Version:** 0.1.0.dev0
**Environment verified on:** Windows 11, Python 3.12.7, PySide6 6.11.2, OpenCV
5.0.0, NumPy 2.5.3, SQLAlchemy 2.x, 16 logical CPUs

This document is the entry point for whoever continues the work. Read alongside
`docs/conflict_review.md` (the operator-facing description), `docs/DATA_MODEL.md`
(the two new tables, the migration, and why two *planned* entities were not
built) and `docs/ARCHITECTURE.md`.

**Phase 6 makes what the machine was unsure about visible, correctable and
traceable. It does not make the machine more accurate, and it does not verify
that a human correction was right** — only who made it, when, and why. A
confidently wrong reading never reaches the review queue at all; narrowing that
remains Phase 3's open item, and Phase 4's calibration is the tool for it.

---

## 1. What already existed

Phase 6 did not start from scratch, and most of the work was resisting the urge
to build a second version of something already present.

| Capability | Where | Verdict |
|---|---|---|
| Per-group `needs_review` judgement, from the template's own `ambiguity_margin` and `min_confidence` | `recognition/decide.py` | **Read, never re-derived** |
| The authoritative zone→group→cells→labels mapping | `recognition/fields.py` `zone_groups()` | Reused verbatim |
| Explicit ambiguity: blank / multiple (`B-D`) / uncertain / unreadable | `recognition/` + `MarkStatus` | Preserved end to end |
| Versioned, round-tripping `ScanResult` | `services/recognition_models.py` | Extended by one field (§4) |
| Per-bubble evidence, optional (`keep_bubble_measurements`) | `services/recognition_service.py` | Reused for review |
| Durable batches, resume, retry, crash repair | `services/batch_store.py` (Phase 5) | **Untouched**, one read helper added |
| Multiprocessing pool, cancellation, progress, worker policy | `services/parallel_batch.py`, `batch_progress.py` | **Untouched** |
| Diagnostic overlays over a registered page | `gui/calibration/` (Phase 4) | Reused, not re-drawn |
| `ScanPreviewView` (zoom, fit, pan) | `gui/scan/preview.py` | Reused; gained a public `set_zoom` |
| Deterministic re-read of one sheet in a background thread | `gui/scan/` `PreviewWorker` (Phase 3) | Pattern followed exactly |
| Forward-only migration framework | `database/migrations.py` | Followed exactly |
| CSV export | `services/scan_export.py` | Two columns appended |

**Nothing was rewritten.** No second recognition path, no second threshold, no
second definition of what a group is, no second batch pipeline.

## 2. What was missing

1. Nowhere to *record* that a value was disputed. A `needs_review` flag lives
   inside one result; a queue needs a persistent, queryable, identity-stable row.
2. No provenance at all. A corrected value would simply have replaced the
   machine's, which is the one outcome the phase exists to prevent.
3. No batch-level view. A duplicate identifier is invisible from inside a single
   sheet, and Phase 5's workers cannot see each other by design.
4. No way to put the disputed field in front of a person — zoomed, in context,
   and on the original — without the GUI doing projective arithmetic.
5. No reviewer identity anywhere in the application.

## 3. What was built

### `domain/review.py` — the vocabulary

Pure: no Qt, no SQLAlchemy, no OpenCV, no `Path` I/O. `ConflictType` (22
members), `ConflictScope`, `ConflictState`, `ValueSource`, `ReviewAction`,
`ReasonCode`, `FieldRef`, `Candidate`, `MachineObservation`, `Provenance`,
`ReviewCounts`.

The enums carry their own rules as properties rather than leaving them to
callers: `ConflictType.scope`, `.is_processing_failure`, `.allows_value_correction`,
`.label`; `ReviewAction.is_human`, `.sets_effective_value`;
`ReasonCode.requires_text`. That is what lets the GUI build a type filter and
the exporter read a provenance without either importing the other, and it is why
the architecture test still passes with a review page in the GUI layer.

### `services/conflict_policy.py` — detection

Pure functions. Same result in, same conflicts out — asserted, because a queue
that differs between two runs over the same batch cannot be trusted.

`detect_conflicts(result, template, policy)` maps each group's `MarkStatus` to a
conflict type through three per-kind tables (`_IDENTIFIER_BY_STATUS`,
`_SET_CODE_BY_STATUS`, `_ANSWER_BY_STATUS`). A failed registration
short-circuits to **exactly one** sheet-level conflict — there are no values on
an unregistered page to dispute, and emitting a hundred would bury the one that
matters.

`detect_duplicate_identifiers(results)` is separate and batch-scoped.

`ConflictPolicy` holds the three judgement calls: `flag_blank_answers` (off — a
candidate may leave a question blank), `flag_alignment_warnings` (off — the
repository's own sample raises one on every sheet), `flag_assumed_orientation`
(**on** — an inverted sheet read as upright produces a full set of confidently
wrong answers).

**There are no thresholds in this module.** It reads the engine's `needs_review`
and the template's `min_confidence`. Calibrating a template in Phase 4 therefore
moves the conflict queue with it, and there is exactly one place where "sure
enough" is configured.

### `services/review_store.py` — the repository

One write path for events (`_append_event`) and no update or delete for them at
all. Every human action is one transaction: the event and the state change
commit together or neither does.

`sync_conflicts()` is idempotent against the unique constraint
`(batch_id, scan_id, conflict_type, zone_id, group_key)`, so Phase 5's resume
and retry update conflicts instead of duplicating them. `_refresh_conflict` is
**the only** writer of the `machine_*` columns, and it appends a `RE_RECOGNISED`
event before touching them — even the machine does not revise itself silently.

`provenance_for()` folds a conflict's ordered events over the machine's reading.
There is no `resolved_value` column, deliberately; see §7.

### Migration 3

`review_conflict`, `audit_event`, two indexes, one unique constraint, and two
`BEFORE UPDATE` / `BEFORE DELETE ... RAISE(ABORT)` triggers. Purely additive:
no existing table or column was altered.

### `gui/review/` — the Resolve page

`page.py` (queue, workspace, decisions), `worker.py` (`SheetWorker`, re-reads one
sheet off the GUI thread), `history_dialog.py` (`render_history()` is pure and
testable; the dialog only displays it).

### Elsewhere

- `ScanResult.source_transform` — the inverse homography the engine already
  computed, now carried on the result, plus
  `recognition_service.map_canonical_to_source()`. §4.
- Scan page: conflict detection after a batch, a **Review Conflicts** button, an
  unresolved count, and an export warning.
- `scan_export`: `value_source` and `unresolved_conflicts`, appended after the
  existing columns so nothing downstream shifted.
- `AppConfig.reviewer_name` and a Reviewer section in Settings.
- `batch_store.scan_ids_by_path()` — one read helper. Phase 5 was otherwise
  untouched.

## 4. Why `source_transform` exists

The §13 requirement is a review workspace showing the **original scan** with the
disputed field located on it. The GUI has three ways to do that, two of them
wrong:

1. Draw canonical coordinates on the original image. **Wrong** — the original is
   not canonical; the highlight would sit somewhere plausible and incorrect.
   This is exactly the failure Phase 4's audit named as the most serious
   possible: an interface that looks convincing while showing different geometry
   from the engine's.
2. Recompute the homography in the GUI. **Wrong** — the GUI may not import
   OpenCV or NumPy (`tests/unit/test_architecture.py` fails the build), and a
   second implementation of the engine's own geometry is a second thing to drift.
3. Have the engine carry the transform it already computed, and one
   services-layer function apply it.

Three it is. `ScanResult.source_transform` is nine floats, defaulted, so older
stored results still load; `map_canonical_to_source()` is the only place the
arithmetic happens. The GUI receives points.

**The original view carries no overlay** — only a note saying where the field
is. Locating it is enough; drawing engine coordinates on a non-canonical image
would be the lie in (1) by another route.

## 5. Why review re-reads the sheet

`keep_bubble_measurements` is **off** for batches. Five hundred bubble records
per sheet is most of a gigabyte over ten thousand sheets, which is why Phase 3
made it optional in the first place.

Review needs that evidence, and a rendered page, for **one sheet at a time**.
Recognition is deterministic, so re-reading the single sheet being reviewed
reproduces exactly the evidence behind the stored result — a few hundred
milliseconds in a `QThread`, the same deterministic-re-read pattern Phase 3's
`PreviewWorker` established.

The worker starts only when the selected conflict belongs to a **different**
sheet, so walking one sheet's ten conflicts decodes it once. A result stored
before the evidence field existed still opens; the panel says the scores were not
kept rather than failing.

## 6. Why detection runs in the coordinator

After the batch finishes, in the main process. Two architectural reasons:

1. A worker process must not open the project database. SQLite is a
   single-writer store and Phase 5 keeps it that way by construction; handing a
   connection to eight `spawn`ed processes would undo that deliberately.
2. A duplicate identifier is **not a property of one sheet**. It cannot be seen
   while reading one, and sharing mutable state between workers to find it would
   be both slower and less correct than one pass at the end.

The consequence worth stating: **Phase 5's pipeline is bit-for-bit unchanged.**
Same pool, same worker count, same cancellation, same resume, same progress.

## 7. Why the effective value is not stored

There is no `resolved_value` column, and `docs/DATA_MODEL.md` records the
planned `FieldValue` table as *superseded* rather than pending.

A stored final value would be a **third copy of the truth** — after the
recognition result and the conflict's machine snapshot — kept in step across
corrections, reopenings and re-corrections by application code. Every drift bug
this phase could have is a drift bug between two of those copies.

The fold cannot drift, because there is nothing to drift from: the events *are*
the record, and one function reads them. `Provenance` carries the effective
value, its source, the machine value, the reviewer, the reason, the timestamp
and the originating event — all derived, all reconstructible, all traceable.

The same reasoning applies to `ReviewConflict.state`, which **is** cached for
the queue's sake — and `recompute_state()` rebuilds it from the events, with a
test asserting the two agree over sequences of actions. A cache that can be
proved equal to its source is not a second source of truth.

## 8. Append-only, at three levels

1. **Service surface.** `review_store` exposes `_append_event` and no update or
   delete for events. A test asserts the module exports nothing matching one.
2. **Application.** Nothing constructs an `UPDATE` or `DELETE` against
   `audit_event`. A human action writes an event and a state change; it never
   touches `machine_value`.
3. **Database.** Two SQLite triggers abort either statement outright.

`audit_event` carries **no foreign key**, so the record that a named person
decided something outlives the row it was about — and so Phases 7-9 can audit
into the same ledger without a schema change.

This is not a cryptographically chained ledger and does not claim to be. §51 of
the brief explicitly ruled out building tamper-proof enterprise logging. A
database administrator with file access can still alter it.

## 9. Testing

| Suite | Count | What it holds |
|---|---|---|
| `tests/unit/test_conflict_policy.py` | 40 | Detection in isolation: the taxonomy, the defaults, determinism, template-driven labels |
| `tests/unit/test_review_store.py` | 48 | The ledger rules, provenance, transactions, the queue at 10,000 conflicts |
| `tests/integration/test_conflict_review.py` | 24 | Scenarios A-E against **real rendered sheets** and the real engine, plus migration onto an existing project and a 1-vs-4-worker comparison |
| `tests/gui/test_resolve_page.py` | 37 | Queue, workspace, decisions, history, reopening, object names |
| **Total new** | **149** | |

Plus 3 new `qtguitesting` smoke checks (**38/38** passing) and four screenshots.

**Full suite: 2,411 passed, 1 skipped.** `ruff check .` clean. `mypy` clean
(110 source files).

The integration tests start from **marks rendered on a page** rather than
hand-built fixtures, so the conflicts under test are the ones the engine
genuinely produces — not the ones a fixture author assumed it would.

### The three assertions that matter most

Everything else is supporting detail:

```python
assert provenance.value == "B"              # what the reviewer decided
assert provenance.machine_value == "B-D"    # what the machine saw, intact
assert provenance.reviewer == "Dr. Rahman"  # who is answerable for it
```

A page that overwrote the machine value would pass the first assertion and fail
the entire phase.

## 10. What is not done

- **No review session with real operators on a real batch.** Everything is
  automated. The workflow has never been driven by someone reviewing sheets they
  cared about, which is the only way to learn whether the queue is *usable*
  rather than merely correct.
- **Queue performance is asserted on a synthetic 10,000-conflict batch**, by
  counting SQL statements rather than timing a machine. No real
  examination-scale review has been timed, and the per-sheet re-read has been
  measured only on the development machine.
- **The policy defaults are reasoned, not evidenced.** Whether an examination
  office wants blank answers flagged, or alignment warnings surfaced, is not yet
  known.
- **Reviewer identity is a name, not an account.** There is no authentication;
  the ledger records who *said* they made a decision. Deliberate — §18 and §50
  of the brief both forbade building an auth system here — but it is a real
  limitation for a high-stakes deployment, and the natural place to fix it is
  Phase 10.
- **Windows only.** Linux and macOS untested, as for every phase since 3.
- **The conflict queue is per batch.** A cross-batch view — "every unresolved
  conflict in this project" — would be useful and does not exist.
- **Phase 6 does not bound the error rate.** A confidently wrong reading never
  reaches the queue.

## 11. For whoever picks this up

**Phase 7 (candidates and attendance)** is the natural next step, and two Phase 6
decisions were made with it in mind:

- `audit_event` has no foreign key and takes `entity_type` + `entity_id`, so
  attendance corrections, answer-key edits and result overrides can all audit
  into the same ledger with **no schema change**. Use it rather than adding a
  second history table.
- `IDENTIFIER_DUPLICATE` already exists and is batch-scoped. "Unknown roll
  number" is the obvious sibling, and it belongs in `ConflictType` next to it —
  not in a parallel reconciliation-only mechanism.

**If you add a conflict type**, add it to `ConflictType` with its `scope`,
`is_processing_failure` and `allows_value_correction` properties correct, and
raise it from `conflict_policy`. Do not add a threshold: read the engine's
judgement, as everything else here does.

**If you change the review GUI**, the free-text row and the value buttons are
mutually exclusive on purpose (`group_labels()` empty means no fixed alphabet),
and a processing failure shows neither. This was found by *looking at a
screenshot* during §57 validation — duplicate-identifier conflicts had offered
only a "(blank)" button, which is useless for correcting a roll number. Keep
capturing the screenshots.

**The one thing not to do** is make a correction write to the recognition
result, the `machine_*` columns, or a stored final value. All three are
reachable; all three end the phase's guarantee. The greps in §63 of the brief —
assignments to a result's values, `UPDATE`/`DELETE` against `audit_event`, a
hard-coded answer alphabet in the GUI, writes to `machine_*` outside
`_refresh_conflict` — are worth re-running after any change here.
