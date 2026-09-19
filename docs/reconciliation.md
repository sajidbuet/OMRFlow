# Candidate & attendance reconciliation (Phase 7)

The stage between a batch of read scripts and a set of results: does every
script belong to somebody, and did everybody who sat the paper hand one in?

For the recognition that produced the scripts, see
[`recognition_engine.md`](recognition_engine.md); for the review that settles a
disputed roll number, [`conflict_review.md`](conflict_review.md); for how the
modules fit together, [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## 1. Four values, and why they are four

Everything in this phase follows from keeping these apart:

| | Where it comes from | Where it lives | Can it change? |
|---|---|---|---|
| **Imported value** | The roster file the office supplied | `registered_candidate` | **Never.** Written once at import. |
| **Machine value** | What recognition read off the sheet | `batch_scan.result_json`, snapshotted on the conflict | **Never.** Phase 6's invariant. |
| **Human resolution** | What an operator decided | `reconciliation_decision` + `audit_event` | Superseded by a later decision; the earlier one stays in the ledger. |
| **Effective value** | The three folded together | *Computed* — `reconciliation.reconcile()` | Follows the inputs. |

> Source data says what was registered. Machine data says what OMR Flow
> recognised. Human resolution says what an operator decided. **A value
> changing does not entitle anything to forget the two before it.**

Concretely: an operator who establishes that a candidate recorded `ABSENT`
actually sat the paper does not make the roster say something else. The
imported value is still `ABSENT`, the raw cell is still `abs`, and the entry
reports both:

```text
Marked absent (list said marked absent)  →  Expected present
                                            by Dr. Rahman, "Candidate did attend; the roster is wrong"
```

And an operator who assigns a script read as `999999` to candidate `100004`
does not make recognition have read `100004`:

```text
machine_candidate_id    999999     (what the engine read; kept)
effective value         100004     (what a named operator decided)
```

---

## 2. The pipeline

```text
roster file (.csv / .xlsx)
     |
     v
candidate_import.read_roster        pure: parse, normalise, validate
     |
     v
reconciliation_store.import_roster  one transaction; refuses an invalid roster
     |
     |        batch_scan (Phase 5)
     |             |
     |             v
     |        review_store.effective_identifiers    machine ID + Phase 6 correction
     |             |
     +-------------+
     |
     v
reconciliation.reconcile            pure, deterministic classification
     |
     v
reconciliation_store.reconcile_batch    persists entries + script links
     |
     v
Attendance page                     queue, detail, resolution
     |
     +--> reconciliation_decision    the operator's standing decision
     +--> audit_event                append-only, shared with Phase 6
```

Reconciliation happens **after** recognition, never inside it. Phase 5's worker
pool, cancellation, resume and progress are untouched — asserted by running the
same sheets on one worker and on four and comparing the resulting
classifications, with the reported worker count checked so the comparison
cannot quietly be between two sequential runs.

---

## 3. Importing a candidate list

### What is supported

`.csv` and `.xlsx`. **Not `.xls`** — the pre-2007 binary format needs another
dependency for something Excel has discouraged for fifteen years, and a user
with one is told to re-save it.

### Column mapping

Three columns matter. Only the first is required.

| Column | Required | Notes |
|---|---|---|
| **Candidate ID** | Yes | The roll number. What a scanned sheet is matched against. |
| **Candidate Name** | No | Shown in the interface, never logged. |
| **Marks / Attendance** | No | See below. Without it, nobody is expected present or absent. |

The importer suggests a mapping from the headers — `Roll No.`, `Candidate ID`,
`Name`, `Total`, `Marks`, `Attendance`, and so on — and the operator can
override any of them. Everything else (`Sl.No.`, `Merit`, `Department`, `Room`)
is ignored.

**It refuses to guess when guessing could misfile a paper.** A file with both a
`Roll No.` column *and* a `Candidate ID` column names two equally exact
candidates for the same thing; the importer reports both and asks, rather than
preferring whichever it happens to list first. (An exact match beside a merely
plausible one is not a contest, and is resolved quietly.)

### Attendance from a marks column

An examination office usually has a marks sheet rather than an attendance
register, so the marks column doubles as one:

```text
ABSENT or ABS  ->  marked absent
anything else  ->  not marked absent (including a blank cell)
```

Case-insensitive, whitespace-insensitive. The comparison is
`str(value).strip().casefold()` against the exact set `{"absent", "abs"}` —
**whole tokens, never substrings**, so `ABSENTEE`, `ABSENCE` and `ABS123` are
not absences. A blank marks cell means "no absence was recorded", which is the
semantics of a marks sheet: a candidate who sat the paper has a mark, one who
did not has the word.

The column name is **not** hard-coded. `Total (90)` matches because `total`
does; so does `Total (75)`, and so does a column the operator picks by hand.

### Candidate IDs are identifiers, not numbers

Excel stores `15000001` as a number, and a naive read hands it back as
`15000001.0` — an ID that would match no scanned sheet ever again.
`normalise_candidate_id` is the single place this is dealt with:

| Cell | Becomes | Why |
|---|---|---|
| `15000001` (int) | `"15000001"` | |
| `15000001.0` (float) | `"15000001"` | The whole-number case that matters. |
| `12.5` | `"12.5"` | **Not** rounded — that could make two candidates one. |
| `"  0015  "` | `"0015"` | Trimmed; leading zeros in *text* survive. |
| `"ABC-123"` | `"ABC-123"` | Alphanumeric IDs pass through. |

> **If leading zeros matter, format the Excel column as Text.** A numeric cell
> containing `0015` is the number fifteen by the time any reader sees it;
> nothing downstream can recover the zeros.

### Validation, and what stops an import

Before anything is stored, the operator sees: rows read, candidates accepted,
expected present, marked absent, blank IDs, duplicate IDs.

**A repeated candidate ID stops the import.** Not a warning — the file states
two different facts about one person, and no phase downstream can be right
about which:

> Candidate ID 15000001 occurs more than once in the imported candidate list
> (rows 2 and 4). Candidate IDs must be unique before reconciliation can
> proceed.

A blank candidate ID stops it too, naming the row. **A failed import leaves
nothing behind** — the roster row and every candidate are written in one
transaction.

### The sample template

*Download Sample Template…* on the Attendance stage writes
`candidate_attendance_sample.xlsx` wherever the operator chooses. It is
packaged **inside** the application at
`src/omr_scanner/resources/templates/`, read through `importlib.resources`, so
it resolves for a source checkout, an editable install, a wheel and a frozen
build alike. (The top-level `resources/` directory is dev fixtures and is never
shipped — see the packaging comment in `pyproject.toml`.)

It contains placeholder names and fictional roll numbers only, it is copied
rather than opened for writing, and an existing destination is confirmed
before it is replaced.

---

## 4. What reconciliation decides

Two normal outcomes and five exceptions.

| Status | Means |
|---|---|
| **Matched** | Expected to attend; exactly one script. |
| **Absent, confirmed** | Recorded absent; no script. *Not an exception.* |
| **Unknown candidate ID** | A script read as an ID that is not on the roster. |
| **Duplicate script** | Two or more scripts for one candidate. |
| **Present but no script found** | Expected to attend; nothing arrived. |
| **Marked absent but script found** | Recorded absent; a script arrived anyway. |
| **Candidate ID not yet resolved** | The sheet's roll number is still awaiting review on the **Resolve** stage. |

That last one exists so an unread identifier is never reported as an *unknown
candidate*. The two need different actions: one is a recognition problem with
its own stage, the other a roster problem. Conflating them sends an operator
hunting for a candidate who may turn out to be perfectly ordinary.

A candidate whose attendance the roster never stated is not expected to hand
anything in — a roster imported without a marks column would otherwise report
every non-attendee as a missing script.

### Conditions co-occur

A candidate recorded absent who has two scripts is **both**
`ABSENT_WITH_SCRIPT` **and** `DUPLICATE_SCRIPT`. An entry therefore carries a
*set* of issues and derives its headline from them by documented precedence:

```text
candidate ID not yet resolved   (everything below is provisional while unread)
unknown candidate ID            (a script filed under nobody is the likeliest to be lost)
marked absent but script found  (questions the roster itself)
duplicate script                (questions the scanning)
present but no script found     (often the consequence of one of the above)
```

The precedence decides only what the *Status* column says. Every issue stays on
the entry, the table appends `(+ Duplicate script)` to the headline, and the
detail panel lists them all. **A physical script must never disappear because
another exception exists.**

---

## 5. Resolving an exception

Every action requires a named operator (*File > Settings > Reviewer* — the same
name Phase 6 records) and appends an audit event.

| Exception | What the operator can do |
|---|---|
| Unknown candidate ID | Assign the script to the right candidate; or accept it as-is with a reason. |
| Duplicate script | Reassign a misidentified script; set one aside as an accidental re-scan; nominate the working script; or leave it. |
| Present but no script | Override attendance to absent; accept that the script is missing; or assign an unplaced script to them. |
| Marked absent but script found | Override attendance to present; reassign the script; or accept it. |
| Candidate ID not yet resolved | Resolve it on the **Resolve** stage, then reconcile again. |

### Nothing is deleted, ever

Setting a script aside as an accidental re-scan **excludes** it: it stops
counting towards its candidate and is shown as *SET ASIDE*. The scan row, its
recognition result, its overlay, the reason and the audit trail all remain.
That is the difference between resolving a duplicate and destroying evidence.

### Cascades are surfaced, not hidden

Every decision re-runs reconciliation, so a consequence appears immediately.
Assigning a script to a candidate who already has one produces a duplicate
**and says so at once**, rather than at export time:

```text
assign 999999 -> 100001   (who already had a script)
    100001: Duplicate script, 2 scripts, needs review
```

An entry whose issues a decision did *not* clear stays **open**. Being touched
is not the same as being fixed.

---

## 6. Re-running, and re-importing

`reconcile_batch` is **idempotent**. The entry and script rows for a
roster/batch pair are rewritten wholesale on every run, so:

- running it twice with unchanged inputs leaves exactly what one run leaves;
- no second set of exception records accumulates;
- **a stale classification cannot survive a change of roster**, which is the
  failure a merge-in-place would produce.

Operator decisions are *not* touched by a re-run: they are the **input**, not
the output. That is why they live in their own table.

Importing a second roster warns first, then **supersedes** the active one. The
old roster, its candidates and every decision taken against it are kept, not
merged and not deleted — an audit trail that points at a deleted roster
explains nothing. An earlier roster can be made active again.

---

## 7. Storage

| Table | Holds |
|---|---|
| `candidate_roster` | One import: its file name, worksheet, column mapping and counts. |
| `registered_candidate` | One candidate as the file declared them. **Write-once.** |
| `reconciliation_run` | When a roster/batch pair was last reconciled, and its counts. |
| `reconciliation_entry` | One candidate's state, or one unplaceable script group. A **cache**. |
| `reconciliation_script` | One row per scan — including unplaced and set-aside ones. |
| `reconciliation_decision` | The operator's standing decisions. The **input**. |

Field-by-field: [`DATA_MODEL.md`](DATA_MODEL.md). Created by **migration 4**,
which also adds `entity_type`/`entity_id` to `audit_event` so a decision about a
candidate or a script is recorded in the **same append-only ledger**, under the
same triggers, as a decision about a recognition conflict.

Rows written before migration 4 read back as `entity_type='conflict'` and were
**deliberately not backfilled** — an `UPDATE` on `audit_event` is aborted by
those triggers, which is exactly their job. `ADD COLUMN` is a schema change and
does not fire them.

The source roster file is **never modified, moved or copied into the project**.
Only its *name* is kept — not its path, because a project database is shared and
a path can name somebody's home directory.

---

## 8. Scale

An indexed match, not a scan: candidate ID → candidate, built once per run.
Matching each script by walking the roster would be O(scripts × candidates) —
a hundred million comparisons for a ten-thousand-candidate cohort. Ten thousand
candidates and ten thousand scripts reconcile in well under a second.

The table filters, searches and counts **in SQL**, and the page holds detached
value objects rather than ORM rows. Import and reconciliation both run in a
`QThread`, so neither freezes the interface.

---

## 9. Privacy

**No candidate ID, name, marks value or attendance value tied to a candidate
may appear in an application log.** Log lines carry counts, source row numbers,
roster ids, scan ids and the *kind* of an error — things that identify work
rather than people:

```python
_LOGGER.info("Roster read: rows=%d accepted=%d duplicate_id=%d", ...)   # yes
_LOGGER.error("Unknown candidate ID: %s", candidate_id)                 # never
```

A user-facing message *may* name a candidate — the duplicate-ID refusal has to,
or the operator cannot find the row — which is precisely why such messages are
never what reaches the log. Even an unexpected exception is logged by **type
only**, because a library's message can quote the cell it choked on.

Enforced by `tests/unit/test_candidate_privacy.py`, which exercises every path
with distinctive synthetic values and asserts none reaches captured logging,
plus a grep that fails if a Phase 7 module formats candidate data into a log
call at all.

### One known exposure, stated plainly

The Phase 3 pipeline logs the **file name** of each scan it reads. That is
documented policy and it is the only way to tell which scan failed — but an
office whose scan files are named by roll number (which is exactly what
OMRFlow's own rename step produces) therefore has roll numbers in its
application log, put there by Phase 3 rather than by anything in this phase.

If logs leave the machine, either turn off renaming or treat the log as
candidate data. Fixing it means logging a scan id instead of a name, which
costs the diagnostic for headless runs; the trade-off has not been made.
`tests/unit/test_candidate_privacy.py` pins the current behaviour so the
exception is written down rather than discovered.

---

## 10. What Phase 7 does not do

- It does not score anything. Scripts, candidates and attendance only;
  answer keys and marks are Phase 8.
- It does not verify that a human decision is *right*. It records who made it,
  when and why, so a wrong one can be found and reversed.
- It does not authenticate. The operator's name is a name, not an account.
- It cannot see across batches. Reconciliation is per roster, per batch; there
  is no project-wide view.
- It has been exercised against **synthetic sheets and rosters**. No
  examination-scale reconciliation with real operators has been run; see
  `development/PHASE_07_HANDOFF.md`.
