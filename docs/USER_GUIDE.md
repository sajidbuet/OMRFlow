# User guide

> **This guide describes only what OMRFlow can do today (version 0.1.0.dev0,
> Phases 0-8).** The application can create and open projects, design `.omrt`
> sheet templates visually, calibrate a template against real scans, read
> filled-in answer sheets in resumable batches, review everything the
> recognition engine was unsure about, reconcile the scripts against the
> candidate list, and calculate marks against a verified answer key. It cannot
> yet produce reports or export results. Everything in
> `development/ROADMAP.md` beyond Phase 8 is not available.
>
> **Do not use this build for examination processing.** Its recognition has been
> validated against one real printed sheet and variants of it, not against a
> corpus of genuinely filled papers. See the README's development status.

## Installing

1. Install Python 3.12 or newer.
2. Open a terminal in the OMRFlow folder and run:

   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -e .
   ```

## Starting OMRFlow

```bash
python -m omr_scanner
```

or, after installation, the `omrflow` command. You may pass a project folder to
open it immediately:

```bash
omrflow "C:/Exams/Physics Midterm 2026"
```

## The main window

The window has a workflow list on the left and a page for each stage on the
right. **1. Project**, **2. Template**, **3. Calibrate**, **4. Scan**,
**5. Resolve**, **6. Attendance**, **7. Answer Key** and **8. Results** do
something in this version; **9. Reports** states which development phase will
implement it and what that phase will provide - it is not broken, and it is not
hiding a setting you need to find.

The status bar shows the open project's name and folder, or "No project open".

## Designing a template

Select **2. Template** to open the designer. It works with or without an open
project (templates saved while no project is open must be placed manually);
with a project open, its `templates/` folder is offered by default.

1. **New from Image...**, choose a scanned or photographed reference sheet
   (PNG, JPEG, TIFF) and a name.
2. **Detect** to locate the four corner squares automatically, then drag any
   that were not found (or that need correcting) into place by hand. Confirm
   the automatically found ones with **Confirm**.
3. Drag the orientation mark (the short dash near the top-left) into place.
4. Use **Student ID**, **Set**, **Questions** or **Custom** to draw a region
   and configure it - each opens a small dialog for the details
   (digit count, answer choices, number of columns, and so on).
5. Adjust anything by dragging, resizing, or typing exact numbers in the
   properties panel; **Ctrl+Z**/**Ctrl+Y** undo and redo.
6. **Validate** checks the template for common problems before you save it.
7. **Save** (or **Save As**) writes the `.omrt` file. **Open...** reopens it
   later, restoring the reference image alongside your regions.

Saving a template inside the open project, or opening one of the project's own
templates, makes it **the project's template**: Calibrate and Scan pick it up
without being told, and it is still there the next time you open the project.
A template from outside the project is opened for you to look at and changes
nothing. If a project already contains exactly one template, opening the
project is enough - it is loaded on all three screens.

Full detail, including every keyboard shortcut, is in
`docs/template_designer.md`.

## Scanning answer sheets

Select **4. Scan** to read filled sheets against a template.

1. **Load Template...** - the `.omrt` the sheets were printed from. Already
   filled in when the project has a template; only needed to override it.
2. **Add Scan(s)...** for individual images, or **Add Folder...** to take every
   supported image in a folder (PNG, JPEG, TIFF, BMP; anything else is ignored).
3. **Process All**, or select rows and **Process Selected**. The window stays
   usable while it runs, shows `Completed 46 / 100`, and **Cancel** stops after
   the sheets in progress. The line above the buttons says how the next run will
   be shared out, for example `124 scans - 8 parallel workers`.
4. Click a row to review it: the corrected sheet with the recognition overlay in
   the middle, and every recognised field and answer on the right. Zoom, fit,
   100% and pan all work, and the overlay layers toggle independently.
5. Optionally tick **Rename processed scans using detected roll number** and
   choose an **Output Folder**. Recognised sheets are then *copied* there under
   their roll number - your originals are never moved or changed.
6. **Export CSV...** writes one row per sheet.

What to look for when reviewing:

- A sheet whose status is **Registration failed** produces no answers at all,
  by design; it is flagged rather than guessed at.
- An answer shown as `B-D` means *two* bubbles were filled. Both are kept.
- A `?` means the reading was too faint or too close to call.
- A roll number that did not fully resolve is **not** used as a file name; the
  sheet is filed as `UNRESOLVED_001` and the reason is stated in the list.
- Two sheets with the same roll number become `2103123.png` and
  `2103123_a.png`. **No scan is ever overwritten.**

Full detail - supported formats, the recognition conventions, the duplicate
naming rule, the CSV columns and the known limitations - is in
`docs/scan_workflow.md`.

## Settling which record a sheet is

Select **5. Resolve** — or press **Review Conflicts** on the Scan page — to
settle the sheets whose **student ID / roll number** or **set code**
recognition could not read, and the sheets it could not read at all. The Scan
page tells you how many there are when a batch finishes.

**Answers are not resolved here.** A question with two bubbles filled, or a mark
too faint to call, is a result rather than a question for you: it stays in the
results and the export as the sheet was marked (`B-D`, `?`, `B?`, or empty —
see above), it is scored as a multiple, and it does not hold the batch up. A
batch whose only ambiguity is in its answers shows **0 conflicts** here.

**First, put your name in File > Settings > Reviewer.** It is remembered between
sessions, and it is recorded against every decision you make. You cannot save a
correction without one.

The page has the queue on the left and the sheet on the right.

1. Narrow the queue if you like — by state (*Unresolved*, *Resolved*,
   *Deferred*), by kind of problem, or by typing a student ID or file name in
   the search box.
2. Click a conflict. Three views load:
   - **Zoomed field** — the disputed bubbles, magnified, with just that group
     circled. Decide from this one.
   - **Normalised sheet** — the whole corrected page, so you can see where on
     the sheet it sits.
   - **Original scan** — the file exactly as it arrived, in case the problem is
     with the scan rather than the reading.
3. Read **What the machine saw**. Alongside its reading it shows each option's
   *fill score* — how much of that bubble was covered in ink. These are
   measurements, not probabilities: a low score on the option you can plainly
   see marked usually means a light pencil or a tick rather than a fill.
4. Decide:
   - **Accept machine value** if you looked and it was right.
   - Click the **correct value** instead if it was not. For a whole roll number
     or a duplicate ID you type the value rather than picking one.
   - **Defer** to come back to it.
   - **Reopen** to change a decision — yours or somebody else's.
5. Give a **reason** for a correction. "Other" asks you to explain.
6. **Next unresolved** jumps to the next thing nobody has decided. ← and → step
   through, **Enter** accepts, **D** defers.

What to expect:

- **Your correction never erases what the machine read.** Both are kept. Press
  **History...** at any time to see the whole story: what was recognised, who
  changed it, when, and why.
- **Reopening a decision** puts the machine's value back as the current one, but
  the correction you are replacing stays in the history under the name of
  whoever made it.
- **Some conflicts have no value to pick.** A sheet that would not register, or
  a file that would not open, needs re-scanning — so those offer only *Defer*.
- **Duplicate roll numbers** are found across the whole batch. Each sheet's
  conflict names the others.
- Exporting with conflicts still open **warns you and says how many**. It does
  not stop you — an interim export is fine — and the CSV records the count per
  sheet either way, along with whether that sheet's values are the machine's or
  a person's.

Full detail — every kind of conflict, what the defaults are and why, and what
the audit record does and does not guarantee — is in `docs/conflict_review.md`.

## Checking the scripts against your candidate list

Select **6. Attendance** once you have processed a batch. This answers two
questions: does every script belong to somebody on your list, and did everybody
who sat the paper hand one in?

### Getting your list in

1. **Download Sample Template...** if you want to see the columns OMRFlow
   understands. Your own list does **not** have to look like it — you map the
   columns when you import.
2. **Import Candidate List...** and choose a `.csv` or `.xlsx` file. If the
   workbook has several sheets, pick the one with the candidates on it.
3. Check the preview, then the three dropdowns:
   - **Candidate ID** — required. The roll number column.
   - **Candidate Name** — optional.
   - **Marks / Attendance** — optional. See below.
4. Read the validation line: how many rows were read, how many candidates were
   accepted, how many are expected present and how many are marked absent.
5. **Import Candidates**.

**A marks column works as an attendance column.** If a candidate's marks cell
says **`ABSENT`** or **`ABS`** — in any capitalisation, with any spaces around
it — they are treated as absent. Anything else, including an empty cell, means
they were not marked absent. That is how a typical result sheet already works.

Words that merely *start* with "ABS", like `ABSENTEE` or `ABS123`, are **not**
treated as absences.

**Two things will stop an import**, and both need fixing in your file:

- the same roll number appearing twice — OMRFlow will not choose between two
  rows claiming the same person;
- a row with no roll number at all.

You will be told the roll number and the row numbers involved.

> **Roll numbers with leading zeros**: format that column in Excel as **Text**
> before saving. A cell holding `0015` as a *number* is just fifteen by the
> time any program reads it, and the zeros cannot be recovered.

### Working through the results

The table opens on the problems, because that is the work. Each row says what
is wrong in plain words:

| What you will see | What it means |
| --- | --- |
| **Matched** | Expected to attend, exactly one script. Nothing to do. |
| **Absent, confirmed** | Marked absent, no script. Nothing to do. |
| **Unknown candidate ID** | A script whose roll number is not on your list. |
| **Duplicate script** | Two or more scripts for one candidate. |
| **Present but no script found** | Expected to attend; nothing arrived. |
| **Marked absent but script found** | Marked absent, yet a script turned up. |
| **Candidate ID not yet resolved** | The roll number could not be read. Deal with it on the **Resolve** stage first. |

If a row has more than one problem, it says so — for example *Marked absent but
script found (+ Duplicate script)*.

Select a row to see the candidate, what your list said about them, and every
script attributed to them. Then decide:

- **Assign Script** — type the correct roll number for the selected script.
- **Set Script Aside** — for a sheet that was scanned twice. It stops counting,
  but **nothing is deleted**: the scan, its reading and your reason are all
  kept, and you can bring it back.
- **Override Attendance** — for a candidate your list has wrong.
- **Accept As-Is** — for a problem nothing can be done about, such as a script
  known to be lost.

Every decision needs a **reason**, and your name from
*File > Settings > Reviewer*. You cannot record one without a name.

What to expect:

- **Your list is never changed.** If you override an attendance, the row still
  shows what the file said alongside what you decided.
- **What the machine read is never changed.** If you assign a script to a
  different candidate, the roll number it was read as is still shown.
- **Fixing one thing can reveal another.** Assigning a script to a candidate
  who already has one creates a duplicate — and it says so immediately rather
  than letting you find out later.
- **History...** is in the detail panel: every decision, who made it, when, and
  why.

Full detail is in `docs/reconciliation.md`.

## Writing the answer key

Select **7. Answer Key**. Nothing can be marked until a key exists and somebody
has verified it.

The page works one **question-paper set** at a time. Type or pick the set code
in the **Set** box at the top — set codes are not limited to a single letter,
so `10` and `X1` are as valid as `A`. If your paper has no sets at all, the
recognised set code is blank and there is a single key for the whole paper.

Two ways to get the answers in:

- **Type or paste them** into the **Answers** box: one character per question,
  in question order, for example `ABCBCCADBDAC...`. Spaces, tabs, line breaks,
  commas, semicolons and vertical bars are ignored, so a column copied out of a
  spreadsheet pastes straight in. Anything *else* that is not a valid choice is
  reported to you rather than quietly dropped.
- **Read From Solution Sheet...** — fill a blank sheet in with the correct
  answers, scan it, and let the same recognition engine read it. **A sheet read
  this way is a draft, never a verified key.** Any question it read as blank or
  as a double mark is listed for you to fix by hand first.

The **Validation** box below tells you exactly what is wrong while you type: how
many characters it has against how many questions the template defines, and
which positions hold a character that is not one of that question's choices.
The table on the right shows the key question by question, so you can check it
against the printed paper without counting characters.

**Wrong questions (full credit for everyone)** takes question numbers —
`17, 64` — for questions withdrawn after the paper was sat. Every scored
candidate gets the full mark for those whatever they marked, including a blank
or a double mark, and no deduction is ever applied to them. They are flagged per
set: Set A and Set B need not agree.

Then:

1. **Save As New Revision** stores it as a **draft**. A stored revision is never
   edited — correcting a key creates the next revision, and the old one stays
   readable in the **revision** dropdown beside the set.
2. **Verify Answer Key** locks that revision and allows marking against it. It
   shows you the answers one last time and asks you to confirm. **Only a
   verified key produces marks**; a draft produces none.

**Put your name in File > Settings > Reviewer first.** A key cannot be verified
without one, and the name is recorded against the verification. The line under
the buttons tells you whose name will be used, in red if there is none.

Verifying a *new* revision of a set that already had one supersedes the old key
and tells you so — any results already calculated under it are marked as needing
recomputation rather than being silently left wrong.

## Calculating results

Select **8. Results** once the batch is processed, the candidate list is
imported and a key is verified.

### The rules

**Scoring Configuration...** opens the marking rules, which apply to the whole
paper:

- **Marks for correct answer** — and for any question flagged as a wrong
  question.
- **Blank answer mark** — what an unanswered question is worth. A blank is not a
  wrong answer: it never attracts the incorrect-answer deduction.
- **Negative marking** — *No negative marking*, *Fixed deduction per wrong
  answer*, *1 mark deducted per 3 wrong answers*, or *1 mark deducted per 4
  wrong answers*. Deductions are entered as positive amounts: `0.25` means minus
  a quarter mark.
- **Multiple answers: same as incorrect** — a question answered twice usually
  attracts the same deduction as one answered wrongly. Clear the box to set it
  separately.
- **Minimum total** — whether a total may fall below zero, and what the floor
  is.

The **Preview** panel works an example out under the rules as you set them, so
you can see what they do before anything is marked.

The bar at the top of the page always states the rules in force and which keys
are verified — *Revision 2 · Correct answer: +1.00 · Blank answer: 0.00 ·
Incorrect answer: -0.25 · ... · Verified answer keys: A rev 1, B rev 1*.

A rule that cannot change a mark — renaming, say — does not create a new
revision. One that can does, and every result already calculated under the old
one is flagged as needing recomputation.

### Marking

1. **Check Before Scoring** lists everything that will stop a candidate being
   marked, with the rules, before you commit to a run. Scoring will still run
   afterwards: a blocked candidate is *recorded* as "cannot be scored", with the
   reason, never skipped or given a zero.
2. **Calculate Results** marks the batch. The window stays usable while it runs.
   **Cancelling discards the whole run** — a half-marked batch under two
   different sets of rules, presenting itself as current, is worse than one that
   was never marked.

The table has one row per registered candidate: candidate, name, set, status,
score, the counts of correct/wrong/blank/multiple, the **key revision** that
produced the mark, and what needs attention. The dropdown above it filters to
*Needs attention*, *Scored*, *Absent* or *Cannot be scored*, and the search box
takes a candidate ID or name.

What the statuses mean:

| What you will see | What it means |
| --- | --- |
| **Scored** | Marked. The score and the key revision are shown. |
| **Absent** | Did not sit the paper. **No mark at all** — not a mark of zero, which would be indistinguishable from somebody who sat it and answered nothing. |
| **Cannot be scored** | Something needs a person. The row says what. |

Every reason a candidate cannot be scored names something you can go and fix:
*Reconciliation is not complete*, *No script was received*, *More than one
script, none nominated*, *Script belongs to no registered candidate*,
*Question-paper set not yet resolved*, *No question-paper set was read*, *No
verified answer key for this set*, *Answers do not match the key length*, *This
sheet has no stored recognition result*. None of them is ever resolved by
guessing.

An ambiguous *answer* is not among them. A question the engine could not reduce
to one option is marked as a multiple — never as the option it nearly said, and
never as a blank — so the candidate is scored under the same rule as anybody who
filled two bubbles.

Select a row for the detail: the total, the counts, the key and configuration
revision it was computed under, and the full question-by-question breakdown —
what the candidate marked, what the key says, and the mark that question earned.
**Review Sheet...** jumps to that candidate's sheet on the Resolve stage.

### When something changes underneath

A result never silently changes, and it is never patched. If you verify a new
key, change the marking rules, correct an answer on the Resolve stage, or
reassign a script on the Attendance stage, the affected rows say so — for
example *Needs recomputing because the scoring configuration has changed. The
mark below is what the earlier inputs produced.* The old mark stays visible
until you act, so nothing changes behind your back.

**Calculate Results** redoes the batch; **Recalculate This Candidate** redoes
one. Either way the engine is re-run from scratch against the stored inputs —
it never adjusts the old number.

Full detail — the arithmetic, the exact rules for blanks, doubles and withdrawn
questions, what is stored and what is recomputed — is in `docs/scoring.md`.

## Using more of your computer (or less)

**File > Settings > Processing** decides how many sheets OMRFlow reads at the
same time.

- **Automatic** (the default) picks a sensible number for your computer and
  leaves it room to stay responsive. Most people never need to change this.
- **Single core** reads one sheet at a time. Slower, but the gentlest on memory
  and the easiest to follow if you are investigating a problem.
- **Custom** lets you set the number yourself, up to the CPU threads your
  machine reports. The dialog shows both "Detected CPU threads" and how many
  workers the current setting will actually use.

Reading sheets in parallel never changes *what* is recognised, only how long it
takes. The results, the file names and the CSV come out in the same order
whichever setting you choose, and no scan is ever overwritten. Your choice is
remembered between sessions.

## Creating a project

1. **File > New Project...**, or the *Create project...* button on the Project
   page.
2. Choose the folder that will *contain* the project. OMRFlow creates a
   sub-folder inside it.
3. Type a project name, for example `Physics Midterm 2026`.

The name is also the folder name, so it cannot contain `< > : " / \ | ? *` or end
with a period. Names with those characters are refused with an explanation.

