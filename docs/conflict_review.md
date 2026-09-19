# Conflict detection and human review (Phase 6)

The stage between processing a batch and trusting its results: every value
recognition could not decide is put in front of a person, with the evidence
behind it, and what they decide is recorded permanently against their name.

For the recognition that produces the conflicts, see
[`recognition_engine.md`](recognition_engine.md); for the batch that runs it,
[`scan_workflow.md`](scan_workflow.md); for how the modules fit together,
[`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## 1. The one rule everything else follows from

> **The machine's observation is never lost.**
> Human judgement is *added* as audited information. It never rewrites history.

A reviewer who decides that a double-marked question meant `B` does not make
`B-D` untrue about the paper. Both survive:

```text
machine_value     B-D      (what recognition read; immutable)
effective value   B        (what a named reviewer decided; exported)
```

The effective value is not a stored column. It is **projected** from the
machine's reading plus the ordered audit events, so a correction, a reopening
and a second correction each add to the record rather than replacing what came
before. That projection is
`omr_scanner.services.review_store.provenance_for`, and nothing else in the
application decides what a final value is.

---

## 2. The lifecycle

```text
recognition
     |
     v
conflict detection          services/conflict_policy.py   (pure, deterministic)
     |
     v
persistent conflict         review_conflict table
     |
     v
human review                gui/review/  (queue, workspace, decision)
     |
     v
resolution                  services/review_store.py      (one transaction)
     |
     +--------------------> audit_event table             (append-only)
     |
     v
effective value + provenance
     |
     v
CSV export, and every later consumer
```

Detection runs **in the coordinating process, after a batch finishes** - never
inside a worker. Two reasons, both architectural: a worker process must not
open the project database (SQLite is a single-writer store and Phase 5 keeps it
that way by construction), and a duplicate identifier is not a property of one
sheet, so it cannot be seen while reading one.

---

## 3. What becomes a conflict

Derived from what recognition *already decided*, never from a second opinion
about the pixels.

### Student ID

| Conflict | Raised when |
| --- | --- |
| Student ID blank | No mark in any position. One conflict, not one per column. |
| Student ID incomplete | One position blank while others carry marks. |
| Student ID multiple marks | One position carries more than one mark. |
| Student ID uncertain | A mark too faint, or too close to its runner-up. |
| Student ID unreadable | A position could not be sampled at all. |
| Student ID low confidence | Resolved, but below the template's own minimum confidence. |
| Duplicate student ID | Two or more sheets in the batch resolved to the same identifier. |

A blank or ambiguous identifier column is *always* a conflict, even when the
engine was confident about the blankness: an identifier with a gap cannot name
a candidate.

### Set code

Blank, multiple marks, uncertain, unreadable, low confidence - the same five
states. Set codes may be **multi-position and multi-character** (`10`, `11`,
`12`), and each printed position is reported separately; nothing here assumes
one character.

### Questions

Multiple marks, uncertain, unreadable, low confidence.

**A blank answer is not a conflict by default.** A candidate is entitled to
leave a question unanswered, and one queue entry per unanswered question would
bury the conflicts that matter. An examination where every question is
compulsory can switch it on
(`ConflictPolicy.flag_blank_answers`).

### The sheet itself

| Conflict | Raised when |
| --- | --- |
| Registration failed | The page could not be rectified. **One** conflict for the sheet - there are no values on it to dispute. |
| Orientation assumed | Which way up the page is was assumed, not measured. On by default: an inverted sheet read as upright produces a full set of confidently wrong answers. |
| Alignment warning | Registered with a geometry reservation. **Off** by default - the repository's own sample raises one on every sheet. |
| Image unreadable | The file could not be decoded. |
| Processing error | An unexpected failure inside recognition. |

The last two are **processing failures, not values**. A corrupt JPEG cannot be
answered with `A`, `B`, `C` or `D`, so the review interface offers
acknowledgement and deferral for them and no value buttons at all.

### Where the thresholds come from

There are none here. Conflict detection reads the engine's own `needs_review`
flag, which the decision layer computed from **the template's own**
`ambiguity_margin` and `min_confidence`. Calibrating a template in
[Phase 4](calibration_workflow.md) therefore moves the conflict queue with it,
and there is exactly one place where "how sure is sure enough" is configured.

---

## 4. The reviewer's workflow

1. Process a batch on the **Scan** stage. Conflicts are detected automatically
   when it finishes, and the page says how many need review.
2. Press **Review Conflicts**, or open the **Resolve** stage.
3. Narrow the queue if you like - by state (*Unresolved / Resolved / Deferred /
   Withdrawn*), by conflict type, or by searching a student ID or file name.
4. Select a conflict. Three views load:
   - **Zoomed field** - the disputed bubbles, magnified, with only that group
     ringed. This is what you decide from.
   - **Normalised sheet** - the whole rectified page, with the same overlay.
   - **Original scan** - the file exactly as it arrived, never modified.
5. Read **What the machine saw**: its value, its status, its decision score,
   and - when per-bubble evidence is available - every option's measured
   **fill score**. These are coverage measurements, not probabilities, and are
   labelled as such.
6. Decide:
   - **Accept machine value** - you inspected it and the machine was right.
   - a **value button** (or the free-text box, for a whole identifier) - a
     correction.
   - **Defer** - postpone it.
   - **Reopen** - change a decision already made.
7. Give a **reason**. Accepting records *"Machine result visually confirmed"*
   automatically; a correction asks you to pick one, and "Other" requires an
   explanation.
8. **History...** shows the complete provenance at any time.
9. **Next unresolved** skips to the next thing nobody has decided.

### Keyboard

| Key | Action |
| --- | --- |
| Left / Right | Previous / next conflict |
| Enter | Accept the machine value |
| D | Defer |

Choosing a *value* deliberately has no shortcut: a stray keypress that silently
corrected an answer is precisely the accidental destructive edit this stage
exists to prevent, and every correction must carry a reason anyway. Typing in
the reason box consumes the keys before any shortcut sees them.

### Your name

Set it in **File > Settings > Reviewer**. It is remembered between sessions and
recorded against every decision. **A correction cannot be saved without one** -
the exit criterion for this phase is that a final value traces back to a
*named* correction, so an unnamed one is not allowed to exist.

It is a name, not an account. Phase 6 needs attribution, not authentication.

---

## 5. Conflict states

```text
        OPEN ──accept/correct──▶ RESOLVED ──reopen──▶ OPEN
          │                          ▲
          ├──────defer──────▶ DEFERRED
          │
          └──(machine withdraws)──▶ WITHDRAWN
```

| State | Meaning |
| --- | --- |
| **Open** | Awaiting review. |
| **Resolved** | A named reviewer decided - by accepting or by correcting. |
| **Deferred** | A named reviewer deliberately postponed it. Still counts as unresolved. |
| **Withdrawn** | Re-reading the sheet no longer produces this conflict. |

Conflicts are **never deleted**. A withdrawn conflict is kept because the fact
that the machine once disputed this value is itself evidence.

**A machine may withdraw its own complaint; it may never erase a person's
decision.** A resolved or deferred conflict keeps its state and its history
whatever a later re-read says.

---

## 6. Provenance, and what "reopen" means

Reopening withdraws the *decision*, not the record of it. The effective value
falls back to the machine's until somebody decides again, and the original
correction stays in the ledger with its own reviewer, reason and timestamp:

```text
14:32:10  Machine recognition          Value: A-C
14:35:22  Corrected by Dr. X           A-C → A     Multiple marks - dominant mark selected
15:12:04  Reopened by Dr. Y
15:13:17  Corrected by Dr. Y           A   → C     Machine misclassification
```

Effective value: `C`. Machine value: still `A-C`. Dr. X's decision is still
recorded as Dr. X's, for `A` - never rewritten to look as though they had
chosen `C` all along.

---

## 7. Why the ledger cannot be rewritten

Append-only is enforced at three levels, so that it does not depend on
developer discipline:

1. **The service surface.** `review_store` exposes an internal
   `_append_event` and *no* update or delete for events. A test asserts the
   module exports nothing matching one.
2. **The application.** Nothing constructs an `UPDATE` or `DELETE` against
   `audit_event`. A human action writes an event and a state change; it never
   touches `machine_value`.
3. **The database.** Migration 3 installs two SQLite triggers that abort any
   `UPDATE` or `DELETE` on `audit_event`. A hand-written statement during
   maintenance, or a future refactor that forgets, fails loudly rather than
   quietly losing history.

`audit_event` also carries **no foreign key**. A conflict may in principle be
removed by housekeeping; the record that a named person decided something about
it must outlive that.

This is not a cryptographically chained ledger, and does not claim to be. A
database administrator with direct access can still alter the file; that is
outside what an application can prevent, and building tamper-proof logging was
explicitly out of scope for this phase.

---

## 8. Atomicity

Every review action is one transaction. An audit event without its state
change - or a state change without its event - cannot be committed. If the
write fails, neither happens, and a test asserts exactly that by making the
event insert explode mid-transaction.

---

## 9. Export

Corrections reach the CSV through one place:
`review_store.sheet_resolutions`, which the export reads and nothing else
computes. Two columns carry the provenance:

| Column | Meaning |
| --- | --- |
| `value_source` | `human` when a named reviewer decided anything on this sheet, `machine` otherwise. |
| `unresolved_conflicts` | How many of that sheet's disputes are still waiting. |

Exporting a batch with unresolved conflicts warns first, stating the count. It
does not block - an interim export is legitimate - but unresolved ambiguity
never leaves the application silently looking like finished data.

The export reads the resolutions; it never writes to them, and the machine's
own values stay in the project database whatever the file says. A CSV can
always be regenerated.

---

## 10. Idempotence, and Phase 5

A conflict's identity is `(batch, scan, type, zone, group)`, enforced by a
unique constraint. Re-running a batch, resuming one, or retrying a failed sheet
therefore **updates** the existing conflicts rather than creating a second set.

If a re-read changes what the machine saw, that is recorded too - as a
`RE_RECOGNISED` event, before the observation is updated. Even the machine does
not get to revise itself silently.

Detection touches nothing about how a batch runs: the worker pool, the worker
count, cancellation, resume and progress reporting are all exactly as Phase 5
left them.

---

## 11. Performance

A ten-thousand-sheet batch can carry thousands of conflicts.

- The queue is **filtered, ordered and paged in SQL**, and holds detached value
  objects rather than ORM rows.
- The summary counts are two **grouped queries**, never a walk over the rows.
- **No image is loaded for a row that is not being looked at.** The sheet is
  decoded and re-read only when a conflict is selected, in a background thread,
  and only when it is a *different* sheet from the last one - so walking one
  sheet's ten conflicts decodes it once.

### Why the sheet is re-read rather than stored

A batch deliberately discards per-bubble evidence and previews: five hundred
bubble records per sheet is most of a gigabyte over ten thousand sheets.
Review needs both, for one sheet at a time. Recognition is deterministic, so
re-reading the single sheet being reviewed reproduces exactly the evidence
behind the stored result, for a few hundred milliseconds in a worker thread.

A result stored before this evidence existed still opens; the panel says the
scores were not kept rather than failing.

---

## 12. What Phase 6 does not do

- It does not make recognition more accurate. It makes what recognition was
  unsure about visible and correctable.
- It does not verify that a correction is *right*. It records who made it, when
  and why, so that a wrong one can be found and reopened.
- It has been exercised against **synthetic sheets and the repository's single
  real sample**. No examination-scale review session with real operators has
  been run; see `development/PHASE_06_HANDOFF.md`.
