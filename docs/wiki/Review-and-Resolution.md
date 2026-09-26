# Review and Resolution

The **Resolve** stage is where a person settles **who a sheet belongs to and
which paper it answers**, when recognition could not.

The principle: **a value OMRFlow is not confident about is never quietly
settled.** It is queued, shown with the evidence, and decided by a named
reviewer with a recorded reason — and the machine's original reading is kept
alongside the decision rather than replaced by it.

> The detailed reference is
> **[`docs/conflict_review.md`](https://github.com/sajidbuet/OMRFlow/blob/main/docs/conflict_review.md)**.

## What reaches the queue

Only ambiguity in a field that identifies the record:

- **Student ID / roll number** — blank, incomplete, a column with two marks, a
  low-confidence or unmeasurable column, or an ID two sheets share
- **Set code** — the same states, at any number of printed positions
- **The sheet itself** — it would not register, or the file would not decode

## What does *not* reach the queue

**Answers.** A question with two bubbles filled, a mark too faint to accept, a
group that could not be measured, or no mark at all is a *recognition result*,
not a conflict. It stays in the results and the export exactly as the sheet was
marked — `B`, `B-D`, `?`, `B?`, or empty — and it does not wait for anybody.

A batch whose only ambiguity is in its answers shows **zero conflicts** here
and proceeds normally. Nothing about those answers is lost; see
[Recognition Symbols](Recognition-Symbols) for what each value means.

## Before you can save a correction

Set your **reviewer name** in **Application menu → File → Settings**. A
correction is attributed to a person, so it cannot be saved anonymously.

## Working through it

Select a conflict. **What the machine saw** shows the original sheet, the
corrected sheet and a zoomed view of the field, with zoom, fit and re-centre
controls.

Under **Your decision**:

| Button | Use it when |
|---|---|
| **Accept machine value** | Recognition was right after all |
| **Save value** | You are entering the correct value yourself |
| **Defer** | You want to come back to it — it stays in the queue |
| **Reopen** | Re-opens something already decided |

A **reason** must be chosen. **History…** shows every decision recorded
against the sheet.

Use **next unresolved** to work through the queue without revisiting decided
items.

## The audit trail

Every decision is appended, never overwritten: what the machine read, what
the reviewer chose, who they were, when, and why. A value can always be
traced back to the person who decided it — and to what it was before.

This is also true of reprocessing: a sheet read twice keeps both readings.

## Resolve before scoring

The **Results** stage's **Check Before Scoring** reports unresolved
conflicts. A script whose set code is still unresolved **cannot** be marked —
marking it against another set's key would produce a plausible mark against the
wrong paper. Clear the queue first.

An ambiguous answer does not stop a script being marked. It is scored as a
multiple, which is what the marking rules say a question answered twice is
worth — never as the option it nearly said, and never as a blank.

## Related

- [Recognition Symbols](Recognition-Symbols)
- [Answer Keys & Scoring](Answer-Keys-and-Scoring)
- `docs/conflict_review.md`
