# Review and Resolution

The **Resolve** stage is where a person decides what recognition would not.

The principle: **a value OMRFlow is not confident about is never quietly
settled.** It is queued, shown with the evidence, and decided by a named
reviewer with a recorded reason — and the machine's original reading is kept
alongside the decision rather than replaced by it.

> The detailed reference is
> **[`docs/conflict_review.md`](https://github.com/sajidbuet/OMRflow/blob/main/docs/conflict_review.md)**.

## What reaches the queue

- **Missing marks** where an answer was expected
- **Multiple marks** — two or more bubbles filled
- **Low-confidence reads**, where the darkest mark was too close to the next
  darkest, or sat between the blank and marked thresholds
- Fields that could not be measured at all

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
conflicts. You *can* score with conflicts outstanding, but the marks will
reflect whatever the unresolved fields currently hold. Clear the queue first.

## Related

- [Recognition Symbols](Recognition-Symbols)
- [Answer Keys & Scoring](Answer-Keys-and-Scoring)
- `docs/conflict_review.md`
