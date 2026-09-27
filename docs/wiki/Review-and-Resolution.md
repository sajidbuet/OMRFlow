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
| a **value button** | You are choosing one of the symbols this sheet prints |
| **Save value** | You are typing the correct value yourself |
| **Defer** | You want to come back to it — it stays in the queue |
| **Reopen** | Re-opens something already decided |

A **reason** must be chosen. The panel then states all three values, so there
is never any doubt about which one is in force:

```text
Machine result:   (blank)
Manual decision:  1
Effective result: 1
```

**History…** shows every decision recorded against the sheet.

## Reading the preview

| Outline | Meaning |
|---|---|
| **Amber, dashed**, around a whole 0–9 bubble column | this printed position is waiting for you |
| **Red, solid**, around the same column | you supplied or overrode the value here |
| **Heavy red ring** on one bubble | the value you chose |
| **`BLANK`** beside a red outline | you decided the position carries no mark |

The outline is the **printed position**, not one bubble: a roll number column
is a stack of ten, and the question is "what is in this column". Every doubtful
position on the sheet is outlined, with the one you are deciding drawn more
heavily — so you can see how much of the identifier is in question before
deciding any of it.

Red means *a person decided this*, not *this is wrong*. The outline is drawn on
the preview only; your scan file is never modified.

## Keyboard

| Key | Action |
|---|---|
| `0`–`9` | Choose the value that digit prints |
| `B` | Choose blank |
| `Enter` | Accept the machine value |
| `D` | Defer |
| ← / → | Previous / next conflict |
| `Shift+Enter` / `Ctrl+Enter` | Previous / next **unresolved** conflict |
| `Ctrl+Z` / `Ctrl+Y` | Undo / redo the last decision |
| `Ctrl+Shift+Z` | Undo the last sheet you finished |

None of these fire while you are typing in **Search**, **Reason** or a value
box, so searching for a roll number cannot record a digit as somebody's ID. A
digit key does nothing unless the active position actually prints that symbol.

With **auto-advance** on (the tick in the toolbar, remembered between
sessions), deciding a conflict selects the next undecided one by itself, and
finishing a sheet carries you on to the next sheet that needs work. Deferring
never advances — deferring means "come back to this".

## Changing your mind

| Control | What it takes back |
|---|---|
| **Undo decision** (`Ctrl+Z`) | the last decision you made, wherever you made it |
| **Redo** (`Ctrl+Y`) | makes that decision again |
| **Undo resolved sheet** (`Ctrl+Shift+Z`) | every decision on the last sheet you finished, and takes you back to it |

Undo steps back **one** decision. If two people have decided the same position,
undoing the second restores the first, not the machine's reading; **Reopen**
is what discards every decision at once.

An undo changes the stored record, not just the screen: the counters, the
queue and the preview update at once, the results downstream follow, and
closing and reopening the project shows the undone state.

## The audit trail

Every decision is appended, never overwritten: what the machine read, what
the reviewer chose, who they were, when, and why. **Undoing is appended too** —
it never erases the decision it reverses, so a history reads *machine value →
decision → undone → decision*, and a value can always be traced back to the
person who decided it and to what it was before.

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
