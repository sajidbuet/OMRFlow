# OMRFlow

### Smart Mark Checker — Open-Source OMR Examination Processing

**OMRFlow** is an open-source desktop application for designing OMR templates,
processing scanned answer sheets, resolving recognition conflicts, reconciling
candidate attendance, evaluating MCQ examinations and generating auditable
examination results.

The goal is a flexible, transparent, locally operated alternative to proprietary
OMR examination-processing systems:

**ordinary image scanner + configurable template + transparent recognition +
human verification + reproducible result processing**

![The OMRFlow main window: a compact branded header, the nine-stage workflow
navigator, and the Project dashboard with no project
open](docs/images/omrflow-main-window.png)

*The nine examination stages run left to right across the top. The navigator
reflows to two rows, then to a two-column grid, as the window narrows — see
[The application shell and navigation](#the-application-shell-and-navigation).*

---

## Development Status

> **Pre-release. Phases 0-2 of 11 are complete; Phases 3-10 are
> implemented and undergoing testing.**
> OMRFlow manages projects, rectifies a scanned sheet into its template's
> canonical page, has an interactive designer for building that template, can
> now read the marks on a filled-in sheet, name/export the results, and file
> the processed images by roll number, can calibrate a saved template
> against representative real scans before a batch is run, records every
> batch durably so an interrupted run resumes rather than restarts, and puts a
> named human in the loop wherever the machine was unsure — without ever losing
> what the machine saw. None of the Phase 3 recognition pipeline has been
> validated on more than one real printed sheet. **Do not use it for
> examination processing.**
>
> **Phase 3 v1 is architecturally stabilized but recognition accuracy remains
> under active validation pending a large real-world OMR dataset. Phase 4
> makes that validation safer and more systematic to perform on whatever real
> scans an operator has - it does not perform the validation itself. Phase 5
> makes a batch reliable and resumable; it says nothing about whether the
> values that batch produced are correct. Phase 6 makes what the machine was
> unsure about visible, correctable and traceable; it does not make the machine
> more accurate, and it does not verify that a human correction was right —
> only who made it, when, and why. Phase 7 reconciles the scripts against the
> registered candidates so that nothing can go missing quietly; it does not
> score anything, and it does not verify that a reconciliation decision was
> right either.**

OMRFlow is being developed incrementally, in the defined phases listed in
[`development/ROADMAP.md`](development/ROADMAP.md). Each phase is implemented,
covered with automated tests, and stabilised before the project moves toward a
production-ready release — but **implemented is not the same as complete**: a
phase is only marked complete once both its implementation *and* its required
validation are satisfied. That distinction matters most right now for Phase 3,
which is implemented and passing its automated test suite, but has not yet been
validated against a broad, real-world set of filled sheets.

| Phase | Description | Development | Testing | Status |
|---|---|---|---|---|
| 0 | Architecture & repository foundation | Complete | Complete | ✅ Complete |
| 1 | OMR geometry & alignment engine | Complete | Complete | ✅ Complete |
| 2 | Template data model & template designer core | Complete | Complete | ✅ Complete |
| 3 | Recognition Engine v1: bubble mapping, recognition, batch scanning, renaming & CSV export | Implemented & architecturally hardened | In progress | 🧪 Testing |
| 4 | Template calibration & validation | Implemented | In progress | 🧪 Testing |
| 5 | Batch scan processing pipeline (persistence, resume) | Implemented | In progress | 🧪 Testing |
| 6 | Conflict detection & human resolution | Implemented | In progress | 🧪 Testing |
| 7 | Candidate & attendance reconciliation | Implemented | In progress | 🧪 Testing |
| 8 | Answer-key & scoring engine | Implemented | In progress | 🧪 Testing |
| 9 | Result management & reporting | Implemented | In progress | 🧪 Testing |
| 10 | Integration, recovery & production hardening | Implemented | In progress | 🧪 Testing |
| 11 | Release, user documentation & packaging | Pending | Not started | ⏳ Pending |

Phase titles and descriptions for 6-11 are taken directly from
[`development/ROADMAP.md`](development/ROADMAP.md), which is the authoritative
plan; see that document for each phase's purpose, deliverables and exit
criteria.

### Examination sets: a multi-part enhancement

Work running alongside the numbered phases, to let one project describe an
examination that is divided into several sets (one per post, paper or
category). It is being delivered in parts, and only the first has been built:

| Part | Description | Development | Testing | Status |
|---|---|---|---|---|
| 1 | Project configuration: exam name, and a variable number of persistent, uniquely identified sets | Implemented | Implemented & tested | 🧪 Testing |
| 2 | Per-Set attendance: one attendance workbook per set, candidates scoped to their set | Implemented | Implemented & tested (synthetic) | 🧪 Testing |
| 2 | Set-aware report generation: each set's result built from its own attendance workbook | Implemented | Implemented & tested (synthetic) | 🧪 Testing |
| 2 | Rollwise result sheet from the attendance template | Implemented | Implemented & tested (synthetic) | 🧪 Testing |
| 2 | `meritwise` sheet copied from the completed rollwise sheet | Implemented | Implemented & tested (synthetic) | 🧪 Testing |

**Real-data validation is still pending for all of it.** Everything above is
verified against synthetic workbooks and synthetic examinations built by the
test suite — no real examination office's attendance file, and no real scanned
cohort, has been processed end to end. That is the difference between
"implemented and tested" and "complete", and it is why none of these rows says
Complete.

### Status legend

- ✅ **Complete** — implementation and its required testing are both finished.
- 🧪 **Testing** — implementation substantially complete; validation and
  stabilisation ongoing.
- 🚧 **In development** — active implementation.
- ⏳ **Pending** — not yet started.

### What "testing" means, level by level

"Tests pass" is four different claims, and the difference is the whole
distinction between a phase that is finished and one that merely runs. For the
phases currently in testing:

| Phase | Implementation complete | Automated tests complete | Synthetic-data validation complete | Real-sheet validation complete |
|---|---|---|---|---|
| 3 — Recognition engine | Yes | Yes | Yes | **No** — one real sheet (`examples/ECE-0000.png`) plus geometric variants of it |
| 4 — Calibration | Yes | Yes | Yes | **Partial** — exercised against the one real sheet; no corpus of deliberately miscalibrated templates |
| 5 — Batch pipeline | Yes | Yes | Yes | **Partial** — largest measured batch is 48 real scans; no examination-scale run |
| 6 — Conflict review | Yes | Yes | Yes | **No** — no review session with real operators on a real batch |
| 7 — Reconciliation | Yes | Yes | Yes | **No** — no reconciliation of a real cohort against a real roster |
| 8 — Scoring | Yes | Yes | Yes | **No** — no examination has been marked with it |
| 9 — Reporting | Yes | Yes | Yes | **No** — no real examination office's own workbook has been reported on, and PDF export has no real-LibreOffice verification in the build environment (not installed there) |

- **Implementation complete** — the code exists and does what the phase set out
  to do.
- **Automated tests complete** — the phase's own suite passes, *and* the
  earlier phases' suites still pass.
- **Synthetic-data validation complete** — exercised end-to-end against
  generated sheets with known ground truth.
- **Real-sheet validation complete** — exercised against a broad corpus of
  genuinely filled, independently scanned sheets. **No phase claims this yet**,
  and it is the single reason no phase after 2 is marked ✅.

### The application shell and navigation

*Implemented and tested; not yet validated against a real examination
office's workflow.* The window is four bands — a compact branded header, the
workflow navigator, the current stage's page, and a status footer — with no
left sidebar. The horizontal navigator replaced a fixed 190-pixel navigation
column, and that width now belongs to the pages, which matters most on the
Template, Calibrate, Scan and Resolve stages where sheet images are inspected
at zoom.

**The application menu.** *File*, *Tools* and *Help* are behind the header's
menu button rather than on a permanent menu row. The menus, their nesting
(*File > Open Recent*, *Tools > Developer / Testing*), their actions and
their keyboard shortcuts are unchanged — `Ctrl+N` and `Ctrl+O` still work, and
the developer and stress-test commands are still where they were, one level
deeper.

**The workflow navigator** shows all nine stages as connected chevrons, and
chooses one of four layouts from the width actually available, measured
against the labels in the font in use — not from screen-resolution
breakpoints:

| Layout | When | What it shows |
|---|---|---|
| One row | The nine chevrons fit | `1. Project → … → 9. Reports` |
| Two rows | They do not | `1`–`5`, then `6`–`9` |
| Two columns | Two rows do not fit | A row-major grid of tiles, `1 2 / 3 4 / …` |
| Scrolling strip | Even that does not fit | All nine at full size, scrolled |

The font is never reduced and no label is ever clipped to make a layout fit;
a larger Windows text size changes the *layout* instead. No stage is ever
hidden. On a 1080p display at 100% scaling the one-row layout is used down to
about a 1180-pixel window; the two-column layout appears at the window's
720-pixel minimum once Windows text scaling reaches 150%.

**The Project page** is a dashboard: the project (or a "no project is open"
panel with Create and Open) beside a narrower column holding *Getting
Started* and *Recent Projects*. It stacks into one column when the two
columns can no longer both be read — decided from its own width, independently
of the navigator, so the two never share a breakpoint. Recent Projects is
backed by the same list as *File > Open Recent*; a project that has been
moved or deleted is listed, disabled, and says so.

**Accessibility.** Every control in the shell is keyboard reachable with a
visible focus indicator; the navigator additionally supports the arrow keys,
Home and End. No state is carried by colour alone — the active stage is also
bold, a locked stage is also dashed and explains itself in its tooltip, and
the footer's status dot is a redundant accent on a word. Text contrast is
verified against WCAG AA by `tests/unit/test_theme_tokens.py` rather than
asserted by eye.

**The visual system** is one accent (`#AC1F24`) on a white and neutral-grey
ground, with every colour, spacing step, radius and type size named once in
`omr_scanner.gui.theme.tokens` — a test fails if a hex literal is typed into a
stylesheet.

### What works today

- Create, close, reopen and validate a project (a folder with a metadata file,
  a SQLite database and the standard working sub-directories); invalid or
  damaged projects are refused with a readable message.
- **Project configuration — examination name and sets** (*implemented and
  tested*): *File > Project Configuration...* describes what the project is
  processing. See "Describing the examination and its sets" below.
- Load, validate and save versioned `.omrt` template documents.
- **Geometric normalisation** (Phase 1): detect the four printed registration
  markers on a scan, resolve which way up the page is, and correct rotation,
  translation, scale, skew and perspective into the canonical page the
  template declares.
- **Interactive template designer** (Phase 2): load a reference sheet image,
  detect and adjust its registration markers, draw student ID / question-set /
  question / custom-bubble regions with generated bubble grids, fine-tune
  individual bubbles, undo/redo, validate and save. See
  `docs/template_designer.md`.
- **Scan / recognition workflow** (Phase 3, *implemented, testing in
  progress*): load a template, import one scan or a whole folder of them,
  process them in the background without freezing the GUI, and review the
  result. Per the automated test suite and the recorded validation run in
  `development/PHASE_03_HANDOFF.md`, this currently includes:
  - orientation detection and marker-based registration, with a scan that
    cannot be confidently registered flagged rather than guessed at;
  - template-to-scan coordinate mapping, so recognition follows the template's
    own regions rather than hard-coded page positions;
  - roll/ID (numeric), set-code and question-block recognition;
  - explicit handling of blank and multiple marks (e.g. `B-D`) and uncertain
    reads, shown in the GUI and carried into the export rather than silently
    resolved;
  - a recognition preview/overlay over the corrected sheet;
  - **configurable multicore batch recognition**: several independent sheets
    read concurrently, one whole page per CPU worker, with the processing mode
    (**Automatic**, **Single core**, **Custom**) chosen in *File > Settings >
    Processing* and remembered between sessions. Results, output file names and
    CSV row order are identical to a single-core run on any number of cores;
  - **large-batch progress tracking**: a progress bar driven by finished
    sheets, completed/total counts and percentage, elapsed time, a smoothed
    estimate of the time remaining, live throughput, per-outcome tallies
    (successful / needs review / failed) and responsive cancellation — all
    from a fixed-size panel that behaves the same for ten sheets or ten
    thousand;
  - deterministic CSV export;
  - optional renaming of processed scans using the detected roll number, with
    safe duplicate-roll handling (`2103123.jpg`, `2103123_a.jpg`,
    `2103123_b.jpg`, ...) and unresolved/uncertain roll numbers left
    unrenamed rather than misnamed;
  - per-sheet error isolation, so one bad file does not abort a batch.
- **Template calibration & validation** (Phase 4, *implemented, testing in
  progress*): load a saved template, add one or more representative real
  scans, and run them through Phase 3's own registration and recognition in a
  diagnostic mode that shows every intermediate measurement rather than a
  second, separate calculation:
  - marker, registration and bubble-geometry overlays drawn from the exact
    coordinates the recognition engine itself computed - asserted equal by
    test, not merely eyeballed, with the **sampled** ellipse and the
    **printed** bubble drawn as separate, separately-labelled layers because
    they are genuinely different regions;
  - registered-page and original-scan views, with overlays deliberately
    restricted to the former (the only frame their coordinates apply to);
  - click-to-inspect on any bubble (field, question, sampling window, fill
    score, ink threshold, active threshold, classification), and per-position
    Student ID / Set Code diagnostics that never assume a single character;
  - the four recognition thresholds (`fill_ratio_threshold`,
    `blank_ratio_threshold`, `ambiguity_margin`, `min_confidence`) adjustable
    by slider or exact value, reclassifying and updating the overlay and
    quality summary immediately, without repeating registration;
  - non-destructive calibration: a working value separate from the template's
    saved value, applied only on an explicit *Save to Template*;
  - a four-state validation verdict per scan and per sample - **Validation
    Passed**, **Validation Passed With Warnings**, **Needs Review**,
    **Calibration Failed** - and a template whose registration markers no
    longer match the scan is reported `Calibration Failed`, with no answers
    at all, rather than a plausible wrong result;
  - the Scan page shows a non-blocking notice when the loaded template has
    never been calibrated, or has been edited since it last was.

  Calibrating against representative scans is **not** the same as validating
  Phase 3's real-world accuracy at scale; see
  [`development/PHASE_04_HANDOFF.md`](development/PHASE_04_HANDOFF.md) and
  [`docs/calibration_workflow.md`](docs/calibration_workflow.md).
- **Durable, resumable batches** (Phase 5, *implemented, testing in progress*):
  with a project open, every scan's result is written to the project database
  as it finishes, so a run that is cancelled, closed or killed is **resumed
  rather than restarted**:
  - one durable row per scan, carrying its status, attempt count, recognised
    roll and set code, output name, failure reason and category, and the full
    recognition result;
  - **Resume Batch** processes only what is left; sheets already read are never
    read again. **Retry Failed** re-reads the failures and only those;
  - a batch that was interrupted mid-run is repaired when the project is
    reopened — scans left mid-flight return to *pending*, never to *failed*,
    so a resume cannot silently skip exactly the sheets the crash caught;
  - resuming with an edited template or retuned thresholds says precisely what
    changed and asks, rather than silently mixing results produced under two
    different sets of rules;
  - closing the window mid-batch warns, stops the run, waits for it, and leaves
    the batch resumable;
  - a filter (*Completed / Needs review / Failed / Not processed*) for reviewing
    what happened;
  - a failure to *store* results is reported separately from a failure to
    *read* a sheet: a run whose results could not be written is never presented
    as a clean success.
- **Conflict detection & human review** (Phase 6, *implemented, testing in
  progress*): when a batch finishes, everything recognition was unsure about
  becomes a reviewable queue, and every decision a person makes is recorded
  against their name — **without the machine's own reading ever being
  overwritten**:
  - conflicts detected from the engine's **own** `needs_review` judgement and
    the **template's own** thresholds, so calibrating a template in Phase 4
    moves the queue with it and there is one place where "sure enough" is
    configured — no second opinion, no new numbers;
  - a taxonomy covering the identifier (blank, incomplete, multiple, uncertain,
    unreadable, low confidence, **and duplicates across the whole batch**), the
    set code, questions, and the sheet itself (registration failed, orientation
    assumed, unreadable file, processing error), with a **processing failure
    distinguished from an ambiguous value** — a corrupt file offers no answer
    buttons, because `A`/`B`/`C`/`D` is not an answer to it;
  - a **review workspace** showing the disputed bubbles zoomed, the whole
    normalised sheet, and **the original scan** — the last with the field
    located on it through the recognition engine's own inverse homography, not
    a second calculation in the GUI, and never by drawing on the file;
  - what the machine saw, its status, its decision score, and each option's
    measured **fill score** — labelled as a coverage measurement, never as a
    "probability";
  - **Accept**, **Correct**, **Defer** and **Reopen**, each requiring a named
    reviewer (*File > Settings > Reviewer*) and each correction a reason;
  - **an append-only audit ledger** enforced three ways — at the service
    surface, in the application, and by SQLite triggers that abort any `UPDATE`
    or `DELETE` on it outright;
  - a final value that is **projected** from the machine's reading plus the
    ordered events rather than stored, so reopening a decision restores the
    machine's value while keeping the superseded correction, its reviewer, its
    reason and its timestamp in the record;
  - CSV export carrying `value_source` (`machine` / `human`) and
    `unresolved_conflicts` per sheet, with a warning — not a block — before
    exporting a batch that still has conflicts open;
  - **special machine values are preserved**: a double mark exported as `B-D`
    is still `B-D` in the ledger after a reviewer decides it meant `B`.

  Phase 6 does **not** make recognition more accurate, and does not verify that
  a correction was correct. See
  [`docs/conflict_review.md`](docs/conflict_review.md) and
  [`development/PHASE_06_HANDOFF.md`](development/PHASE_06_HANDOFF.md).
- **Candidate & attendance reconciliation** (Phase 7, *implemented, testing in
  progress*): import the candidate list an examination office already holds,
  match the scanned scripts against it, and deal with every discrepancy
  explicitly — **nothing is ever dropped, merged or silently chosen between**:
  - import from **CSV or Excel (.xlsx)**, with a worksheet chooser, a preview of
    the real file and a column mapping the operator confirms. Only
    **Candidate ID** is required; **Name** and **Marks / Attendance** are
    optional;
  - a marks column doubles as attendance: **`ABSENT` or `ABS`** in any case,
    with any spacing, means absent; **anything else, including a blank cell,**
    means not marked absent. Compared as whole tokens, so `ABSENTEE`,
    `ABSENCE` and `ABS123` are not absences, and the column name is never
    hard-coded (`Total (90)` matches because `total` does);
  - **candidate IDs are treated as identifiers, not numbers**: an Excel cell
    holding `15000001` imports as `"15000001"`, never `"15000001.0"`, and a
    non-integral value is left alone rather than rounded — because rounding is
    how two candidates become one;
  - **it refuses to guess.** A file with two columns that equally name a
    candidate ID asks which; a repeated candidate ID **stops** the import with
    the ID and both row numbers, because the file states two different facts
    about one person; a failed import leaves nothing behind;
  - **Download Sample Template…** hands the operator a packaged example
    workbook containing placeholder data only;
  - reconciliation classifies every candidate and every script: **Matched**,
    **Absent confirmed**, **Unknown candidate ID**, **Duplicate script**,
    **Present but no script found**, **Marked absent but script found**, and
    **Candidate ID not yet resolved** — the last kept distinct so an unread
    roll number is never reported as an unknown candidate;
  - **conditions that co-occur are both shown.** A candidate marked absent who
    has two scripts carries both facts, in the table and in the detail;
  - resolution that never destroys: assign a script to the right candidate,
    set an accidental re-scan **aside** (kept, with its reason and its audit
    trail — never deleted), nominate the working script, override attendance,
    or accept an exception as-is. Every action needs a named operator and a
    reason, and **re-runs reconciliation immediately so a cascading duplicate
    is surfaced rather than discovered later**;
  - the **imported** value, the **machine** value and the **human** decision
    stay independently visible; overriding an attendance does not change what
    the roster said, and assigning a script does not change what the engine
    read;
  - **no candidate name, ID or mark reaches an application log** (see
    [Data privacy](#data-privacy-and-examination-integrity)).

  See [`docs/reconciliation.md`](docs/reconciliation.md) and
  [`development/PHASE_07_HANDOFF.md`](development/PHASE_07_HANDOFF.md).
- **Answer keys & scoring** (Phase 8, *implemented, testing in progress*):
  turn recognised answers into marks that can be defended — **reproducible from
  stored inputs, traceable to the exact key that produced them, and recomputed
  rather than patched when a rule changes**:
  - a **canonical answer string**, one character per question in question
    order, using the template's own option labels plus `_` for a blank and `?`
    for a **confirmed** multiple. Question *N* is character *N*, always: the
    string is never compressed and a blank never removed;
  - an **unresolved reading is not a `?`**. A sheet still in the Phase 6 queue
    **blocks** scoring, naming the question, rather than being marked as a
    blank or a multiple somebody has not actually looked at;
  - one **independent answer key per question-paper set**, typed, pasted, or
    read from a scanned solution sheet through the *existing* recognition
    engine. Set codes are not assumed to be one character;
  - **validation that names the question**: *"The answer key contains 98
    answer(s), but this template contains 100 questions. Please add answers for
    Questions 99-100."* Every problem is reported at once, and a stray
    character is reported rather than silently dropped;
  - **verification before scoring.** A key is a draft until a named person
    checks it — including one read off a solution sheet, because recognition
    completing does not make a key right. Only a verified key produces marks;
  - **revisions.** A verified key is never edited: correcting it creates the
    next revision and supersedes the old one, which is **kept**, because
    results point at it. Every result records **the exact revision used**;
  - **wrong questions**, flagged independently per set. Every scored candidate
    receives full credit whatever they marked — right answer, wrong answer,
    multiple or blank — and **no deduction is ever applied**. An absent
    candidate stays absent;
  - **four negative-marking modes**: none, a fixed deduction, 1 mark per 3
    wrong, and 1 mark per 4 wrong. Fractional penalties are **never
    truncated** — one wrong answer under the 1-per-3 rule costs exactly `1/3`;
  - **exact arithmetic.** Every mark is a rational, never a float, and rounding
    happens once, at the end, for display. A `-1/3` policy scores the same
    whether or not a label is narrow;
  - a configurable **minimum total**, recorded in the policy rather than
    hard-coded, so a result can say whether it was clamped;
  - an **absent candidate gets no mark**, not a zero — zero would be
    indistinguishable from somebody who sat the paper and answered nothing;
  - a **pre-scoring check** listing every candidate who cannot be marked, and
    why, all at once;
  - **stale results.** Changing a key, a rule, an answer, a set or a
    reconciliation makes affected results stale. They keep their mark — a true
    record of what the earlier inputs produced — and say so, and recalculating
    runs the whole scorer again from the stored inputs. **Nothing adds a delta
    to an existing mark**, which a test asserts by corrupting a stored score;
  - a **question-by-question breakdown** showing what the machine read, what
    was scored, the key, the evaluation and the exact contribution.

  See [`docs/scoring.md`](docs/scoring.md) and
  [`development/PHASE_08_HANDOFF.md`](development/PHASE_08_HANDOFF.md).
- A PySide6 application shell with the nine workflow stages; **Project**,
  **Template**, **Calibrate**, **Scan**, **Resolve**, **Attendance**, **Answer
  Key** and **Results** are implemented, and **Reports** states which phase
  will implement it.

### Describing the examination and its sets

*File > Project Configuration...* is where a project says what it is
processing. It opens automatically once a new project has been created, and
can be reopened at any time to change any of it.

**Exam name.** The title of the examination, as it should read on a report —
for example *Recruitment Exam, Bangladesh Submarine Cable Regulatory
Authority*. This is **not** the project's folder name, and is deliberately not
restricted to characters a folder name allows: a title containing `:` or `/`
is perfectly ordinary and is accepted. The folder name is asked for once, when
the project is created, and is shown beside the exam name so the two are never
confused. Leading and trailing whitespace is trimmed; a blank name is refused.

**Sets.** An examination is often divided into sets — one per post, paper or
category — and a project may define **any number** of them, from one to fifty
or more. Each set has:

| | |
|---|---|
| **Set code** | What is printed on the paper and shown as *Set 10*. Codes need not be sequential or numeric: `1`, `2`, `10`, `11`, `A`, `B`, `EEE-01` are all accepted. Whitespace is trimmed; a blank code is refused; **a duplicate code is refused with a message naming the set that already uses it, and never silently overwrites it**. |
| **Description** | Free text explaining what the set is, e.g. *Name of Post: Assistant Engineer (Electrical)*. May be long, and is otherwise unrestricted. |

Sets can be added, edited, deleted and reordered, and every change is written
to the project database as it is made — there is no separate save step, and
nothing is held only in the dialog. An example project therefore reads:

```text
Exam name:  Recruitment Exam, Bangladesh Submarine Cable Regulatory Authority

Set  Description
10   Name of Post: Assistant Engineer (Electrical)
11   Name of Post: Assistant Engineer (Civil)
12   Name of Post: Assistant Engineer (Mechanical)
```

**Each set also carries a stable internal identifier**, generated once and
never reused — not the row's position in the table, and not the code, which an
operator may legitimately correct later. That identifier is what the later
parts of this enhancement will link attendance, candidates and results to, so
that fixing a typo in a set code cannot detach the data already filed under it.

**Existing projects are unaffected and open normally.** One created before
this feature simply starts with no sets defined and shows its project name as
the exam name until a fuller title is entered. Nothing is invented from its
existing data: if the project already mentions set codes in its answer keys or
scanned sheets, the dialog *offers* to add those codes — without descriptions,
because the old data records that a set was processed, never what it was for —
and adding them is the operator's decision. A set whose papers were never
scanned cannot appear in that list, which is precisely why it is a suggestion
and not a migration. See [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md), "Schema
version 8", for exactly what happens.

### One attendance workbook per Set, and the result built on it

Each Set gets **its own attendance file**, assigned in the *Attendance* stage.
Set 10's `set10_attendance.xlsx` and Set 11's `set11_attendance.xlsx` are
independent: different candidate lists, different headings, different
formatting. **One Set's workbook is never used for another Set's result** — a
Set with no attendance file assigned stops with
*"No attendance/template workbook has been assigned to Set 11."* rather than
borrowing its neighbour's, falling back to the most recently imported file, or
quietly producing a generic report.

**The attendance workbook is also the result template.** When the file is an
`.xlsx`, it becomes that Set's layout source: generating the result copies the
workbook and writes marks into the office's own sheet, so the institution
name, the post, the column widths, the borders, the print setup and the logo
all come through. What is preserved has been measured rather than assumed —
see "What the generated workbook preserves" below.

**Rollwise** keeps every registered candidate, absentees included; an
absentee's mark cell reads the project's existing absence marker rather than a
fabricated number, and their rank cell shows the absence marker too.

**`meritwise`** is a *copy of the completed rollwise sheet* — not a freshly
built table — with absentees removed and the remaining rows sorted by merit.
It therefore keeps the same heading, post details, logo and print setup as the
roll list. Ranking stays the `RANK.EQ` formula the project already used, now
over the meritwise sheet's own row range, so tied candidates still share a
rank; row order breaks ties by ascending roll purely so Excel has an order to
print, never to imply a difference in merit.

**CSV attendance is still fully supported for reconciliation** — but a CSV has
no formatting to build a result on, so a Set whose attendance came from a CSV
needs an `.xlsx` result template chosen separately in the *Reports* stage
before its result can be generated. That is stated explicitly rather than
silently substituting a generic layout.

**Candidates are scoped to their Set.** Roll `10001` may exist in Set 10 and
Set 11 as two unrelated candidates; a mark from one can never populate the
other. The relationship key is the Set's stable internal id, not its code and
not a row position.

### What the generated workbook preserves

Measured against openpyxl with a workbook built to contain every feature, and
asserted in `tests/unit/test_meritwise_workbook.py` — not claimed:

| Preserved | Notes |
|---|---|
| Merged cells, fonts, fills, borders, number formats, alignment | Including on the `meritwise` copy |
| Column widths, row heights | Row heights travel with the row when it moves |
| Page orientation, paper size, scaling, margins | |
| Freeze panes, print titles, print area, headers/footers | Restored explicitly on the copy — `copy_worksheet` drops them |
| Static text, headings, and formulas unrelated to result fields | |
| Logos/images | Requires Pillow, which is now a dependency **because** openpyxl silently *destroys* images on save without it |

Known limitation: a merged cell that spans several **candidate rows** cannot
survive those rows being reordered on `meritwise`. That is reported as a
warning on the generation rather than silently mangled; merges in heading rows
(the usual case) are unaffected.

### Phase 3 architectural hardening

Phase 3 was subsequently hardened into a **replaceable recognition
subsystem**, so that Phases 4 and 5 can be built against it now and a future
Recognition Engine v2 can replace it without rewriting them:

- **Recognition API established.** One entry point -
  `RecognitionEngine.process(image, template)` - behind which no caller needs
  to know about thresholds, contours or homographies. The older
  `recognise_scan()` function remains supported.
- **Structured `RecognitionResult`.** Plain, versioned, JSON-serialisable data:
  the values, machine-readable status codes, the engine and template identity,
  scan-quality metrics, per-stage timings, and the **raw per-bubble
  measurements** behind every decision - so a future recalibration can re-score
  a batch without re-reading a single image.
- **Measurement separated from decision**, with every threshold still owned by
  the template.
- **Diagnostics.** Optional staged debug images and a headless annotated
  overlay per sheet, switchable from *File > Settings > Diagnostics* or the
  command line, and off by default.
- **Headless operation.** `python -m omr_scanner.tools.recognise` reads a scan
  or a folder with no GUI at all — asserted by a test that runs recognition in
  an interpreter where Qt was never imported.
- **Synthetic test generator.** Reproducible labelled datasets rendered from a
  real template, with controlled mark styles, geometry, exposure and structural
  damage, and ground truth written beside every image.
- **Benchmark framework.** Scores the engine against ground truth, classifies
  every disagreement by kind, writes machine-readable reports and compares a
  run against a stored baseline.
- **Regression fixtures.** Eleven stored recognition results covering the
  scenarios later phases must handle, usable with no engine present.
- **Multicore-ready processing** (see above), with the worker pool reading one
  whole page per process.
- **Real-dataset validation pending.** Everything above is infrastructure;
  accuracy is still a Phase 3 open question.

### Large-batch progress (Phase 3)

The batch architecture is built for examination-scale runs — **10,000+ OMR
scripts in a single batch** — and the Scan page reports on one without
changing shape as it grows:

```text
Processing OMR scans...
████████████████████░░░░░░░░░░░░  63.4%
6,342 / 10,000 processed
Elapsed 00:18:42 · Remaining ~00:10:47
Speed 5.7 scans/sec · 12 workers · Finish ~15:42
Successful 6,301 · Review 28 · Failed 13
```

- **Real-time completed / total count** and percentage, driven by *finished*
  sheets — a scan that fails to read still advances the bar, so a batch full
  of damaged files cannot stall it.
- **Elapsed time** on a monotonic clock, so a system clock change cannot
  corrupt it.
- **Smoothed ETA** from recent measured throughput (an exponential moving
  average), shown as `Calculating...` until there is enough evidence — never
  an absurd estimate from the first sheet, and never presented as exact.
- **Processing throughput**, and an estimated finishing time once the estimate
  is stable.
- **Success / needs-review / failure counters** that reconcile with the
  processed count.
- **Multicore-safe progress reporting**: worker processes never touch a
  widget; completions are counted centrally, in one place, under one lock.
- **Responsive cancellation**: the button disables itself at once, no new
  sheet is started, sheets already in a worker finish cleanly rather than
  being killed mid-write, and everything already read is kept.

The interface is fixed-size — one progress bar and five labels whatever the
batch length, repainted about five times a second rather than once per sheet —
and a batch queues file paths, never images. Verified by a headless
10,000-job simulation and by real batches of a few dozen sheets; **no
10,000-scan real-world run has been timed**, so no performance limit is
claimed.

Details: [`docs/recognition_engine.md`](docs/recognition_engine.md).

### Developer testing tools (Phase 3)

*Tools > Developer / Testing* puts the two things recognition work needs inside
the application: a labelled dataset to test against, and a score for what the
engine did with it. Both are also available from the command line, and both run
entirely on the local machine — nothing is uploaded, and no analytics are
collected.

**Generate Synthetic Test Dataset** renders a dataset from a real `.omrt`
template, at roughly **150 DPI derived from the template's physical page size**
(A4 → 1240 × 1754 px), as PNG or JPEG:

```text
synthetic_dataset/
├── images/            SYN_000001.png ...
├── ground_truth/      SYN_000001.json ...   (what was actually marked)
├── manifest.json      template, profile, seed, DPI, format — how to regenerate
├── manifest.csv       one row per sheet, with its test-case tags
└── dataset_summary.json  how many sheets of each kind
```

Nothing about the sheet is hard-coded: identifier length, symbol set, option
labels, question count, bubble size and page dimensions all come from the
template, so a nine-digit alphanumeric identifier with six options generates
correctly without a code change. Identifiers are fictional by construction,
derived from the sheet index.

Sheets are **named test cases**, not random noise, and each carries tags that
survive into the benchmark's category table — blank and multiple identifier
columns, faint and erased marks, tick/cross/ring/dot/stroke/slash mark styles,
a ten-step intensity sweep across the decision boundary, rotation, quarter
turns, scale, translation, perspective, cropping, faint/damaged/missing/extra
registration markers, blur, noise, speckle, exposure, JPEG artefacts, paper
tint, scanner streaks, edge shadow, planted duplicate identifiers, and
deliberate combinations. Profiles (Baseline, Recognition, Degradation, Batch,
Stress, Mixed, Custom) choose which families are drawn on; **the interesting
cases are emitted first**, so a twelve-sheet dataset is a spread of edge cases
rather than a random sample that happens to omit the one that would fail.
A seed makes a dataset byte-for-byte reproducible.

**Run Recognition Benchmark** does *not* open a second processing window. It
puts the existing Step 3 (Scan) page into **benchmark mode** — a banner, the
dataset's scans in the ordinary list, and the same *Process All* button, the
same settings and the same worker pool — because a benchmark of a different
pipeline would measure nothing worth knowing. When the run ends it is scored
automatically and reported:

```text
Dataset: synthetic · 120 scans · sheet accuracy 0.9397 · answer accuracy 0.9652

By test case (least accurate first):
  MARK_STYLE_DOT        1 sheet   sheet ok 0.0000   answers 0.0000   100 errors
  MARK_STYLE_SLASH      1 sheet   sheet ok 0.0000   answers 0.0000   100 errors
  ...
  BASELINE             15 sheets  sheet ok 1.0000   answers 1.0000     0 errors
```

The report covers sheet, registration, student-ID, set-code and answer
accuracy, blank and multiple-mark handling, borderline marks, accuracy by
decision-score band, and every disagreement classified by kind
(`FALSE_BLANK`, `FALSE_MARK`, `WRONG_OPTION`, `MISSED_MULTIPLE_MARK`,
`ROLL_ERROR`, `ROLL_AMBIGUITY_MISSED`, `SET_ERROR`, `ALIGNMENT_ERROR`,
`ORIENTATION_ERROR`, `PROCESSING_FAILURE`). **Per-test-case-category metrics**
are the point of the tags: a dataset that is 94% correct overall is far more
useful described as "perfect everywhere except dot-shaped marks". Failing
scans are listed and open in the scan list with their overlay, and each run
writes `summary.json`, `summary.csv`, `errors.csv`, `category_metrics.csv` and
`run_config.json` beside the dataset, comparing itself with the previous run.

Duplicate identifiers are benchmarked **separately** from recognition
correctness: reading the same number on two sheets is *correct*, and whether
the batch layer noticed is a different question from whether the engine read
the digits.

```bash
# Generate a reproducible labelled dataset
python -m omr_scanner.tools.make_dataset out/dataset --template sheet.omrt \
    --count 250 --profile mixed --seed 20260918 --format png

# Score it, printing the per-test-case table
python -m omr_scanner.tools.benchmark_recognition out/dataset \
    --template sheet.omrt --report out/benchmark --workers auto --categories 0
```

> **What synthetic numbers mean.** These datasets measure *regression
> consistency* and *controlled edge-case handling*. They do **not** establish
> real-world recognition accuracy — the pages have clean geometry, even paper
> and marks drawn by arithmetic. Every report this tool writes says so in the
> file itself.

One finding already recorded rather than tuned away: on a 100-question
template with the default 0.55 fill threshold, **dot-shaped marks measure a
mean fill ratio of 0.16** and are read confidently as blank, while horizontal
strokes and slashes measure 0.51–0.54 and land in the uncertainty band, where
the engine flags them for review. Filled bubbles measure 0.93. Whether real
candidates' marks behave this way is exactly what the real-dataset corpus is
for.

### Phase 3 testing status

Phase 3's functionality is implemented and exercised by an automated suite
(2,029 tests passing at the time of writing, plus the repository-local
`qtguitesting` Qt GUI harness), but it has only been run end-to-end against
**one real scanned sheet** (`examples/ECE-0000.png`) — validated with correct
roll number, set code and all 100 answers — plus geometrically distorted copies
of that same sheet and synthetic pages. It has not yet been run against a
broad, independently filled corpus (varied handwriting, pencil vs. pen,
erasures, genuinely ambiguous marks, different scanners), which is the
condition under which Phase 3 will be marked complete. Full detail is in
[`development/PHASE_03_HANDOFF.md`](development/PHASE_03_HANDOFF.md).

Confirmed by the current automated suite:

- [x] Recognition-accuracy validation (real sample: 100/100 answers, roll and
  set code correct, 0 flagged; synthetic corpus covering blank/single/multiple/
  uncertain marks)
- [x] Rotation/orientation testing (±3° rotation, exact 90°/180°/270° turns,
  rescaling, translation, perspective distortion, JPEG re-encoding — the real
  sample reads identically under each)
- [x] Duplicate roll-number handling (`_a`/`_b`/`_c`/... and beyond 26 via
  `_aa`)
- [x] Existing-output-filename collision handling (a pre-existing file is
  never overwritten)
- [x] CSV export validation (column order, question ordering, Unicode,
  escaping, duplicate-name recording, determinism)
- [x] Automated Qt GUI validation using `qtguitesting` (27/27 smoke checks,
  including the real sample recognised end-to-end through the GUI, and a
  dataset generated from the real template and benchmarked through the Scan
  page; three real defects were found this way and fixed)
- [x] Multicore recognition validation (a batch read across worker processes
  produces the same results, in the same order, as one read on a single core;
  also checked through the GUI on the real sample)
- [x] Single-core vs multicore result consistency (the same dataset processed
  at 1, 2 and 4 workers exports **byte-identical** CSVs — values, statuses,
  confidences, output names and row order)
- [x] Parallel duplicate-roll collision testing (eight sheets recognising to
  one roll number, read on four workers, produce `2103123.png`, `_a` ... `_g`;
  nothing overwritten, every CSV row naming the file actually written)
- [x] Worker failure isolation (corrupt, missing and unregistrable images fail
  individually; a worker process that dies outright becomes one failed scan,
  not a failed batch; no worker process outlives a run, a cancellation or the
  window)
- [x] Multicore GUI responsiveness (the event loop keeps delivering queued
  progress signals throughout a four-worker run; the progress bar advances
  monotonically to the scan count and the completion summary is shown)
- [x] Performance benchmarking (48 real scans at 1/2/4/8/12/16 workers, with
  peak memory; measured results and the reasoning behind the automatic worker
  cap are recorded in `docs/scan_workflow.md` §10)
- [x] Recognition API and result contract (engine runs headlessly with no Qt
  imported; `ScanResult` serialises, round-trips and refuses a newer schema)
- [x] Synthetic dataset generation (reproducible from a seed; labels verified
  by recognising the generated sheets; baseline profile scores 100%; DPI
  derived from the template's physical page size; PNG and JPEG output;
  cancellable, one sheet in memory at a time)
- [x] Template-independence of the generator (a nine-digit identifier, five
  options and a template with no set code at all each generate correctly with
  no code change)
- [x] Named test cases and their tags (every profile's mandatory edge cases
  present by construction, and still present when the dataset is too small to
  hold them all)
- [x] Benchmark harness and error categorisation (summary metrics, per-error
  CSV, per-test-case-category metrics, baseline and per-category regression
  comparison, run configuration — all exercised by tests)
- [x] Duplicate-identifier benchmarking, scored apart from recognition
  correctness (planted groups detected, missed, and unplanted collisions)
- [x] Developer testing tools through the GUI (menu, generation dialog,
  cancellable generation, benchmark mode in the Scan page, results dialog,
  failing-case review, second-run comparison — 20 pytest-qt tests)
- [x] Diagnostics (staged images and overlay produced on demand, nothing
  written by default, and generating them provably does not change a result)
- [x] Stored result fixtures for Phases 4-5 (11 scenarios, loadable with no
  recognition engine present)
- [x] Large-batch progress tracking (headless 10,000-job simulation reaching
  exactly 10,000/10,000 and 100%; ETA warm-up, smoothing, stall and
  cancellation behaviour; counters reconciling with the batch report)
- [x] Progress GUI validation (bar advances monotonically to exactly 100%, a
  failed scan still advances it, no dialog per failed scan, cancellation is
  immediate and honest, ten thousand rows create exactly one progress bar)

Still open, and why Phase 3 is not marked complete:

- [ ] Validation against a broad corpus of independently, genuinely filled
  sheets (different handwriting, pencil/pen, erasures, real ambiguous marks) —
  currently one real sheet plus geometric variants of it
- [ ] Registration/alignment edge cases beyond Phase 1's documented limits
  (e.g. illumination gradients, rotation beyond ±15°, heavy cropping)
- [ ] Batch-processing stress testing at realistic exam volumes. The largest
  *measured* run is 48 real scans; the 10,000-scan figure the architecture
  targets has been exercised as a simulation of the progress and counting
  path, not as ten thousand real recognitions
- [ ] Multicore validation on hardware other than the 16-thread Windows
  development machine (core counts, memory limits and `spawn` behaviour all
  differ; Linux and macOS are untested)
- [ ] **Threshold calibration against real marks.** The defaults come from one
  real sheet and synthetic pages
- [ ] **Confidence calibration.** What the engine reports is a bounded
  *decision score*, not a probability; the benchmark reports accuracy by band
  so it can be checked once there is data to check it against
- [ ] Difficult handwriting and mark-shape analysis. Synthetic datasets already
  show the shape of the problem: on one template tick-shaped marks produced a
  ~33% false-blank rate against 0% for crosses and scribbles, and on another
  dot-shaped marks measured a mean fill ratio of 0.16 (read confidently as
  blank) while strokes and slashes measured 0.51–0.54 (flagged as uncertain),
  against 0.93 for a filled bubble — all because the measurement is
  coverage-based. Whether real marks behave this way is exactly what the corpus
  is for, and the thresholds are deliberately **not** being tuned against
  synthetic pages
- [ ] Recognition accuracy and performance optimisation, once there is real
  data to optimise against
- [ ] Final Phase 3 regression sign-off once the above are addressed

### Validating a template before a batch (Phase 4)

Run this before trusting a template with a production batch. Full detail,
including what each overlay means, is in
[`docs/calibration_workflow.md`](docs/calibration_workflow.md).

1. Create or load the template in the **Template** stage.
2. Open **Calibrate**.
3. Load **several representative real scans** — light and dark marking, pencil
   and pen, a slightly skewed feed, more than one scanner if the batch will
   use more than one. One perfect scan proves very little.
4. **Run All Tests**.
5. Verify registration: status, `Markers detected: 4 / 4`, orientation
   resolved.
6. Switch on the **Markers** overlay and confirm the detected marker squares
   sit on the printed registration marks.
7. Switch on **Sampling** and **Centres**, zoom to 100%, and verify the bubble
   geometry at the **top, middle *and* bottom** of the page — a scale or
   perspective error accumulates down the sheet, so a template that looks
   perfect in the Student ID block can be most of a bubble out by the last
   question.
8. Inspect the **Student ID** block position by position in the field
   diagnostics panel.
9. Inspect the **Set Code** block the same way.
10. Inspect question regions across the whole page, not just the first column.
11. Click individual bubbles — one marked, one empty — and read their raw fill
    scores against the active threshold.
12. Adjust a threshold **only when the evidence calls for it**. A misplaced
    region is a geometry problem; no threshold fixes it. Use **Edit Template**
    to return to the designer instead.
13. Re-run, and confirm the change holds across **every** representative scan.
14. Review the warnings and findings on each scan.
15. **Save to Template** — nothing is written until you do.
16. Proceed to the **Scan** stage only once satisfied.

> **What a successful calibration means.** That the scans you tested appear
> geometrically and numerically suitable for this template. It is **not** a
> guarantee of accuracy on every future sheet, and a calibration run against
> synthetic scans establishes nothing about real-world accuracy at all.

### Phase 4 testing status

Phase 4 (Template Calibration & Validation) is implemented and covered by 89
automated tests (34 unit, 18 integration, 37 GUI), plus `qtguitesting` smoke
checks and screenshot inspection against the real sample sheet. Full detail is
in [`development/PHASE_04_HANDOFF.md`](development/PHASE_04_HANDOFF.md).

Confirmed by the current automated suite:

- [x] Overlay geometry matches the recognition engine's own computed
  coordinates exactly (asserted, not merely visually checked)
- [x] The overlay draws the region the sampler **actually measured** — an
  ellipse at 62% of the printed bubble's half-axes, recorded by the sampler
  itself — as a layer distinct from the printed bubble outline, so an operator
  checking alignment is never shown a region recognition did not read
- [x] A template whose **bubble zones** are displaced but whose **markers
  still register** is reported as needing review rather than passing — the
  case no registration-level check can see, because every sampling window
  lands on the page and every group reads a confident blank
- [x] Threshold changes propagate to results, the overlay and the quality
  summary without repeating registration (asserted via unchanged measured
  fill values and unchanged registration output on a re-decide)
- [x] A deliberately mismatched template is reported `Calibration Failed`,
  with no fields, answers or bubbles at all - verified against the real
  scanned sheet as well as synthetic ones
- [x] Small marker offsets are tolerated and large ones are flagged, using
  tolerances derived from the template's own measured geometry rather than
  assumed numbers
- [x] Non-destructive threshold adjustment (working value vs. saved value;
  reset to template, reset to defaults, explicit save)
- [x] Template staleness detection (an edited template's saved calibration is
  correctly invalidated; the Scan page shows the resulting warning)
- [x] Per-scan and per-sample (aggregate) quality summaries, worst-status-wins
  aggregation
- [x] Backward compatibility: a template saved before this phase loads with no
  recorded calibration, rather than failing to load
- [x] Automated Qt GUI validation using `qtguitesting` (30/30 smoke checks,
  including the real sample sheet scoring `passed_with_warnings` and a
  deliberately mismatched template scoring `failed`)
- [x] Full pre-existing Phase 1-3 recognition, batch and benchmark suites pass
  unchanged after the one isolated, behaviour-preserving refactor this phase
  made to `recognition_service` (see
  [`development/PHASE_03_HANDOFF.md`](development/PHASE_03_HANDOFF.md))

Still open, and why Phase 4 is not marked complete:

- [ ] Calibrating a template against representative scans has **not** been
  shown to make Phase 3's underlying recognition accurate - it makes
  mis-registration and mis-calibration visible and correctable, which is a
  different, narrower claim
- [ ] No real corpus of *deliberately* miscalibrated templates exists yet to
  validate the calibration-judgement thresholds (5% unusable bubbles, 30%
  systematic ambiguity) against; they are reasoned defaults, documented as
  such
- [ ] A score-distribution histogram was not built; a simpler, documented
  textual separation label is used instead, per the phase's own brief
  permitting a simpler alternative where a fitted statistical measure could
  not be justified on the data available

### Phase 5 testing status

Phase 5 (Batch Scan Processing Pipeline) is implemented and covered by 72 new
automated tests (35 unit, 17 integration, 20 GUI), plus three new `qtguitesting`
smoke checks against the repository's real sample sheet. Full detail is in
[`development/PHASE_05_HANDOFF.md`](development/PHASE_05_HANDOFF.md).

Confirmed by the current automated suite:

- [x] **Original scans are byte-for-byte unchanged** by processing — hashed
  before and after a batch that includes renaming *and* a deliberately corrupt
  file, at both the integration and `qtguitesting` levels (the mandatory Phase 5
  exit criterion)
- [x] Recognition results are persisted **incrementally**, in bounded groups,
  so an abrupt termination costs at most a second or two of finished work
- [x] A cancelled batch keeps everything it read and leaves the rest resumable
- [x] **Resume processes only what is left** — asserted by the resumed run's own
  scan count, not merely by the final total
- [x] A batch interrupted mid-run is repaired on reopening: scans left
  in-flight return to *pending*, never to *failed*
- [x] Resuming with a changed template or changed thresholds warns and asks,
  rather than silently mixing incompatible results
- [x] One broken sheet fails alone; the batch finishes and records the reason
  and a machine-readable error category
- [x] A sheet that cannot be registered produces no fabricated values at all
- [x] Single-worker and multi-worker runs persist identical results, in
  identical order
- [x] Duplicate roll numbers produce distinct output files and distinct rows;
  nothing is overwritten
- [x] The GUI event loop keeps running throughout a batch (asserted with a
  timer that could not tick if the GUI thread were blocked)
- [x] Closing the window mid-batch warns, stops the run, waits for it, and
  leaves the batch resumable with no orphaned worker processes
- [x] A storage failure is reported rather than swallowed, and never presented
  as a successful run
- [x] Multiprocessing genuinely parallelises: measured **1.00x / 1.30x / 1.79x /
  1.98x** at 1/2/4/8 workers over 24 real scans on the development machine
- [x] Existing project databases gain the new tables by migration, not by
  `create_all`

Still open, and why Phase 5 is not marked complete:

- [ ] **No real examination-scale run.** The largest *measured* batch is 48
  real scans; the 10,000-scan figure the architecture targets has been
  exercised as a simulation of the progress and counting path, not as ten
  thousand real recognitions with ten thousand database rows
- [ ] Persistence has not been exercised on a network share, a synchronised
  folder (OneDrive/Dropbox) or a full disk — the three places a real
  examination office would most plausibly hit a storage failure
- [ ] Crash recovery is tested by *simulating* the state a crash leaves
  (closing the database with rows still claimed as in-flight), not by killing
  a live process
- [ ] Only tested on Windows with a 16-thread CPU; Linux and macOS are
  untested, as are low-memory machines
- [ ] **Phase 5 makes a batch reliable, not accurate.** It says nothing about
  whether the values it durably recorded are correct — that remains Phase 3's
  open item

### Reviewing what the machine was unsure about (Phase 6)

Between processing a batch and trusting its CSV. Full detail, including every
conflict type and what the audit ledger guarantees, is in
[`docs/conflict_review.md`](docs/conflict_review.md).

1. Put your name in **File > Settings > Reviewer**. It is remembered between
   sessions. **A correction cannot be saved without one** — attribution is the
   point of this stage.
2. Process the batch on the **Scan** stage. Conflicts are detected automatically
   when it finishes, and the page reports how many need review.
3. Press **Review Conflicts**, or open **Resolve**.
4. Work the queue. **Next unresolved** skips to the next thing nobody has
   decided; ← and → step through; **Enter** accepts the machine's value and
   **D** defers.
5. For each conflict, look at the **zoomed field** first — that is the evidence.
   The **normalised sheet** shows where on the page it sits, and the **original
   scan** shows the file exactly as it arrived, in case the fault is in the
   scan rather than the reading.
6. Read **What the machine saw**, including each option's measured fill score.
   These are coverage measurements, not probabilities.
7. **Accept** when the machine was right; pick a **value** when it was not; give
   a reason for a correction.
8. Deal with the **sheet-level** conflicts too. A registration failure or an
   unreadable file has no value to correct — it needs re-scanning, and the queue
   says so rather than offering you an answer to pick.
9. Check for **duplicate student IDs** — these are found across the whole batch
   and are the one conflict no single sheet could reveal.
10. Export when the queue is clear. Exporting earlier warns and states the
    count; it does not block, but the CSV records `unresolved_conflicts` per
    sheet either way.

> **What review does and does not establish.** That a person looked at every
> value the machine flagged, and that every final value traces back to either
> the machine or a named human decision with a reason. It is **not** a check
> that the machine was right about what it did *not* flag — a confidently wrong
> reading never reaches this queue. That is what Phase 4's calibration and
> Phase 3's accuracy validation are for.

### Phase 6 testing status

Phase 6 (Conflict Detection & Human Resolution) is implemented and covered by
149 new automated tests (88 unit, 24 integration, 37 GUI), plus three new
`qtguitesting` smoke checks and screenshot inspection of the Resolve page. The
integration tests start from **marks rendered on a page** and run the real
recognition engine, a real project database and the real review services, so
the conflicts under test are the ones the engine genuinely produces rather than
the ones a fixture author assumed it would. Full detail is in
[`development/PHASE_06_HANDOFF.md`](development/PHASE_06_HANDOFF.md).

Confirmed by the current automated suite:

- [x] **The machine value is never overwritten** — after one correction, after
  two, and in the stored columns themselves; the special multi-mark form
  (`B-D`) survives intact (the mandatory Phase 6 invariant)
- [x] **The audit ledger cannot be rewritten** — the service module exposes no
  update or delete for events, and the database refuses both outright; a
  refused tamper leaves the ledger complete
- [x] **A correction without a named reviewer is refused**, as is an "Other"
  reason with no explanation; accepting records a confirmation reason without
  the reviewer typing one
- [x] **Reopening restores the machine's value while keeping the superseded
  correction** verbatim, with its own reviewer, reason and timestamp
- [x] The cached conflict state is **always reconstructible** by folding its
  events — asserted over sequences of actions, so the cache cannot become a
  second source of truth
- [x] **A decision is one transaction**: a write made to fail mid-way leaves
  neither the event nor the state change
- [x] Detection is **deterministic and idempotent** — the same result produces
  the same conflicts every time, and re-syncing an unchanged sheet writes
  nothing to the ledger at all
- [x] A changed machine reading is **recorded, not silently overwritten**; a
  conflict the machine no longer raises is **withdrawn, not deleted**; and a
  later machine read **never** withdraws a conflict a person has decided
- [x] Every conflict kind in the taxonomy is raised from a real rendered sheet:
  blank and multiply-marked identifier columns, blank and multiply-marked set
  codes, multiple answers, a failed registration, an undecodable file — and a
  clean sheet raises nothing at all
- [x] **A processing failure is not a value**: an undecodable image offers no
  answer buttons, and a value correction against one is refused by the service
  as well as hidden by the GUI
- [x] **Duplicate identifiers are detected across the batch**, each sheet
  pointing at the others, the unique sheet untouched, and re-detection
  idempotent
- [x] Choices, labels and symbol sets come from **the template**, never a
  hard-coded `A`/`B`/`C`/`D`; a multi-position set code is reported per
  position; question numbers come from the template's own numbering
- [x] **Original scans are byte-for-byte unchanged** by review (Phase 5's
  invariant, still in force — review draws overlays, never annotations)
- [x] Everything survives closing and reopening the project — states, history
  and effective values — and reopening a batch does not duplicate its conflicts
- [x] **An existing Phase 5 project upgrades cleanly**: a database genuinely
  wound back to schema version 2 regains both tables and both triggers on open,
  keeps its batch and results intact, and its conflicts are re-detected from
  the *stored* results without re-reading a single image
- [x] **The queue is paged and counted in SQL at batch scale** — 10,000
  conflicts, one page costing a bounded number of statements and the summary a
  bounded number of grouped queries, asserted by counting statements rather
  than by timing a particular machine
- [x] The GUI event loop keeps running while a sheet loads, and walking one
  sheet's conflicts **decodes its image once**
- [x] The CSV carries `value_source` and `unresolved_conflicts`, an unreviewed
  export says the values are the machine's, and exporting with conflicts open
  warns and states the count
- [x] Automated Qt GUI validation using `qtguitesting` (38/38 smoke checks,
  including a named decision recorded end to end against the real sample sheet,
  the ledger refusing both an update and a delete, and a correction refused for
  want of a reviewer)
- [x] **Phase 5's multiprocessing is intact**: the same four sheets read on 1
  worker and on 4 produce byte-identical conflicts, with the run's own reported
  worker count asserted so the comparison cannot be between two sequential runs
- [x] Full pre-existing Phase 0-5 suites pass unchanged (2,411 tests, 1 skipped)

Still open, and why Phase 6 is not marked complete:

- [ ] **No review session with real operators on a real batch.** Everything
  above is automated; the workflow has never been driven by someone reviewing
  sheets they cared about, which is the only way to find out whether the queue
  is usable rather than merely correct
- [ ] Queue performance at scale is asserted on a **synthetic**
  10,000-conflict batch and reasoned from the SQL. No real examination-scale
  review has been timed, and the sheet re-read on selection has been measured
  only on the development machine
- [ ] The conflict taxonomy's **defaults** (blank answers not flagged, assumed
  orientation flagged, alignment warnings not flagged) are reasoned choices
  documented as such in [`docs/conflict_review.md`](docs/conflict_review.md).
  Which of them an examination office actually wants is not yet known
- [ ] **Phase 6 makes what the machine was *unsure* about reviewable.** A
  confidently wrong reading never reaches the queue, so this phase does not
  bound the error rate — that remains Phase 3's open item, and Phase 4's
  calibration is what narrows it
- [ ] The ledger is append-only, not tamper-*proof*. A database administrator
  with direct file access can still alter it; building cryptographically
  chained enterprise logging was explicitly out of scope
- [ ] Reviewer identity is a **name, not an account**. There is no
  authentication, so the ledger records who *said* they made a decision

### Phase 7 testing status

Phase 7 (Candidate & Attendance Reconciliation) is implemented and covered by
279 new automated tests (204 unit, 19 integration, 56 GUI), plus five new
`qtguitesting` smoke checks. The integration tests run **real recognition over
real rendered sheets** against a real roster and a real project database, so
the roll numbers being reconciled are the ones the engine actually read. Full
detail is in
[`development/PHASE_07_HANDOFF.md`](development/PHASE_07_HANDOFF.md).

Confirmed by the current automated suite:

- [x] **Every required classification** is produced from controlled data and
  from a real end-to-end run: matched, absent confirmed, unknown ID, duplicate
  script, present without script, absent with script, and candidate ID not yet
  resolved
- [x] **Nothing disappears**: every script in a batch appears in exactly one
  entry, including scripts belonging to nobody and scripts set aside; every
  registered candidate has an entry
- [x] **Co-occurring conditions are both reported** — a candidate marked absent
  with two scripts carries both issues, in the summary, the table and the
  detail
- [x] **The imported value is never changed.** Overriding an attendance leaves
  `imported_attendance` and the raw cell exactly as the file had them
- [x] **The machine value is never changed.** Assigning a script to another
  candidate leaves what recognition read intact, and the audit event carries
  both
- [x] **`ABSENT`/`ABS` in every case and spacing** reads as absent; `ABSENTEE`,
  `ABSENCE`, `ABS123`, a blank cell and a mark of zero do not
- [x] **Candidate IDs survive Excel**: `15000001` imports as `"15000001"` and
  never `"15000001.0"`; a non-integral value is not rounded; text IDs and
  leading zeros pass through; two distinct IDs never collapse into one
- [x] **A duplicate candidate ID stops the import**, naming the ID and both
  rows, and is never silently deduplicated; **a failed import leaves nothing
  behind**
- [x] **Ambiguous and missing ID columns ask rather than guess**, with a
  message saying what to do
- [x] Malformed input fails readably, never by crashing: corrupt workbook, a
  zip that is not a workbook, empty worksheet, header-only file, missing file,
  a folder, a legacy `.xls`, an unsupported extension, a malformed CSV, a
  mapping that names one column twice
- [x] The **packaged sample** is byte-identical when saved, contains
  placeholder names only, imports straight back with no mapping help, and is
  never altered by being handed out
- [x] **Re-running reconciliation is idempotent** — no duplicate exception
  records, exactly one link row per scan, and no stale classification after a
  roster change
- [x] **Cascading exceptions are surfaced**: assigning a script to an occupied
  candidate reports the new duplicate immediately, and the entry stays open
- [x] **A set-aside script is never deleted** — the scan row, the recognition
  result, the reason and the audit trail all remain
- [x] Every decision **appends** an audit event with both values and the
  operator's name; a second decision appends rather than replacing; the ledger
  still refuses an `UPDATE` and a `DELETE`
- [x] **A decision without a named operator is refused**, and nothing is
  written
- [x] **Everything survives closing and reopening the project** — roster,
  classifications, resolutions, reasons, attendance overrides and history
- [x] **An existing Phase 6 project upgrades cleanly**: a database wound back
  to schema version 3 regains the six tables and the two audit columns, keeps
  its conflicts and its ledger, and is reconcilable afterwards — with **no
  audit row rewritten**
- [x] **No candidate name, ID or mark reaches the log** across import,
  reconciliation, unknown-ID handling, duplicate handling, refusals and errors
  — 18 dedicated tests plus a grep that fails if a Phase 7 module formats
  candidate data into a log call at all
- [x] **The source roster file and the source scans are byte-for-byte
  unchanged** by reconciliation
- [x] **Phase 5's multiprocessing is intact**: the same sheets read on 1 worker
  and on 4 reconcile identically, with the reported worker count asserted
- [x] The GUI event loop keeps running while a roster imports and reconciles
- [x] Matching is indexed, not quadratic: 10,000 candidates against 10,000
  scripts
- [x] Automated Qt GUI validation using `qtguitesting` (43/43 smoke checks,
  including every classification, the machine-and-imported-values check, the
  missing-operator refusal, the sample download, and a log-capture check)

Still open, and why Phase 7 is not marked complete:

- [ ] **No reconciliation of a real cohort against a real roster.** Everything
  is synthetic or the repository's one real sheet. A genuine examination roster
  — with its own column names, its own spelling of absence, and candidates who
  really are missing — has never been through this
- [ ] **No examination-scale run.** Matching is asserted at 10,000 candidates,
  but the largest *real* batch anywhere in this project is 48 scans
- [ ] **Scan file names can still leak a roll number into the log.** Phase 3
  logs the file name of each scan it reads — documented, and useful — so an
  office whose files are named by roll number has roll numbers in its
  application log. Phase 7 puts none there itself; the exposure is recorded in
  [`docs/reconciliation.md`](docs/reconciliation.md) §9 and pinned by a test
- [ ] **Reconciliation is per batch.** There is no project-wide view, and a
  cohort split across two batches must be reconciled twice
- [ ] The operator's identity is a **name, not an account** — the same
  limitation Phase 6 has
- [ ] **Phase 7 says nothing about whether an answer is right.** It accounts
  for scripts and candidates; scoring is Phase 8

### Reconciling the scripts against the candidate list (Phase 7)

After the conflict queue is clear, and before anyone trusts a set of results.
Full detail is in [`docs/reconciliation.md`](docs/reconciliation.md).

1. Open **Attendance**. Press **Download Sample Template…** if you want to see
   the columns OMRFlow understands — your own list does not have to match it.
2. **Import Candidate List…** and choose your CSV or `.xlsx`. Pick the
   worksheet if the workbook has several.
3. Check the **preview**, then the **column mapping**: Candidate ID is
   required; Name and Marks/Attendance are optional. OMRFlow suggests a mapping
   and asks rather than guessing when two columns could both be the ID.
4. Read the **validation** line: rows read, candidates accepted, expected
   present, marked absent, and anything wrong. A duplicate candidate ID has to
   be fixed in the source file — OMRFlow will not choose between two rows that
   claim the same person.
5. **Import Candidates**. Reconciliation runs immediately.
6. Work the **exception list** — it is what the table shows by default.
7. For each exception, read the detail panel: what the candidate list said, what
   recognition read, and every script attributed to the entry.
8. Decide, with a reason: **assign** a script to the right candidate, **set
   aside** an accidental re-scan, **override attendance**, or **accept as-is**.
   Put your name in *File > Settings > Reviewer* first — a decision cannot be
   saved without one.
9. Watch the counts. A decision that creates a *new* problem — assigning a
   script to a candidate who already has one — says so at once.
10. Send anything reading **Candidate ID not yet resolved** back to the
    **Resolve** stage; those are recognition problems, not roster problems.

> **What reconciliation establishes.** That every script maps to exactly one
> registered candidate or to an explicit exception somebody has looked at, and
> that every registered candidate has an understandable state. It is **not** a
> check that a candidate's answers are right, or that a reconciliation decision
> was correct — only that it was made by a named person, for a stated reason,
> and can be traced and reversed.

### Phase 8 testing status

Phase 8 (Answer-Key & Scoring Engine) is implemented and covered by 314
automated tests (219 unit, 23 integration, 72 GUI), plus five
`qtguitesting` smoke checks. The integration tests run **real recognition over
real rendered sheets**; the unit tests assert every mark **exactly**, as a
rational, because `assert score == 0.5` would pass for a value that is not one
half. Full detail is in
[`development/PHASE_08_HANDOFF.md`](development/PHASE_08_HANDOFF.md).

The implementation was subsequently put through an **independent adversarial
audit**, which found and repaired nine defects — among them a key written for
a differently numbered paper silently withdrawing no questions at all, a
candidate recorded absent whose script had turned up being filed as a settled
"Absent", a template edit quietly costing candidates the multiple-answer
deduction, and a verified key not reaching the stage that uses it. The audit's
findings and the tests added to pin them down are in the Phase 8 handoff.

Confirmed by the current automated suite:

- [x] Every hand-calculated case in the phase brief, exactly: all-correct,
  one wrong, blank, multiple, the `AA?_` fixed-deduction case totalling `0.50`,
  and the 1-per-3 and 1-per-4 examples
- [x] **Fractional penalties are not truncated** — one wrong answer under the
  1-per-3 rule costs exactly `1/3`, two cost `2/3`, three cost `1`
- [x] **Exact arithmetic**: `0.1 + 0.2 == 0.3`, three hundred thirds sum to
  exactly 100, and display rounding never feeds back into a total
- [x] **Wrong questions take precedence over everything** — a right answer, a
  wrong answer, a multiple and a blank all receive full credit, and no
  deduction is ever applied
- [x] The canonical answer string keeps question *N* at position *N*, never
  compresses, never drops a blank, and works with six-option papers and
  non-1-based numbering
- [x] **An unresolved reading blocks scoring**, naming the question, rather
  than being marked as a blank or a confirmed multiple
- [x] **An unverified key produces no marks**, and a draft read off a solution
  sheet is still a draft
- [x] Key validation names the question and reports **every** problem at once;
  a stray character is reported, never dropped
- [x] **Key revisions**: a verified key is never edited, correcting it
  supersedes the old revision, the old revision is **kept**, and a superseded
  revision cannot be re-verified
- [x] **Every result records the exact key and policy revision used**, and a
  later key does not change what an earlier mark was computed from
- [x] **A candidate is scored against their own set only** — a deliberately
  different Set B key makes cross-set scoring impossible to miss
- [x] **An absent candidate has no mark**, not a zero, and gets no
  wrong-question credit
- [x] Scoring is blocked, with a reason, for every unresolved condition: no
  script, an unnominated duplicate, an unknown candidate, a missing or unread
  set, no verified key, answers still under review, a key written for a
  differently numbered paper, or answers this template no longer offers
- [x] **A candidate recorded absent whose script turned up is blocked**, not
  filed as absent: that contradiction is Phase 7's to settle, and recording it
  as an outcome would leave a real script unmarked
- [x] A **blocked** result stops asserting a problem that has been fixed — once
  the missing key is verified, the row says it needs recomputing
- [x] A deduction for a multiple answer set by the operator is applied **in
  every negative-marking mode**, rather than being stored, previewed and then
  ignored outside the fixed one
- [x] Opening and saving the scoring configuration unchanged **never rewrites
  an exact rule**, so no revision is created and nothing goes stale for a rule
  nobody typed
- [x] **Recomputation never patches**: a deliberately corrupted stored score is
  ignored and the mark is re-derived from the stored inputs
- [x] The full key-then-policy sequence: score, change the policy, go stale,
  recompute, change the key, go stale, recompute — with the answers and set
  unchanged throughout and each revision recorded
- [x] **Repeated recomputation is idempotent**
- [x] A **cancelled** run writes nothing, rather than leaving a batch half
  marked under two policies, and the summary says so rather than looking like a
  finished run
- [x] The per-question breakdown is **regenerated** and always agrees with the
  total; a stale result explains the mark it actually has
- [x] A Phase 6 correction changes the effective answer, leaves the machine's
  reading intact, and makes the result stale
- [x] Everything survives closing and reopening the project, including exact
  `1/3` penalties and wrong-question flags
- [x] An existing Phase 7 project upgrades cleanly by migration
- [x] Source scans and the roster file are byte-for-byte unchanged by scoring
- [x] The GUI event loop keeps running while a batch is marked
- [x] **Verifying a key reaches the stage that uses it**, and a batch read
  during the session reaches the Results stage without reopening the project
- [x] **Verification applies to a stored revision, never to unsaved text** in
  the editor
- [x] A batch is read **once** per refresh of the Results stage, not once for
  the table and again for the summary line above it
- [x] Automated Qt GUI validation using `qtguitesting` (48/48 smoke checks,
  including a draft key producing no marks, the acceptance outcomes with the
  key revision recorded, a rule change making results stale, recomputation
  ignoring a corrupted mark, and a withdrawn question paying everyone)

Still open, and why Phase 8 is not marked complete:

- [ ] **No examination has been marked with it.** Every test is synthetic or
  uses the repository's one real sheet. No real cohort, no real answer key, no
  operator checking a mark against a paper in front of them
- [ ] **No examination-scale run.** Scoring ten thousand candidates is
  arithmetic and fast, but the largest *real* batch in this project is 48 scans
- [ ] **A key is not checked for correctness.** Verification records who
  looked at it, which is a different and much weaker claim
- [ ] Scoring is **per batch and per roster**; a cohort split across two
  batches is marked twice
- [ ] Section-wise or per-question mark weights are not supported: one policy
  applies to the whole paper

### Marking a batch (Phase 8)

After the conflict queue is clear and the batch is reconciled. Full detail is
in [`docs/scoring.md`](docs/scoring.md).

1. Open **Answer Key**. Choose the question-paper **set**.
2. Type or paste the correct answers, one character per question — or press
   **Read From Solution Sheet…** to recognise a filled solution sheet. Spaces,
   line breaks and commas are ignored; anything else is reported.
3. Flag any **wrong questions** for this set. Every scored candidate gets full
   credit for those, whatever they marked.
4. Check the validation line, then **Save As New Revision**.
5. **Verify Answer Key**. Nothing is marked against a draft. Put your name in
   *File > Settings > Reviewer* first.
6. Repeat for every set. Sets are independent — Set A's wrong questions need
   not be Set B's.
7. Open **Results**. Press **Scoring Configuration…** and set the marks, the
   negative-marking mode and the minimum total. The preview shows a worked
   example so a misplaced decimal point is visible before it is applied.
8. Press **Check Before Scoring**. It lists the rules and every candidate who
   cannot be marked, and why, together.
9. **Calculate Results**.
10. Work the list. A candidate who cannot be marked is a row with a reason, not
    an absence. Select one to see their answers question by question, with what
    the machine read beside what was scored.
11. If you change a key, a rule or an answer, affected results say **they need
    recalculating** and keep their old mark until you press **Calculate
    Results** again.

> **What a mark here establishes.** That it was computed by a stated rule from
> stated answers against a stated key revision, and that all three are recorded
> so the mark can be reproduced and defended. It is **not** a check that the
> key is right, or that recognition read the paper correctly — Phase 4's
> calibration and Phase 6's review are what narrow that.

### Phase 9 testing status

Phase 9 (Result Management & Reporting) is implemented and covered by 179 new
automated tests (142 unit, 25 integration, 12 GUI), plus 3 new `qtguitesting`
smoke checks. The integration tests run the phase brief's own six acceptance
scenarios end to end against real Phase 7/8 services; the unit tests assert
every generated workbook by reading it back with `openpyxl`, not by inspecting
internal state.

Confirmed by the current automated suite:

- [x] **Standard competition ranking**, checked against the brief's own
  worked example (`90, 88, 88, 85 → 1, 2, 2, 4`) and against a hand-built
  `RANK.EQ` truth table, including ties, zero, decimal and negative marks, and
  a 20,000-candidate timing check
- [x] **The Excel rank formula is generated, never hard-coded** — the marks
  column and the first/last row are derived from the template's own mapping
  and row count in every test, including one where the row range is neither
  `2` nor `230`
- [x] **No candidate row is lost.** The supplied sample's own shape (Sl.No./
  Roll No./Name/Total (90)/Merit) round-trips completely; every documented
  variation is covered — a different worksheet name, a header starting on row
  3+ with decorative rows above it, extra columns, merged header cells,
  `ABS`/lowercase `absent`, leading-zero Roll Nos., Unicode names, an empty
  name, and a duplicate Roll No. (kept and reported, never silently dropped)
- [x] **Absent candidates remain in the Rollwise list**, in their original
  position, with the canonical `ABSENT`/`---` marker — never deleted, never
  sorted away
- [x] **Present candidates carry the exact stored score**, as a number, at
  the same two-decimal precision Phase 8's own display already uses
- [x] **The original template is provably unmodified** — SHA-256 before and
  after every generation, byte for byte identical
- [x] **A readiness check blocks Final Export** for every condition the brief
  lists: no template, no verified key, a template candidate not in the
  project, a registered candidate missing from the template, a duplicate Roll
  No., an absentee-status mismatch, a present candidate with no score, and an
  unresolved reconciliation exception — every issue names what is wrong,
  never a stack trace
- [x] **A Preview still generates**, showing every readiness issue as a
  warning rather than refusing outright
- [x] **Per-set isolation**: two sets with deliberately different templates
  and keys produce workbooks that cannot cross-contaminate — proven by giving
  each set a key that would mark the *other* set's candidates at zero if it
  were ever used against them, and confirming neither is
- [x] **An existing output file is never silently overwritten** — a second
  generation writes `..._1`, and the first file is untouched
- [x] **Regenerate, never patch**: changing the scoring policy and
  regenerating produces a new report from the new stored marks, never a
  patched cell in the old one
- [x] **Spreadsheet-injection protection**: a candidate name beginning with
  `=`, `+`, `-` or `@` is quoted, never left as a live formula, in every sheet
  that writes user-controlled text
- [x] **Unicode names and project text** (Bangla tested explicitly) survive
  every sheet unchanged
- [x] **PDF export is dependency-injected and testable without a PDF
  engine** — the orchestration (readiness, regenerate-before-export, single-
  sheet extraction, no-silent-overwrite, graceful "unavailable" failure) is
  fully tested with a fake exporter; a real-LibreOffice test exists and is
  skipped, honestly, in this build environment
- [x] Automated Qt GUI validation using `qtguitesting` (52/52 smoke checks,
  including associating a template, generating an XLSX with every row and a
  working rank formula, and a second generation never overwriting the first)

Still open, and why Phase 9 is not marked complete:

- [ ] **No real examination has been reported on.** Every test is synthetic
  or uses this project's rendered sheets; no institution's own result
  template, filled by a real cohort, has been fed through this pipeline
- [ ] **No real-LibreOffice PDF verification.** LibreOffice was not installed
  in the environment this phase was built and tested in; the exporter
  abstraction and everything around it are fully tested by dependency
  injection, but a real conversion has not been observed
- [ ] **No Windows Excel COM PDF adapter** — deliberately not built; see
  `development/PHASE_09_HANDOFF.md` §12
- [ ] **No examination-scale run** — the largest real cohort anywhere in this
  project remains under fifty candidates
- [ ] Reporting is **per batch and per roster**, following Phase 8's own
  limitation — a cohort split across two batches is reported on twice

### Generating a report (Phase 9)

After a batch is scored (Phase 8). Full detail is in
[`docs/reporting.md`](docs/reporting.md).

1. Open **Reports**. Each row is a set with a verified key or a scored script.
2. **Select Template…** for a set and choose its result/absentee `.xlsx`
   workbook. Confirm the suggested column mapping — Roll No. and Marks are
   never guessed when two columns are equally plausible.
3. **Validate** to see every readiness issue at once, or **Preview** to
   generate a draft with issues shown as warnings rather than blocks.
4. **Report Layout…** to set a header/logo/font/page setup, per project or
   per set. Leaving everything blank preserves the template exactly.
5. **Generate XLSX** for the selected set, or **Generate All Sets** for every
   set the project has evidence of — one set's failure never hides another's
   success.
6. **Generate PDF** where LibreOffice is installed; otherwise XLSX generation
   still works and PDF export says plainly that it is unavailable.
7. If you change the answer key, the scoring configuration, an effective
   answer, or the associated template, the next generation reads the current
   stored state and writes a fresh file — nothing here ever patches a cell in
   a previously generated report.

> **What a generated report establishes.** That every mark, rank and
> attendance status on it was read from Phase 7/8's stored state at the moment
> of generation, that the rank is provably the same rank the workbook's own
> formula computes, and that the exact template, key revision and policy
> revision it was built from are recorded. It is **not** a check that the
> template's own candidate roster is correct, or that the underlying scan was
> read correctly — those remain Phase 7's and Phase 3's problems.

### Phase 10 testing status

Phase 10 (Integration, Recovery & Production Hardening) is implemented and
was subsequently the subject of an independent audit and validation pass
that treated its own prior test results as claims to verify, not facts to
accept — the pass found and fixed several genuine, previously-undiscovered
defects (an orphaned-worker-process bug, a silent CSV-export corruption
risk on an interrupted write, a non-ASCII-Windows-path failure in a debug
image writer, an unbounded-memory stress-harness query, and a telemetry
blind spot that hid nearly all real CPU usage) and completed several items
previously listed as not built (a diagnostic bundle, a global GUI exception
handler, automatic backup-before-migration, a lazy Qt model for the Scan
page's table, and a stress-scale reconciliation/reporting test at 10,000
sheets). Full detail for both passes, including every defect found and
fixed, is in
[`development/PHASE_10_HANDOFF.md`](development/PHASE_10_HANDOFF.md).

Confirmed by the current automated suite:

- [x] Content-hash provenance and exact-duplicate-file detection, threaded
  and off the GUI thread
- [x] A second live open of the same project is refused; a lock a crash left
  behind is never removed without an explicit operator decision
- [x] A read-only session cannot write, including the two "create a default
  row on first read" functions found, during this phase's own testing, to
  attempt one
- [x] A database backup is provably complete only once its manifest exists;
  an interrupted one is reported as incomplete, never as valid; a tampered
  one fails verification and cannot be restored
- [x] A quick and a comprehensive health check each run correctly against a
  database damaged at the byte level, not only against a synthetic
  "corrupt = true" flag
- [x] Reprocessing a sheet archives its previous machine reading before
  resetting it, and a sheet reprocessed twice keeps both readings on record
- [x] Reprocessing one sheet never touches an unrelated sheet in the same
  batch
- [x] Worker recycling and a configurable per-worker OpenCV thread count
  change nothing about what is recognised
- [x] A deterministic stress-sheet generator reproduces the same sheet from
  `(seed, index)` alone, independent of the run's total sheet count
- [x] **Real, forced (not simulated) process termination and resume**, at
  100, 1,000 and 10,000 sheets: every previously-committed sheet's result
  is unchanged after resume, and the final count is exactly correct (the
  10,000-sheet run took ~23 minutes for the full kill-then-resume cycle in
  this environment)
- [x] Registering a full 100,000-sheet batch was executed and measured
  directly (see the Phase 10 handoff for the exact time and peak memory)
- [x] A forced kill's worker processes are now bound to the coordinator's
  lifetime via a Windows Job Object and terminate with it — verified
  against real orphaned processes and a negative control, not merely
  claimed
- [x] The Scan page's scan list is a lazy `QAbstractTableModel`, not an
  item-per-cell `QTableWidget` — measured at 100,000 rows: ~4 orders of
  magnitude less memory and time than the eager approach it replaced
- [x] A privacy-safe diagnostic bundle and a global GUI exception handler
  both exist and are tested (*Tools* menu)
- [x] A database backup is taken automatically before an older-schema
  project is upgraded, in addition to the existing manual path
- [x] A real 10,000-sheet stress batch's recognition output was fed
  through the real reconciliation engine and the real Phase 9 Excel report
  generator, with a deterministic synthetic roster including absentees,
  duplicates, unknown candidates and an attendance disagreement — every
  invariant held and report cells were spot-checked against known values
- [x] An uninterrupted 10,000-sheet run was executed end to end (873s,
  10,000/10,000 processed) with corrected telemetry that measures the
  worker pool's real CPU/memory, not only the coordinator's
- [x] A real, non-ASCII, 270-character Windows path was used to create,
  write to and reopen a project end to end — which is what surfaced and
  led to fixing a silent `cv2.imwrite` failure on such a path elsewhere in
  the codebase

Still open, and why Phase 10 is not marked complete:

- [ ] **The mandatory full-scale 100,000-sheet processing run, and its
  1%/25%/50%/75%/99% kill-and-resume acceptance matrix, have not been
  executed** — a scoped, agreed deferral (roughly a day of machine time on
  this hardware), not a technical limitation. It is now a single unattended
  command rather than a manual procedure; see
  [Running the 100,000-sheet qualification](#running-the-100000-sheet-qualification)
  below and [`docs/phase10_qualification.md`](docs/phase10_qualification.md)
- [ ] **Lazy Qt models were built only for the Scan page's table.** The
  Results/Resolve/Attendance pages' equivalent tables remain item-based
  widgets — bounded by candidate/question/report counts rather than raw
  sheet count, so a lower-priority remaining gap than the Scan page was
- [ ] No packaged-application smoke test — no packaging build exists yet
  (Phase 11)
- [ ] No real-scan recognition *accuracy* calibration at any scale —
  everything above validates software correctness, recoverability and
  reproducibility under a synthetic workload, never recognition accuracy
  against real, physical examination scans

### Running the 100,000-sheet qualification

Phase 10's final acceptance test is one headless, unattended, resumable
command. It runs for many hours, force-kills processes on purpose, and needs
nobody watching it. Full detail — including what it does and does not
establish — is in
[`docs/phase10_qualification.md`](docs/phase10_qualification.md).

```powershell
# Check whether it can finish. Changes nothing, takes seconds, and prints
# the estimated runtime and peak disk for your machine.
.venv\Scripts\python.exe -m omr_scanner.tools.phase10_qualification preflight `
    --output-dir D:\OMRflow-qualification `
    --template examples\templates\100_question_4_choice_example.omrt

# Start it, then leave the machine alone.
.venv\Scripts\python.exe -m omr_scanner.tools.phase10_qualification run `
    --output-dir D:\OMRflow-qualification `
    --template examples\templates\100_question_4_choice_example.omrt

# Watch it from any other window, as often or as rarely as you like.
# Read-only; it cannot disturb the campaign.
.venv\Scripts\python.exe -m omr_scanner.tools.phase10_qualification status `
    --output-dir D:\OMRflow-qualification

# Continue an interrupted campaign. Verified runs are skipped.
.venv\Scripts\python.exe -m omr_scanner.tools.phase10_qualification resume `
    --output-dir D:\OMRflow-qualification
```

`.\run_phase10_100k_qualification.ps1 -OutputDir D:\OMRflow-qualification`
is a thin wrapper over the same command, and **Tools → Developer / Testing
→ Run 100,000-Sheet Stress Test...** is a launcher and monitor for it —
neither adds behaviour, and closing the GUI does not stop a running
campaign.

A campaign is one uninterrupted reference run plus **five independent
forced-kill runs**, each in its own project, killed once at 1%, 25%, 50%,
75% and 99% of durably-committed sheets and then restarted. Each must
satisfy fifteen release-blocking assertions — no committed result lost,
duplicated, re-read or re-decided; no worker process outliving the
coordinator it belonged to; SQLite's own integrity and foreign-key checks
clean; and every sheet's recorded decision identical to the reference run's.
No assertion is ever downgraded to a warning.

The report headlines **QUALIFIED** only for a full-scale, full-mode
campaign with all five checkpoints. A smaller campaign that passes
headlines `ALL RUNS PASSED — NOT THE RELEASE QUALIFICATION` and names the
specific deficiency, so a convenient shorter run cannot later be cited as
the qualification.

> The campaign performs real, abrupt process terminations. That is the test.
> OMRFlow never changes a Windows power setting — disable sleep yourself
> before starting a multi-hour run.

### Production hardening (Phase 10)

Phase 10 does not change what any earlier phase computes. It makes the
existing pipeline survive abrupt termination, detect its own damage, avoid
silent duplication, and demonstrate that its *architecture* — not its
recognition accuracy — scales to a 100,000-sheet examination. Full detail,
including every genuine defect found and fixed across both its original
implementation and a subsequent independent audit and validation pass, and
exactly what remains to be run at full scale, is in
[`development/PHASE_10_HANDOFF.md`](development/PHASE_10_HANDOFF.md).

**Recovery.**

- Every whole-file save (`project.json`, settings, `.omrt` templates) is
  already atomic (temp file, `fsync`, rename) — audited this phase, not
  newly built.
- A batch interrupted mid-run resumes from durable per-sheet state on the
  next project open (Phase 5, unchanged); nothing is re-read that already
  finished.
- **Project locking**: OMRFlow refuses to open a project another instance
  already has open for writing. A lock left behind by a crash is never
  removed automatically — you are shown who (or what) held it and whether
  it looks stale, and you decide: cancel, open **read-only**, or remove the
  lock and open for editing.
- **Backups**: *Tools → Project Health / Recovery…* → **Create Backup Now**
  takes a consistent snapshot using SQLite's own backup mechanism (safe
  against a live database), never a raw file copy. A backup that did not
  finish writing can never be mistaken for a complete one. A backup is also
  taken automatically, without any action needed, whenever opening a
  project finds it on an older schema than the current build — before that
  migration runs.
- **Diagnostic bundle**: *Tools → Create Diagnostic Bundle...* writes a
  `.zip` of version/environment info, the health check, processing settings
  and a bounded recent-log excerpt — never the project database, never a
  candidate name, roll number, answer or score, which a dedicated test
  confirms by inserting real secret data and asserting none of it appears
  in the output.
- **Unhandled errors**: an exception that escapes a Qt slot with nothing
  else catching it is logged and shown in plain language instead of
  disappearing silently — the message never claims unsaved work was saved;
  it tells you to check the screen you were on.
- **Health checks**: the same dialog's **Run Full Check** verifies database
  structural integrity, foreign-key consistency, schema version, source-scan
  availability (missing or changed since import), unresolved Phase 6/7
  exceptions, sets missing a verified Phase 8 key, and free disk space —
  and reports what it finds without attempting to repair anything. If a
  check fails, the guidance is to back up what remains and seek support;
  OMRFlow does not offer a "fix it automatically" button for a damaged
  examination database.
- **Relinking a moved scan**: a scan's SHA-256 content hash, recorded at
  import, is what lets a moved or renamed source file be safely reattached
  — verified by content, never by filename alone.

**Reprocessing.** Individual sheets, a selection, every currently-failed
sheet, or a whole batch can be sent back to `pending` for reprocessing,
without losing the machine's previous reading: it is archived first, so a
sheet reprocessed twice keeps both readings on record, not one overwritten
by the other. Rescoring after an answer-key or policy change, and
regenerating a report after a layout change, already worked this way from
Phases 8 and 9 and are unaffected.

**Large-batch architecture.** Job submission has always been bounded (never
more futures in flight than a small multiple of the worker count); worker
processes have never written to the database directly, and now cannot
outlive an abruptly killed coordinator either — a Windows Job Object binds
their lifetime to it, verified against real orphaned processes and a
negative control. New this phase: content-hash duplicate detection, an
explicit worker-recycling interval and a configurable OpenCV-thread count
per worker (both under *File → Settings → Advanced*), and a deterministic
synthetic-sheet generator that can register and address a 100,000-sheet
batch without ever holding 100,000 images on disk or in memory at once —
including the batch scheduler's own resume query, which fetches only a
bounded window of pending sheets at a time rather than the whole remaining
list. The Scan page's own scan list is likewise a lazy, on-demand Qt model
rather than a table of 100,000 pre-built widget items — measured at four
orders of magnitude less memory and build time than the eager approach it
replaced.

**Performance and reproducibility.** A headless benchmark CLI
(`python -m omr_scanner.tools.benchmark_stress`) runs or resumes a
deterministic synthetic stress batch of any size, with telemetry (CPU,
memory, disk, throughput) sampled to a file every few seconds and a JSON
report written at the end — including the worker pool's own aggregate
CPU/memory, not only the coordinator process's, so the report reflects
where recognition time is actually spent. The same `(seed, sheet_count)`
always produces the same 100,000 logical sheets, independently addressable
one at a time — what makes an interrupted run resumable without keeping
every generated image around. A deterministic synthetic roster generator
lets the same stress dataset also exercise reconciliation (absentees,
duplicates, unknown candidates, attendance disagreements) and Phase 9
report generation under load; both were verified end to end at a real
10,000-sheet scale. Real, deliberate, forced process terminations
(not simulated) were run against 100, 1,000 and 10,000-sheet batches, each
verified to lose nothing and duplicate nothing on resume; a separate
10,000-sheet run was also completed uninterrupted end to end. The
100,000-sheet acceptance campaign itself is now a single unattended command
(see [above](#running-the-100000-sheet-qualification)), validated end to end
at reduced scale — including a real forced kill and a real orchestrator
crash — but **not yet run at full scale**. See the Phase 10 handoff §14 for
exactly what that validation measured.

**Data protection.** Telemetry and benchmark reports contain sheet counts,
timings and resource usage only — never candidate names, roll numbers,
recognised answers or scan images, following the same rule Phase 7
established for the application log.

### Development philosophy

OMRFlow follows an incremental development process. Major functionality is
introduced in phases, followed by automated testing, GUI validation, regression
testing and stabilisation before a phase is considered complete. This does not
mean every phase must be completely frozen before work on the next begins —
Phase 4 (template calibration) and Phase 5 (persistent batch processing) both
build on Phase 3's recognition engine, for example — but a phase's own status
in the table above reflects its own testing state, not merely whether its code
exists.

### Pending development

Phase 11 has not started. Its title, purpose and deliverables as currently
planned are documented in
[`development/ROADMAP.md`](development/ROADMAP.md); this project does not
promise a delivery date for it, and its scope may be refined as earlier
phases surface real requirements.

**The examination-sets enhancement: Parts 1 and 2 are implemented.** Project
configuration (exam name and sets), per-Set attendance, set-aware report
generation, the rollwise sheet built on the attendance template, and the
`meritwise` sheet copied from it are all implemented and covered by automated
tests — **against synthetic data only**. No real examination office's
attendance workbook and no real scanned cohort has been processed end to end,
so none of it is marked complete.

**Real-world validation remains pending for every phase that needs it.**
Implemented, automated tests passing, synthetic dataset validated, real scanned
dataset validated and production validated are five separate things, and
OMRFlow currently claims the first three.

Current detail: [`development/CURRENT_STATE.md`](development/CURRENT_STATE.md).
Plan: [`development/ROADMAP.md`](development/ROADMAP.md).
Phase 3 handoff: [`development/PHASE_03_HANDOFF.md`](development/PHASE_03_HANDOFF.md).
Phase 4 handoff: [`development/PHASE_04_HANDOFF.md`](development/PHASE_04_HANDOFF.md).
Phase 5 handoff: [`development/PHASE_05_HANDOFF.md`](development/PHASE_05_HANDOFF.md).
Phase 6 handoff: [`development/PHASE_06_HANDOFF.md`](development/PHASE_06_HANDOFF.md).
Phase 7 handoff: [`development/PHASE_07_HANDOFF.md`](development/PHASE_07_HANDOFF.md).
Phase 8 handoff: [`development/PHASE_08_HANDOFF.md`](development/PHASE_08_HANDOFF.md).
Phase 9 handoff: [`development/PHASE_09_HANDOFF.md`](development/PHASE_09_HANDOFF.md).
Phase 10 handoff: [`development/PHASE_10_HANDOFF.md`](development/PHASE_10_HANDOFF.md).

---

## Installation

Requires **Python 3.12 or newer**.

```powershell
git clone https://github.com/sajidbuet/OMRflow.git
cd OMRflow
python -m venv .venv
.venv\Scripts\Activate.ps1          # Linux/macOS: source .venv/bin/activate
pip install -e .                    # add ".[dev]" for the development tools
```

## Running

```bash
python -m omr_scanner                  # from a source checkout
omrflow                                # console entry point
omrflow "C:/Exams/Physics Midterm"     # open a project on start-up
```

## Testing

```bash
pip install -e ".[dev]"
pytest                   # 2,000+ tests
ruff check .
mypy
```

All three must pass before a development phase is considered complete. Passing
tests are a precondition for completion, not proof of it by themselves — see
[Development Status](#development-status) above for what "complete" requires
for a given phase.

## Developer tools

Not user-facing, but the quickest way to see the alignment engine work:

```bash
# Render a synthetic sheet, distorted by a known transform
python -m omr_scanner.tools.make_test_sheet scan.png --rotate 6 --perspective 0.02 --seed 7

# Align it, print what was measured, and write diagnostic overlays
python -m omr_scanner.tools.align_image scan.png --output aligned.png --debug debug/

# Measure batch throughput on this machine at several worker counts
python scripts/benchmark_batch.py --scans 48 --workers 1,2,4,8
```

Phase 3 recognition, entirely without the GUI:

```bash
# Read one scan or a folder; write JSON results, overlays, or a full debug dump
python -m omr_scanner.tools.recognise scans/ --template sheet.omrt \
    --json-dir out/results --overlay-dir out/overlays --workers auto

# Generate a reproducible labelled test dataset from a real template
python -m omr_scanner.tools.make_dataset out/dataset --template sheet.omrt \
    --count 250 --profile mixed --seed 20260918

# ...or just the cases you are working on, as JPEG
python -m omr_scanner.tools.make_dataset out/answers --template sheet.omrt \
    --profile custom --families answers mark_styles intensity --format jpg

# See what a template offers the generator before generating anything
python -m omr_scanner.tools.make_dataset out/x --template sheet.omrt --describe

# Score recognition against that dataset, classifying every disagreement
# and reporting accuracy per kind of test case
python -m omr_scanner.tools.benchmark_recognition out/dataset \
    --template sheet.omrt --report out/benchmark --categories 0

# ...and compare a change against the run before it
python -m omr_scanner.tools.benchmark_recognition out/dataset \
    --template sheet.omrt --baseline out/benchmark/summary.json
```

Both are in the application as well, under *Tools > Developer / Testing* — see
[Developer testing tools](#developer-testing-tools-phase-3) above.

Diagnostics (the corrected page, an annotated overlay, every bubble's
measurement, and the result as JSON) come from `--diagnostics out/diagnostics`
on the command line, or *File > Settings > Diagnostics* in the application.
Both are off by default. Full detail:
[`docs/recognition_engine.md`](docs/recognition_engine.md).

---

## Repository structure

```text
OMRflow/
├── pyproject.toml            packaging + pytest/Ruff/mypy configuration
├── CHANGELOG.md
│
├── src/omr_scanner/
│   ├── main.py               entry point: CLI, logging, start-up
│   ├── errors.py             application exception hierarchy
│   ├── config/               per-user settings, platform paths and the
│   │                         CPU-worker policy (Phase 3)
│   ├── domain/               pure models: project, geometry, template,
│   │                         template_authoring (region generation, Phase 2)
│   ├── database/             SQLite schema, migrations, sessions
│   ├── services/             workflows the GUI calls: alignment, template,
│   │                         recognition, batch processing (incl. the
│   │                         multicore worker pool), filename allocation,
│   │                         scan import/export (Phase 3), calibration
│   │                         verdicts (Phase 4), batch persistence (Phase 5),
│   │                         conflict detection policy and the review/audit
│   │                         store (Phase 6), candidate list import and
│   │                         reconciliation (Phase 7), answer keys and
│   │                         scoring (Phase 8), result templates,
│   │                         readiness and report generation/persistence
│   │                         (Phase 9)
│   ├── resources/            packaged non-GUI assets: the candidate list
│   │                         sample workbook (Phase 7)
│   ├── gui/                  PySide6 window and workflow pages
│   │   ├── theme/               the design system: tokens (colours, spacing,
│   │   │                        type, shell metrics - no Qt import) and the
│   │   │                        stylesheets composed from them
│   │   ├── widgets/             the application shell's parts and the shared
│   │   │                        presentation primitives: branded header,
│   │   │                        responsive chevron workflow navigator,
│   │   │                        status footer, page header, card, empty
│   │   │                        state, action row, button roles
│   │   ├── template_designer/  interactive .omrt editor (Phase 2)
│   │   ├── calibration/         calibrate a saved template against real
│   │   │                        scans before a batch (Phase 4, testing in
│   │   │                        progress)
│   │   ├── scan/                scan/recognition workflow page, incl.
│   │   │                        benchmark mode (Phase 3, testing in progress)
│   │   ├── review/              conflict queue, review workspace and audit
│   │   │                        history (Phase 6, testing in progress)
│   │   ├── attendance/          candidate list import, reconciliation table
│   │   │                        and exception resolution (Phase 7, testing in
│   │   │                        progress)
│   │   ├── answer_key/          write, scan and verify one key per set
│   │   │                        (Phase 8, testing in progress)
│   │   ├── results/             scoring configuration, batch marking and
│   │   │                        per-question detail (Phase 8, testing in
│   │   │                        progress)
│   │   ├── reports/             per-set result templates, readiness,
│   │   │                        report layout and XLSX/PDF generation
│   │   │                        (Phase 9, testing in progress)
│   │   └── devtools/            Tools > Developer / Testing: dataset
│   │                            generation and benchmark results (Phase 3)
│   ├── imaging/              pixel algorithms: alignment, plus per-bubble
│   │                         fill-metric measurement (Phase 3)
│   ├── tools/                developer command line utilities
│   ├── recognition/          value interpretation: decide, fields (Phase 3,
│   │                         testing in progress). Its `needs_review`
│   │                         judgement is what Phase 6 turns into conflicts
│   ├── evaluation/           QA above the engine: ground-truth schema, named
│   │                         test cases, dataset planner and renderer,
│   │                         benchmark, error categories and the benchmark
│   │                         session (Phase 3)
│   ├── reporting/            XLSX/PDF report generation mechanics (Phase 9):
│   │                         `excel.py` builds the workbook, `pdf.py` is the
│   │                         LibreOffice-backed exporter abstraction. CSV
│   │                         export already existed, in
│   │                         services/scan_export.py
│   └── utils/                logging setup, atomic JSON
│
├── tests/
│   ├── unit/                 logic, domain models, layering rules
│   ├── integration/          services + database + file system
│   ├── gui/                  pytest-qt smoke tests, incl. the Scan workflow
│   └── fixtures/             test data (policy in docs/TESTING.md), incl.
│                             recognition/ - stored results for Phases 4-5
│
├── local_test_data/          where a real validation corpus goes; ignored by
│                             git except its README (Phase 3)
│
├── resources/
│   ├── templates/            illustrative .omrt example
│   └── icons/                RESERVED
│
├── examples/
│   ├── templates/            worked .omrt examples (Phase 2), incl. the
│   │                         template for the real sample sheet (Phase 3)
│   └── ECE-0000.png          the one real scanned sheet Phase 3 has been
│                             validated against
├── docs/                     architecture, data model, formats, ADRs
└── development/              roadmap, current state, phase handoffs
```

## Documentation

| Document | Contents |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Layers, dependency direction, GUI/service separation, error and logging rules |
| [`docs/DEVELOPMENT_GUIDE.md`](docs/DEVELOPMENT_GUIDE.md) | Setup, commands, conventions, where each kind of setting belongs |
| [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) | Entities across all phases and their relationships |
| [`docs/TEMPLATE_FORMAT.md`](docs/TEMPLATE_FORMAT.md) | The `.omrt` format, with a worked example |
| [`docs/template_designer.md`](docs/template_designer.md) | The interactive template designer: workflow, shortcuts, architecture |
| [`docs/IMAGE_PROCESSING.md`](docs/IMAGE_PROCESSING.md) | The alignment and recognition pipeline: algorithms, accuracy, failure modes and limits |
| [`docs/scan_workflow.md`](docs/scan_workflow.md) | The Scan / recognition workflow: importing, batch processing, renaming and CSV export (Phase 3) |
| [`docs/recognition_engine.md`](docs/recognition_engine.md) | The recognition subsystem for developers: the engine API, the `ScanResult` contract, diagnostics, synthetic datasets and the benchmark harness (Phase 3) |
| [`docs/calibration_workflow.md`](docs/calibration_workflow.md) | The Calibration workflow: procedure, overlay layers, thresholds, validation status and how to recognise a bad calibration (Phase 4) |
| [`docs/conflict_review.md`](docs/conflict_review.md) | Conflict detection and human review: what becomes a conflict, the reviewer's workflow, conflict states, provenance, the append-only audit ledger and export integration (Phase 6) |
| [`docs/reconciliation.md`](docs/reconciliation.md) | Candidate & attendance reconciliation: the four values and why they stay apart, importing a roster, the classifications, resolving an exception, and the privacy rule (Phase 7) |
| [`docs/scoring.md`](docs/scoring.md) | Answer keys and scoring: the canonical answer string, key revisions and verification, wrong questions, the negative-marking modes, exact arithmetic, staleness and recomputation (Phase 8) |
| [`docs/reporting.md`](docs/reporting.md) | Result management and reporting: why a template defines set membership, associating and validating a template, Rollwise/Meritwise/Summary/Answer-Key/Processing-Log sheets, ranking, readiness, layout configuration, PDF export and no-silent-overwrite (Phase 9) |
| [`docs/TESTING.md`](docs/TESTING.md) | Testing strategy and the test-fixture policy |
| [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) | How to use what currently exists |
| [`docs/decisions/`](docs/decisions/) | Architecture decision records |

---

## Capabilities

The following describes the finished system. Each capability is tagged with
the phase that delivers it and its current state; anything tagged **planned**
does not exist yet. Phase 3 and Phase 4 items are implemented but still
"testing in progress" in the sense described in
[Development Status](#development-status) above.

**Template designer** *(Phase 2, implemented)*. Load a reference sheet, draw
recognition zones, define the four registration markers and the orientation
marker, and configure field types: numeric, alphanumeric, set code, candidate
id, MCQ question blocks and ignored regions. Templates use
resolution-independent normalised coordinates and are stored as versioned
`.omrt` documents.

**Automatic scan alignment** *(Phase 1, implemented)*. Registration markers
drive rotation, skew, perspective and scale correction, transforming every raw
scan into the canonical coordinate system of the template. Original scans are
never modified.

**Confidence-aware recognition** *(Phase 3, implemented; testing in
progress)*. Bubbles are measured (local fill ratio against a locally estimated
paper/ink level, relative darkness among competing candidates), not merely
thresholded against a fixed value, so uncertain responses are flagged instead
of silently accepted.

**Missing and multiple marks** *(Phase 3, implemented; testing in progress)*.
Ambiguity is represented explicitly rather than guessed: an unmarked response
is blank, and multiple marks on one question are reported with both kept (for
example `B-D`), never collapsed to a single answer. A roll number or set code
that could not be read reliably is flagged rather than filed under a guessed
value.

**Multicore batch processing** *(Phase 3, implemented; testing in progress)*.
Independent sheets are read concurrently, one complete page per CPU worker
process, while the Qt interface stays in the main process and stays responsive.
The processing mode is a user setting - **Automatic** (OMRFlow chooses,
leaving the machine room to breathe), **Single core** (deterministic
troubleshooting, low-memory machines, benchmark baselines) or **Custom** (a
worker count of your own, up to the CPU threads detected). Parallel execution
never changes what is recognised: output names are assigned centrally, in batch
order, by a single allocator in the main process, so duplicate roll numbers,
the scan list and the CSV come out identically however the work was divided.

**Batch scanning, export and safe renaming** *(Phase 3, implemented; testing
in progress)*. Import one scan or a whole folder; process in the background
without freezing the GUI; export a deterministic CSV; optionally copy
processed scans into an output folder named after the detected roll number,
with duplicate rolls safely suffixed (`2103123.jpg`, `2103123_a.jpg`,
`2103123_b.jpg`, ...) so no file is ever overwritten, and unresolved rolls left
unrenamed rather than misnamed. See `docs/scan_workflow.md`.

**Template calibration & validation** *(Phase 4, implemented; testing in
progress)*. Verify a saved template against representative real scans before
running a batch, reusing Phase 3's own registration and recognition rather
than a second engine: marker/registration/bubble-geometry overlays drawn from
the engine's own coordinates, per-bubble score inspection, non-destructive
threshold tuning with immediate reclassification, and a four-state validation
verdict (Passed / Passed with Warnings / Needs Review / Calibration Failed)
that reports a mis-registered or mis-calibrated template as failed rather than
producing a confident-looking wrong result. See `docs/calibration_workflow.md`.

**Conflict resolution** *(Phase 6, implemented; testing in progress)*. A review
interface showing the original sheet, the normalised sheet, the highlighted
field, the zoomed region and the detected alternatives with their measured fill
scores. A manual correction never overwrites the machine value - it is recorded
alongside it in an append-only ledger with the reviewer's name, a timestamp and
a reason, and the final value is *derived* from the two rather than stored, so
reopening a decision restores the machine's reading without erasing the
correction that preceded it. Duplicate identifiers are detected across the whole
batch. See `docs/conflict_review.md`.

**Candidate and attendance reconciliation** *(Phase 7, implemented; testing in
progress)*. Import a candidate list from CSV or Excel, mapping its columns
rather than requiring a fixed layout, and compare registered candidates,
recorded attendance and detected scripts. Unknown roll numbers, duplicate
scripts, absent-with-script and present-without-script cases each become an
explicit, reviewable exception — never a dropped script or an arbitrarily
chosen duplicate. A resolution is recorded beside the imported and recognised
values, with the operator's name and a reason, in the same append-only ledger
Phase 6 uses. See `docs/reconciliation.md`.

**Answer keys and scoring** *(Phase 8, implemented; testing in progress)*. Keys
entered manually or read from solution sheets through the existing recognition
engine, independent per question paper set, and **verified before use** —
recognition completing is not verification. Configurable marks for correct,
incorrect, blank and multiple answers, with four negative-marking modes;
questions can be withdrawn per set for full credit. Marks are exact rationals,
never floats. Every result records the **exact key revision and scoring-policy
revision** that produced it, goes **stale** when any input changes, and is
**recomputed from stored inputs** rather than adjusted. See
`docs/scoring.md`.

**Result management and reporting** *(Phase 9, implemented; testing in
progress)*. Each set's own result/absentee workbook is **the authoritative
roster** — order, Roll No., name and existing absentee markers are its, not
recreated; Phase 7/8's stored state supplies attendance and marks. Generation
copies the template's bytes and populates a copy — **the original is never
opened for writing**, proven by SHA-256. Rollwise keeps every candidate,
absent or present, with a dynamic `RANK.EQ` formula the application's own
ranking is checked against directly; Meritwise, Summary, Answer Key and
Processing Log sheets accompany it. A readiness check blocks Final Export
while any registered/template/score disagreement stands; a Preview shows the
same issues as warnings. Existing output files are never overwritten
silently, and PDF export runs through LibreOffice, reporting plainly when it
is unavailable rather than pretending to succeed. See `docs/reporting.md`.

**Reports** *(planned, Phase 9)*. Roll-wise (including absentees) and
merit-wise Excel workbooks with a user-editable layout, plus PDF export. (CSV
export of raw recognition results already exists, from Phase 3.)

**Auditability** *(Phase 6-8, implemented; testing in progress)*. Anything that can change a result is recorded append-only: machine
value, corrected value, timestamp, operation, reviewer and reason. Phase 6
covers recognition values; attendance (Phase 7), answer keys (Phase 8) and
results (Phase 9) will record into the same ledger, which was given no foreign
key precisely so that they can.

---

## Technology

| Component | Technology |
|---|---|
| Language | Python 3.12+ |
| Desktop GUI | PySide6 / Qt |
| Image processing | OpenCV (headless), NumPy |
| Database | SQLite via SQLAlchemy 2.x |
| Validation | Pydantic 2 |
| Tabular / Excel | pandas, openpyxl |
| Testing | pytest, pytest-qt |
| Lint / types | Ruff, mypy (strict) |

Initial target platform is Windows; the application core avoids
platform-specific logic, so Linux and macOS support remains open.

## Design principles

> **Readable over clever** · **Explicit over implicit** · **Configurable over
> hard-coded** · **Modular over monolithic** · **Testable over tightly coupled**
> · **Human-verifiable over black-box automation**

OMR algorithms must never live inside GUI event handlers:

```text
GUI  ->  Service  ->  Alignment / Recognition  ->  Database
```

The layering is not merely documented - `tests/unit/test_architecture.py` fails
the build if the GUI imports OpenCV or SQLAlchemy, or if the imaging layer
imports Qt.

## Data privacy and examination integrity

OMRFlow runs locally. Candidate identities, answer sheets, attendance,
answer keys and results never need to leave the machine, and candidate data is
kept out of application logs by policy — a policy Phase 7 makes executable:
`tests/unit/test_candidate_privacy.py` drives every import and reconciliation
path with distinctive synthetic candidate data and fails if any of it reaches
captured logging.

**One exposure is known and worth knowing.** The scan pipeline logs the *file
name* of each scan it reads, which is how you tell which sheet failed. If your
scan files are named by roll number — which is exactly what OMRFlow's own
rename step produces — then your application log contains roll numbers. If
logs leave the machine, either leave renaming off or treat the log as candidate
data. Detail: [`docs/reconciliation.md`](docs/reconciliation.md) §9.

Imported candidate lists are read, never written: OMRFlow stores what it needs
in the project database and does not modify, move, rename or copy your
spreadsheet.

Successful processing is not the same as a correct result. For high-stakes
examinations, institutions should retain independent procedures for template
validation, answer-key verification, unresolved-conflict review, attendance
reconciliation, result verification, output approval and archival of original
scans. The software supports such controls; it does not replace institutional
responsibility.

Never include confidential candidate or examination data in bug reports or test
fixtures.

## Contributing

Contributions are welcome once the architecture has stabilised. Before opening a
pull request, run `pytest`, `ruff check .` and `mypy`, and read
[`docs/DEVELOPMENT_GUIDE.md`](docs/DEVELOPMENT_GUIDE.md). Please keep unrelated
architectural changes and features in separate pull requests.

### Development status maintenance

The [Development Status](#development-status) table and its testing checklist
should be updated whenever a phase is started, implemented, enters testing, or
is completed — including moving individual checklist items from unchecked to
checked only once repository evidence (a passing automated test, a documented
validation run) actually confirms them, never merely because related code or a
test file now exists. Testing status and known pending work should stay
synchronised with `development/CURRENT_STATE.md` and the relevant
`development/PHASE_NN_HANDOFF.md`, so this README remains an accurate
high-level status page rather than a second, drifting source of truth.

## License

Intended to be released as open-source software under a permissive licence
(Apache 2.0 or MIT). The final choice will be made before the first public
release.

## Developer

OMRFlow is developed by [Dr. Sajid Muhaimin Choudhury](https://www.sajid.bd).

---

## OMRFlow

**From scanned marks to verified results.**
