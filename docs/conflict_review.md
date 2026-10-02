# Conflict detection and human review (Phase 6)

The stage between processing a batch and trusting its results: every value that
decides **which record a sheet is** and that recognition could not settle is put
in front of a person, with the evidence behind it, and what they decide is
recorded permanently against their name.

> **Conflict Resolution is for identity, not for answers.**
> Only the **student ID / roll number**, the **set code**, and the **sheet
> itself** can produce a conflict. An ambiguous or multiply-marked *answer* is a
> recognition result: it stays in the result, it is exported and scored as the
> sheet was marked, and it never waits for a human.
>
> See [§3](#3-what-becomes-a-conflict) for why, and
> [§12](#12-projects-from-an-earlier-build) for what happens to a project
> scanned before this was true.

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
about the pixels - and only where the ambiguity leaves the **record** unusable:

```text
recognition result
     |
     +-- student ID ambiguity ----> conflict   (nobody knows whose script this is)
     |
     +-- set code ambiguity ------> conflict   (nobody knows which paper it answers)
     |
     +-- sheet unreadable --------> conflict   (nothing was measured at all)
     |
     +-- answer ambiguity --------> answer result only
```

The single definition lives in
`omr_scanner.domain.review.ConflictType.requires_resolution`; every count,
queue, badge and block asks it rather than deciding for itself.

### Student ID

| Conflict | Raised when |
| --- | --- |
| Student ID blank | No mark in any position. One conflict, not one per column. |
| Student ID incomplete | One position blank while others carry marks. |
| Student ID multiple marks | One position carries more than one mark. |
| Student ID uncertain | A mark too faint, or too close to its runner-up. |
| Student ID unreadable | A position could not be sampled at all. |
| Student ID low confidence | Resolved, but below the template's own minimum confidence. |
| Duplicate student ID | Two or more sheets that count in the **scan session** (any of its batches; revised phase 4) resolved to the same identifier. A rejected, excluded, deferred or replaced sheet does not take part, and another session never does. Each record lives in its sheet's own batch. |

A blank or ambiguous identifier column is *always* a conflict, even when the
engine was confident about the blankness: an identifier with a gap cannot name
a candidate.

### Set code

Blank, multiple marks, uncertain, unreadable, low confidence - the same five
states. Set codes may be **multi-position and multi-character** (`10`, `11`,
`12`), and each printed position is reported separately; nothing here assumes
one character.

### Questions — never

**No answer produces a conflict, in any state.** Not a double mark, not a mark
too faint to accept, not a group that could not be sampled, not a blank.

An answer ambiguity is a fact about the paper, and the paper is not in dispute:

| What the sheet says | Where it lives |
| --- | --- |
| `B` | the answer value |
| `B-D` (two bubbles filled) | the answer value - **both marks kept** |
| `?` / `B?` (too faint, or too close to call) | the answer's display value and status |
| `` (blank) | the answer value |

All of it stays in the recognition result - `status`, `needs_review`, `value`,
the per-bubble fill ratios - is exported in the question column, and is counted
in the sheet's `warning_count`. Nothing is discarded; it is simply not a
*conflict*.

Why it changed: an examination of a hundred questions can produce a hundred
answer ambiguities per sheet. A queue holding them made the two entries that
genuinely stopped a script being attributed impossible to find, and blocked a
batch on ambiguity that no human decision could improve. A candidate who filled
two bubbles filled two bubbles; a reviewer cannot know which they meant, and the
marking scheme already says what a multiple is worth.

**Scoring still refuses to guess.** An answer the engine could not reduce to one
option is marked as `?` (`domain.scoring.MULTIPLE`) - never as the option it
nearly said, and never as a blank. That guard is
`services.scoring._scorable_answer`; see [`scoring.md`](scoring.md).

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

There are none here. Conflict detection reads the statuses and confidences the
decision layer computed from **the template's own** `ambiguity_margin` and
`min_confidence`. Calibrating a template in
[Phase 4](calibration_workflow.md) therefore moves the conflict queue with it,
and there is exactly one place where "how sure is sure enough" is configured.

---

## 3b. The session queue (revised phase 4)

Resolve shows the **scan session's** queue, opened from any of its batches:
every conflict of every sheet that counts, and the session's rejected / rescan
cases, deferred sheets and processed sheets. A decision on a sheet is recorded
in the batch that sheet was read into. A decision that changes a Student ID
re-runs the session's duplicate check once, so a correction can raise or clear
a duplicate with a sheet in another batch. Reopening the project rebuilds the
same queue from the database; Scan need not be visited. See
[ADR-0007](decisions/ADR-0007-session-effective-sheet-set.md).

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
6. **Pick** a value - a value button, its number key, `B` for blank, or the
   free-text box for a whole identifier. Picking does not record anything: the
   ring lands on the bubble you chose and the comparison strip fills in, so you
   can see that it landed where you meant before it becomes the value.
7. Give a **reason**. Confirming the machine's reading records *"Machine result
   visually confirmed"* automatically; a correction asks you to pick one, and
   "Other" requires an explanation.
8. **Confirm** (or `Enter`) records it. The button says which of three things
   it will do:

   | Button | When |
   | --- | --- |
   | **Confirm '5'** | you have picked a value |
   | **Confirm machine reading** | the machine's reading is a value this field can hold, and you have inspected it and agree |
   | **Choose a value first** (disabled) | the machine's reading is not one value this position can hold - see below |

   **Defer** postpones it; **Reopen** appears once something has been decided.
9. The comparison strip states all three values, always, and says **Pending**
   for a pick that has not been committed:

   ```text
   MACHINE      MANUAL      EFFECTIVE
   1-7          1           Pending
   ```

### Correcting a whole field at once

A candidate who leaves four positions of their roll number blank produces four
position conflicts. The operator usually knows the whole number - it is written
on the script - so making them visit four positions to type four digits, each
with its own reason, is work the interface was creating rather than work the
examination needed.

**Edit full \<field\>…** opens a one-line editor beside the value buttons,
prefilled with the field as it currently reads (`??00 29`, with `?` where no
value can yet be stated). Typing a value stages every position it would change
**on the sheet**, in the pending style, so the operator can see the number
landing on the bubbles they meant before anything is written. One **Apply**,
one reason, one note.

Everything about it is derived from the template - how many positions the field
has, and which symbols each one prints - so a five-digit identifier or a
two-position set code is validated against *its* field. A set code whose
options are `10`, `11`, `12` is split by symbol, not by character. Nothing is
padded or truncated: a value the field cannot hold is refused with a reason.

What it writes is **ordinary position corrections**, one per position, each
against its own conflict with its own audit event. The position model is
untouched; this is a faster way to reach it. Two consequences worth knowing:

- **Positions the machine read confidently are left alone.** Six digits typed
  against four disputed positions writes four corrections, not six.
- **A position nobody disputes cannot be overruled here.** A correction is
  recorded *against a conflict*, and a position the engine read confidently has
  none - there is nowhere to put the decision. The editor says so and names the
  position rather than storing an identifier different from the one typed. If
  such a digit is genuinely wrong, the sheet needs re-reading.

The editor is not offered where a typed field value could not be mapped
honestly: a duplicate student ID (which names no position and is already edited
whole in the free-text box), and a sheet that never registered, whose template
positions have no trustworthy association with the paper.

Every event of one edit carries a shared action token, so the history shows the
four corrections as one operator action - and **one `Ctrl+Z` takes the whole
edit back**, not one position of it. A position somebody has since decided
again is left alone rather than rolled back over their work.

### Why the machine's reading is not always offered

A roll-number position the engine read as `0-5` carries **two marks**. That is
not a value one printed digit can hold, and recording it as the human-decided
value of that position substitutes `0-5` into the identifier - producing a
candidate ID no roster will ever match, from a button that said the machine was
right.

So the interface does not offer it. The primary action is disabled, says
*Choose a value first*, and its tooltip explains that the normal resolution is
to choose one value, choose blank, or defer.

**Nothing in the backend changed.** `accept_machine_value` can still store any
reading, and a project that recorded one before this build is unaffected. What
changed is that the *interface* no longer presents an ambiguous reading as a
resolution. A reading is offered for confirmation when it is one of the symbols
the template prints for that group, or a blank - confirming "this position
really is empty" is a legitimate decision - or where the group has no fixed
alphabet at all, as for a duplicate student ID, whose value is a whole
identifier and is perfectly legible.

9. **History...** shows the complete provenance at any time.
10. With **auto-advance** on (the toolbar's tick, remembered in your own
    settings), resolving the current conflict selects **the next one on the
    same sheet**, and only moves to another sheet once this one has nothing
    left needing a decision. An operator holds one script; finishing it before
    being sent elsewhere is the point. Deferring does not advance - deferring
    is "come back to this".

### The queue is sheet by sheet

`list_conflicts` orders the queue **sheet-major**: the sheets in most trouble
first, and within each sheet its worst conflict first, then by field and
printed position. One sheet's conflicts are therefore one contiguous run, which
is what makes finishing a sheet possible and what the queue's grouping shows -
the file name is written once per sheet and its other rows carry a
continuation mark.

It did not used to be. Ordering by severity across the whole *batch* put a
roll-number column with two marks (severity 1) and an uncertain one (severity
0) hundreds of rows apart, so two problems on the same paper were nowhere near
each other and resolving one moved the selection to a different sheet. That was
the jumping.

Nothing is restored by row number either. Rows disappear as they are decided,
so an index means a different conflict after every rebuild; what is restored
is the conflict that was selected, then the sheet it was on, then the
scrollbar.

### What the preview shows

On this stage **amber means "the machine read this"** and **red means "a person
decided this"** - on the sheet and on the value buttons alike. The recognition
status palette the Scan page uses is turned off here, so a zone outlined red
because its status is `multiple` cannot put a third meaning on the same colour
inside the same rectangle. Every distinction is carried by line weight or style
as well, so none of it depends on telling amber from red.

| Outline | Meaning |
| --- | --- |
| **Amber, dashed**, round the whole 0-9 bubble stack | this printed position is waiting for a decision |
| **Amber, dashed ring** on a bubble | the engine read this mark on the paper |
| **Red, dashed** lane + heavy solid red ring | a value has been picked and **not yet recorded** |
| **Red, solid**, round the same stack | a person supplied or overrode the value here |
| **Heavy red ring** on one bubble inside a red lane | the value they chose |
| **`BLANK`** caption beside a red lane | they decided the position carries no mark |

The zoomed field frames the disputed position **with its neighbouring printed
positions**, because a faint mark is judged against the columns beside it -
filled by the same candidate, in the same pencil. The region is grown to the
pane's proportions before it is fitted, so a wide pane showing a tall column
spends its spare width on more of the sheet rather than on blank canvas. Any
manual zoom or pan takes the framing over; **Re-centre** hands it back.

The rectangle is the **printed position**, not a bubble: a roll-number column
is a stack of ten, and the question a reviewer is answering is "what is in this
column". A field with three doubtful positions shows three lanes, the one being
decided drawn more heavily than the rest, so you can see how much of the
identifier is in question before deciding any of it.

Red means *a human decided this*, not *this is wrong*.

The `?` glyph beside the bubble the engine nearly chose is **gone** from this
stage. It pointed at where the machine's doubt landed rather than at where your
attention has to go, and it is still on the Scan page, where skimming a whole
sheet for what went wrong is the actual task.

The lane's geometry comes from the bubbles recognition measured - the
template's own normalised coordinates projected onto the canonical page - so it
sits exactly where the engine looked, at any zoom, and follows a template that
is later edited. Nothing is drawn onto the scan; the original file is never
touched. A conflict that is **not** a position on the paper - a page that would
not rectify, a corrupt file, a curled corner - gets no rectangle, because
inventing one would put a confident outline somewhere arbitrary.

### Keyboard

The stage is usable without the mouse, because a sitting is hundreds of sheets.

| Key | Action |
| --- | --- |
| `0`-`9` | **Pick** the value that digit prints |
| `B` | Pick blank |
| `Enter` | **Confirm** what the strip says the effective value would be |
| `D` | Defer |
| `E` | Open the whole-field editor |
| `Esc` | Close it, discarding what was staged |
| Left / Right | Previous / next conflict |
| `Shift+Enter` | Previous unresolved conflict |
| `Ctrl+Enter` | Next unresolved conflict |
| `Ctrl+Z` | Undo the last decision |
| `Ctrl+Y` | Redo it |
| `Ctrl+Shift+Z` | Undo the last resolved sheet |

A key and its button are the same command: the digit keys call exactly what the
value buttons call, so a typed correction carries the same reviewer, the same
reason and the same audit event as a clicked one. There is no separate keyboard
state to drift. A pick is discarded whenever the selected conflict changes, so
it can never follow you onto another sheet.

Two things keep that from being the accidental destructive edit this stage
exists to prevent:

- a digit acts only when the active conflict genuinely prints that symbol - the
  labels come from the template, so a key this sheet has no answer to does
  nothing at all;
- every keyboard action refuses while the focus is in a text box, so searching
  for roll number `170501` cannot record `1` as somebody's student ID.

### Undo

| Command | What it takes back |
| --- | --- |
| **Undo decision** (`Ctrl+Z`) | the most recent decision anywhere in this batch |
| **Redo** (`Ctrl+Y`) | makes that decision again |
| **Undo resolved sheet** (`Ctrl+Shift+Z`) | every decision of the last sheet you finished |

Undo is batch-wide rather than "whatever is selected", because with
auto-advance on the conflict you have just decided is no longer the one on
screen. The page follows the undo to wherever it landed, so you see what
changed rather than being told that something did.

**Undo steps back one command; it is not a reset.** A position corrected by one
reviewer and then corrected by another returns to the first reviewer's value
and stays resolved. Only when no decision is left standing does it return to
the machine's value and to *Open*. That is the difference between **Undo** and
**Reopen**, which discards every standing decision at once.

None of it is a gesture on screen. A reversal is a persisted, attributed audit
event; the effective value and the conflict's state are re-derived from the
ledger; the counters, the queue and the overlay update immediately; downstream
results see the restored value; and closing and reopening the project shows the
undone state, not the decision. See [§6](#6-provenance-undo-and-what-reopen-means).

> **The two curved-arrow buttons in the previous build were *not* undo and
> redo.** They were bound to "previous conflict" and "next conflict", so the
> one control an operator would reach for to take a mistaken correction back
> moved the selection instead. They now mean what they look like, and
> navigation wears chevrons.

### Your name

Set it in **File > Settings > Reviewer**. It is remembered between sessions and
recorded against every decision. **A correction cannot be saved without one** -
the exit criterion for this phase is that a final value traces back to a
*named* correction, so an unnamed one is not allowed to exist.

It is a name, not an account. Phase 6 needs attribution, not authentication.

---

## 5. Conflict states

```text
        OPEN ──accept/correct──▶ RESOLVED ──reopen/undo──▶ OPEN
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

The queue names the state **in words with a glyph beside it**, and tints the
row as well - so the state survives a colour-vision deficiency and a grey
printout, where the tint alone would not. A conflict that is open *because
somebody put it back* reads **Reopened**: still `OPEN` in storage and in every
count, because inventing a sixth state would mean teaching every filter,
predicate and count about it, but a distinction a reviewer working a queue
needs to see. It is drawn from the ledger -
`review_store.ConflictRecord.reversed_before`.

Conflicts are **never deleted**. A withdrawn conflict is kept because the fact
that the machine once disputed this value is itself evidence.

**A machine may withdraw its own complaint; it may never erase a person's
decision.** A resolved or deferred conflict keeps its state and its history
whatever a later re-read says.

---

## 6. Provenance, undo, and what "reopen" means

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

### One command model, folded as a stack

Undo is an **event**, not an edit, because the ledger cannot be edited
([§7](#7-why-the-ledger-cannot-be-rewritten)). `review_store.standing_commands`
folds a conflict's history into the commands still in effect: a decision, a
deferral or a reopening **pushes**; an `UNDONE` event **pops**. Everything else
is derived from that single fold - the effective value
(`_standing_decision`), the cached state (`recompute_state`), what the next
`Ctrl+Z` would take back, and what the queue calls the row.

That is what makes undo step back one command rather than reset:

```text
14:32:10  Machine recognition          Value: A-C
14:35:22  Corrected by Dr. X           A-C → A
15:13:17  Corrected by Dr. Y           A   → C
15:14:02  Undone    by Dr. Y           C   → A     (Dr. X's decision stands again)
```

Effective value: `A`, by Dr. X, and the conflict is still *Resolved*. A second
`Ctrl+Z` would leave nothing standing and return it to `A-C` and *Open*.

**Redo has no event of its own.** Making a decision again is a decision, so it
is re-issued through the ordinary path and recorded as one, by whoever is
reviewing now, at the time they did it - rather than as a ledger entry claiming
a value had been "restored". The redo stack is therefore session-only: it is
discarded when the project closes, when a different project is opened, and as
soon as a new decision is made, which is what stops a stale redo putting a
value back onto a conflict somebody has since decided differently.

### Undoing a whole sheet

**A sheet's resolution session is a run in the ledger, and nothing extra is
stored to know it.** A reviewer works one sheet to the end and moves on, so the
session is the maximal run of consecutive human events in the batch that belong
to that sheet - the run ends exactly where they moved on. `Ctrl+Shift+Z` finds
the most recent such run whose sheet is *finished* (at least one conflict
resolved, none left unresolved), reverses every decision still standing in it
**in one transaction**, and navigates back to the sheet.

Two consequences that matter:

- **Decisions from an earlier sitting are left alone.** Undoing a sheet somebody
  finished this morning does not discard what was decided on it last week,
  because the session is a run rather than "everything on this sheet".
- **It is all or nothing.** A half-restored sheet is worse than one not restored
  at all - the counters would be right, the queue would look finished, and one
  conflict would silently still carry a decision nobody meant to keep.

Because the run is derived from the ledger, it survives closing the project,
and a second operator opening the same project sees the same answer.

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

### After a crash, a forced close or a restart (0.1.1 phase 3)

Because each decision is its own transaction, a decision that was confirmed is
kept whatever happens to the process afterwards - this was tested by killing a
real OMRFlow process from outside after several corrections
(`tests/crash/test_crash_matrix.py`, cases 5-9 and 15). On the next open:

- **Resolve opens on its own**, on the batch the later stages read (or the
  batch the Scan stage was interrupted on), without a visit to Scan or a click
  on *Review Conflicts*.
- Every committed decision is applied: the machine's reading, the correction,
  the effective value and the full history are as they were.
- The queue is **exactly** the unresolved conflicts - nothing decided comes
  back, nothing undecided is missing - and the summary reads e.g.
  *120 total · 73 unresolved · 47 resolved*, counted from the stored rows.
- Reopening repeatedly writes nothing: no conflict, decision or ledger row is
  added by a reopen.

The sheet's own conflicts are saved together with its recognition result
([ADR-0006](decisions/ADR-0006-crash-safe-scan-work-units.md)), so a sheet the
Scan stage counted as processed always has its conflicts in this queue; the
batch-wide duplicate-ID and set-code conflicts of an interrupted batch are
completed by the reopen, before Resolve shows the queue. What is **not** kept:
which row was selected, and the redo list, which lives in memory only (an
undone decision cannot be *redone* after a restart).

---

## 9. Export

Corrections reach the CSV through one place:
`review_store.sheet_resolutions`, which the export reads and nothing else
computes. Two columns carry the provenance:

| Column | Meaning |
| --- | --- |
| `value_source` | `human` when a named reviewer decided anything on this sheet, `machine` otherwise. |
| `unresolved_conflicts` | How many of that sheet's **identification** disputes are still waiting. An ambiguous answer counts zero here; it is in its own question column, as the sheet was marked. |

Exporting a batch with unresolved conflicts warns first, stating the count. It
does not block - an interim export is legitimate - but unresolved ambiguity
never leaves the application silently looking like finished data.

A batch whose only ambiguity is in its answers reports `0` unresolved
conflicts and proceeds without a warning. That is correct: there is nothing for
anybody to resolve, and the ambiguity itself is in the exported row.

The export reads the resolutions; it never writes to them, and the machine's
own values stay in the project database whatever the file says. A CSV can
always be regenerated.

---

## 10. Idempotence, and Phase 5

A conflict's identity is `(batch, scan, type, zone, group)`, enforced by a
unique constraint. Re-running a batch, resuming one, or retrying a failed sheet
therefore **updates** the existing conflicts rather than creating a second set.
Re-synchronising a sheet whose reading has not changed writes nothing at all -
including, since 0.1.1 phase 3, a sheet whose conflict the machine had already
withdrawn (an identity comparison used to re-withdraw it, with a fresh event,
on every re-sync).

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

## 12. Projects from an earlier build

A project scanned before answer ambiguity stopped being a conflict may hold
thousands of `answer_*` rows in `review_conflict`. Opening it in this build is
safe and needs no migration step:

- **Nothing is deleted or rewritten on load.** A stored observation is
  evidence, and a decision somebody recorded is still theirs.
- **They are excluded from every active read** - the queue, the batch and
  per-sheet counts, the scan page's badge, the export's
  `unresolved_conflicts`, the project health check, and the scoring block for
  unresolved answers. One filter, `review_store._resolution_only`, built from
  `ConflictType.requires_resolution`.
- **Decisions already made are still honoured.** A reviewer who corrected an
  answer under the old semantics still sees their value in the export and in
  scoring.
- **Re-reading a sheet tidies up.** Detection no longer produces them, so an
  untouched legacy answer conflict is *withdrawn* in the ordinary way - kept,
  with its history, and out of the queue.

The five `ConflictType` members are kept for exactly this reason: refusing to
name `"answer_multiple"` would make an old project unreadable rather than
merely out of date. They are listed in `domain.review.LEGACY_ANSWER_TYPES`.

---

## 13. What Phase 6 does not do

- It does not make recognition more accurate. It makes what recognition was
  unsure about *in a record's identity* visible and correctable.
- It does not verify that a correction is *right*. It records who made it, when
  and why, so that a wrong one can be found and reopened.
- It has been exercised against **synthetic sheets and the repository's single
  real sample**. No examination-scale review session with real operators has
  been run; see `development/PHASE_06_HANDOFF.md`.
