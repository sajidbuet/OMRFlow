# User guide

> **This guide describes only what OMRFlow can do today (version 0.1.0.dev0,
> Phases 0-7).** The application can create and open projects, design `.omrt`
> sheet templates visually, calibrate a template against real scans, read
> filled-in answer sheets in resumable batches, review everything the
> recognition engine was unsure about, and reconcile the scripts against the
> candidate list. It cannot yet calculate results or produce reports. Everything in
> `development/ROADMAP.md` beyond Phase 7 is not available.
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
right. **1. Project**, **2. Template**, **Calibrate**, **3. Scan**,
**4. Resolve** and **5. Attendance** do something in this version; the remaining
pages state which
development phase will implement them and what that phase will provide - they
are not broken, and they are not hiding a setting you need to find.

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

Full detail, including every keyboard shortcut, is in
`docs/template_designer.md`.

## Scanning answer sheets

Select **3. Scan** to read filled sheets against a template.

1. **Load Template...** - the `.omrt` the sheets were printed from.
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

## Reviewing what the machine was unsure about

Select **4. Resolve** — or press **Review Conflicts** on the Scan page — to look
at everything recognition could not decide. The Scan page tells you how many
there are when a batch finishes.

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
3. Read **What the machine saw**. Alongside its answer it shows each option's
   *fill score* — how much of that bubble was covered in ink. These are
   measurements, not probabilities: a low score on the option you can plainly
   see marked usually means a light pencil or a tick rather than a fill.
4. Decide:
   - **Accept machine value** if you looked and it was right.
   - Click the **correct answer** instead if it was not. For a whole roll number
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
- **Some conflicts have no answer to pick.** A sheet that would not register, or
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

Select **5. Attendance** once you have processed a batch. This answers two
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