OMRFlow then creates:

```text
Physics Midterm 2026/
├── project.json          project identity and metadata
├── database.sqlite       working data store
├── templates/            OMR sheet templates (.omrt)
├── scans_original/       scanned sheets, never modified
├── scans_aligned/        normalised sheets, regenerable
├── answer_keys/          answer keys and solution sheets
├── candidate_lists/      imported candidate and attendance files
├── exports/              generated reports
└── logs/                 log of this project's processing
```

Most of these folders stay empty until the phase that uses them.

## Opening a project

- **File > Open Project...** and select the project folder (the one containing
  `project.json`), or
- **File > Open Recent** and pick from the last projects you used.

If the folder is not a project, or its files are damaged or were written by a
newer version of OMRFlow, you get a short explanation and nothing is changed.
A project that can no longer be opened is removed from the recent list.

## Closing

**File > Close Project** closes the project and releases its database file, so
the folder can be moved, copied or backed up safely. Closing the window does the
same.

## Where OMRFlow keeps your settings

Your preferences - log level, recent projects, the folder new projects start in,
the Processing mode and your reviewer name - live in `omrflow.config.json` in
your account's
application data folder
(`%APPDATA%\OMRFlow` on Windows). Deleting that file loses only preferences, not
project data.

The application log sits beside it under `%LOCALAPPDATA%\OMRFlow\logs`; each
project also keeps its own log in `<project>/logs/project.log`. Attach the
relevant log when reporting a problem - but check it first and remove anything
you are not permitted to share.

## Your data stays local

OMRFlow runs entirely on your computer. Candidate data, scans, answer keys and
results are not sent anywhere. Protecting that data according to your
institution's policies remains your responsibility.
