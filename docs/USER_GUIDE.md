# User guide

> **This guide describes only what OMRFlow can do today (version 0.1.0.dev0,
> Phases 0-2).** The application can create and open projects, and design
> `.omrt` sheet templates visually. It cannot yet read filled-in answer sheets,
> recognise marks, reconcile attendance, calculate results or produce reports.
> Everything in `development/ROADMAP.md` beyond Phase 2 is not available.
>
> Do not use this build for examination processing.

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
right. **1. Project**, **2. Template** and **3. Scan** do something in this
version; the other five pages state which development phase will implement them
and what that phase will provide - they are not broken, and they are not hiding
a setting you need to find.

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
   usable while it runs, and **Cancel** stops after the sheet in progress.
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

Your preferences - log level, recent projects, the folder new projects start in -
live in `omrflow.config.json` in your account's application data folder
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
