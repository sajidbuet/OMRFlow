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

1. **Pick** the value — a value button, its number key, `B` for blank, or type
   it in for a whole identifier. Nothing is saved yet: the ring lands on the
   bubble you chose so you can see it landed where you meant.
2. Choose a **reason**.
3. **Confirm** — or press `Enter`. The button tells you what it will record:

| Button | Meaning |
|---|---|
| **Confirm '5'** | Record the value you picked |
| **Confirm machine reading** | Recognition was right; record that you checked |
| **Choose a value first** *(greyed)* | The machine read two marks, which is not one value this position can hold — see below |
| **Defer** | Come back to it; it stays in the queue |
| **Reopen** | Appears once something has been decided |
| **Save value** | For a whole identifier you typed yourself |

The strip beneath states all three values, and says **Pending** until you
confirm, so a pick is never shown as if it had been saved:

```text
MACHINE      MANUAL      EFFECTIVE
1-7          1           Pending
```

**History…** shows every decision recorded against the sheet.

### Correcting a whole Student ID or Question Set at once

If a candidate left several positions of their Student ID blank, you do not
have to decide them one at a time.

1. Press **Edit full Student ID…** (or `E`). A one-line editor opens, prefilled
   with what the sheet currently reads — `??0029`, with `?` where nothing can
   be stated yet.
2. Type the whole number. As you type, every position it would change lights
   up on the sheet above, so you can see it landing on the right bubbles.
3. Choose a **reason** once, and press **Apply** (or `Enter`).

The same works for the **Question Set / Set Code**, including multi-position
and multi-character codes — the number of positions and the values allowed come
from your template, not from an assumption.

What OMRFlow records is still one ordinary correction per position, each
auditable on its own — this is a faster way to reach them, not a different
kind of decision. **One `Ctrl+Z` takes the whole entry back.**

Two things it deliberately will not do:

- It leaves alone the positions recognition read confidently, so typing six
  digits against four blanks records four corrections, not six.
- It will not overrule a position that **nobody is disputing**. If the value
  you type disagrees with a digit recognition read confidently, the editor
  names that position and refuses, rather than saving an ID different from the
  one you typed. If that digit really is wrong, the sheet needs re-reading.

It is not offered for a duplicate Student ID (type the whole ID in the box
instead) or for a sheet that failed registration, where there is no reliable
link between the template's positions and the paper.

### Finishing one sheet before the next

The queue is ordered **sheet by sheet**. When you resolve a conflict, OMRFlow
selects the next one **on the same sheet**, and only moves to a different sheet
once this one has nothing left needing a decision — so you can work a script
through to the end while you are holding it. The queue does not jump back to
the top, and your scroll position is kept.

The header counts each thing separately, for example:

```text
Conflict 2 of 5 on this sheet · 4 left here · 137 left in batch
```

### When the machine's reading is not offered

If recognition found **two marks** in one position — `0-5` — that is not a
value a single digit can hold. Recording it would put `0-5` into the student
ID and produce a number no candidate list will match. So the confirm button is
greyed and says *Choose a value first*: pick one of the values, pick **Blank**,
or **Defer**.

## Reading the preview

**Amber means the machine read it. Red means a person decided it.** That is
true of the sheet and of the value buttons alike.

| On the sheet | Meaning |
|---|---|
| **Amber dashed outline** around a whole 0–9 column | this position is waiting for you |
| **Amber dashed ring** on a bubble | recognition read this mark |
| **Red dashed outline** + heavy red ring | you have picked this — not saved yet |
| **Red solid outline** + heavy red ring | saved |
| **`BLANK`** beside a red outline | you decided the position carries no mark |

| On the buttons | Meaning |
|---|---|
| **Amber** | recognition read this symbol |
| **Red** | the value you have picked |

The outline is the **printed position**, not one bubble: a roll number column
is a stack of ten, and the question is "what is in this column". Every doubtful
position on the sheet is outlined, with the one you are deciding drawn more
heavily — so you can see how much of the identifier is in question before
deciding any of it.

The zoomed view frames the disputed column **together with its neighbours**, so
you can compare an uncertain mark against the ones the same candidate made with
the same pencil. Zoom or pan takes the view over; **Re-centre** frames it
again.

Red means *a person decided this*, not *this is wrong*. The outline is drawn on
the preview only; your scan file is never modified.

## Keyboard

| Key | Action |
|---|---|
| `0`–`9` | **Pick** the value that digit prints |
| `B` | Pick blank |
| `Enter` | **Confirm** — records what the strip says |
| `D` | Defer |
| `E` | Edit the whole field |
| `Esc` | Close the field editor |
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
