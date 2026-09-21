# Quick Start

A pass through OMRFlow's whole workflow using **synthetic data**, so you can
see what it does without needing real answer sheets. Allow about 30–45
minutes the first time.

> Everything here uses data OMRFlow generates itself. Nothing in this guide
> requires, or should be done with, real candidate information.

**Prerequisite:** OMRFlow [installed](Installation) and launched.

---

## 1. Create a project

A project is one folder holding one examination: its database, its imported
scans, and its generated workbooks.

1. On the **Project** stage, click **Create Project**.
2. Choose a parent folder and a project name, e.g. `Demo Exam`.
3. OMRFlow creates the folder and opens the project. The title bar and the
   status bar now name it.

**Project Configuration** opens automatically for a new project.

## 2. Describe the examination and its sets

In *Project Configuration* (also at **Application menu → File → Project
Configuration…**):

1. Set the **examination name** — the title that appears on reports. It need
   not be a legal folder name.
2. Define the **question-paper sets**. If your examination has one paper, one
   set is enough; give it a code such as `A`. If candidates sat different
   papers, add one set per paper.
3. Save.

Sets matter later: attendance is per set, answer keys are per set, and each
set's report is built from that set's own attendance workbook. See
[Examination Sets](Examination-Sets).

## 3. Open a template

A template tells OMRFlow where the bubbles are on the page. OMRFlow ships an
example, which is the quickest route through this guide.

1. Go to the **Template** stage.
2. Click the **open** button on the toolbar (second icon) and choose
   `examples/templates/100_question_4_choice_example.omrt` from the
   repository, if you have it.

   *Installed from the installer and have no repository checkout?* Download
   that file from
   [the repository](https://github.com/sajidbuet/OMRflow/tree/main/examples/templates)
   and open it.
3. The canvas shows the reference sheet with its regions outlined, and the
   **Regions** list on the left names them: registration markers, an
   orientation mark, Student ID, Question set, and four question blocks.
4. Click **Validate** to confirm the template is internally consistent.

To build a template from your own blank sheet instead, see
[Template Design](Template-Design) — that is the real starting point for a
real examination, and it is the stage that takes the most care.

## 4. Generate synthetic answer sheets

Rather than scanning paper, have OMRFlow render sheets it already knows the
answers to.

1. **Application menu → Tools → Developer / Testing → Generate Synthetic Test
   Dataset…**
2. Choose the same template, an output folder, and a modest count — **25
   sheets** is plenty.
3. Generate. OMRFlow writes the images plus a manifest recording exactly what
   it drew on each one.

This is also the right way to produce an attachment for a bug report: the
sheets are realistic and contain nobody's data.

## 5. Process the sheets

1. Go to the **Scan** stage.
2. **Load Template…** and select the same template. The panel names it and
   warns that it has not been calibrated against representative scans —
   expected here, because synthetic sheets are rendered from the template
   itself.
3. **Add Folder…** and choose the folder you just generated.
4. Click **Process All**.

The progress panel shows completed and total counts, elapsed time, an
estimate of the time remaining, throughput, and per-outcome tallies. The
table fills in as sheets finish: original file, roll number, set, status and
output filename.

Sheets OMRFlow could not decide confidently are marked for review rather
than guessed at. Click any row to see the corrected sheet and the recognised
values beside it.

For what the status values and answer symbols mean, see
[Recognition Symbols](Recognition-Symbols).

## 6. Resolve what recognition was unsure about

1. Go to the **Resolve** stage.

If the queue is empty, recognition was confident about everything — likely
with clean synthetic sheets. Skip to step 7.

Otherwise, for each conflict:

2. The queue lists what needs a decision. **What the machine saw** shows the
   original sheet, the corrected sheet and a zoomed view of the field in
   question.
3. Under **Your decision**, either **Accept machine value**, enter the right
   value and **Save value**, or **Defer** it.
4. A reason must be chosen, and your reviewer name must be set
   (**Application menu → File → Settings**) before a correction can be
   saved — every correction is recorded against a named person.

Nothing is overwritten. The machine's original reading is kept alongside your
decision, append-only. See [Review & Resolution](Review-and-Resolution).

## 7. Attendance

Attendance is per set, and the workbook you import becomes that set's result
template — so its layout is what your final report looks like.

1. Go to the **Attendance** stage and pick a set under **Attendance by set**.
2. Click **Download Sample Template…** to get a correctly shaped workbook,
   and open it.
3. Fill in a few fabricated candidates whose roll numbers match the synthetic
   sheets you generated. (The Scan stage's table shows the roll numbers that
   were recognised.) Mark one candidate absent.
4. **Choose / Replace Attendance File…** and select your workbook.
5. Click **Reconcile**.

The **Reconciliation summary** reports matched scripts and exceptions:
unknown candidate IDs, duplicates, candidates with no script, and absentees
that nevertheless have one. Resolve each under **Your decision** —
**Assign Script**, **Set Script Aside**, **Override Attendance** or **Accept
As-Is**.

See [Attendance](Attendance) and
[Attendance Workbook Format](Attendance-Workbook-Format).

## 8. Answer key

1. Go to the **Answer Key** stage and select the set.
2. Enter the answers. For the example template that is 100 questions; the
   synthetic dataset's manifest records what was drawn, so you can make a key
   that produces sensible marks.
   Alternatively use **Read From Solution Sheet…** to recognise a key from a
   filled-in sheet.
3. **Save As New Revision**.
4. **Verify Answer Key**. A key must be verified before results can be
   calculated — a wrong key silently mis-marks a whole cohort, so this is a
   deliberate second pair of eyes.

Under **Wrong questions (full credit for everyone)** you can mark a question
as defective; every candidate then receives credit for it.

## 9. Score

1. Go to the **Results** stage.
2. **Scoring Configuration…**: marks for a correct answer, for an incorrect
   one, and for a blank; and whether negative marking applies.
3. **Check Before Scoring** reports what is not ready — an unverified key, an
   unreconciled roster, unresolved conflicts.
4. **Calculate Results**.

The **Results summary** lists candidates with their marks and ranks. Select a
candidate to see their per-question detail; **Review Answers…** shows it
against the key. **Recalculate This Candidate** re-scores one person after a
correction.

Ranking uses standard competition ranking, so `90, 88, 88, 85` gives ranks
`1, 2, 2, 4`. See [Answer Keys & Scoring](Answer-Keys-and-Scoring).

## 10. Generate reports

1. Go to the **Reports** stage.
2. The set's attendance workbook is normally already bound as its result
   template. **Select Template…** if you need a different one.
3. **Validate** checks the template against the roster and reports what is
   missing.
4. **Preview** shows what will be written.
5. **Generate XLSX** writes the workbook — a roll-wise sheet built on your
   own template, a merit-wise sheet with absentees removed and the rest
   ordered by result, plus Summary, Answer Key and Processing Log sheets.
6. **Generate All Sets** does every set at once.
7. **Generate PDF** additionally needs LibreOffice installed; without it the
   XLSX is still produced.

Your workbook's formatting, fonts and logo are preserved — the report is
built by copying your template and writing into the copy. See
[Results & Reports](Results-and-Reports).

## 11. Find the logs and support information

- **Application menu → Tools → Create Diagnostic Bundle…** collects the
  version, platform, settings, a project health check and the log tail into
  one zip. It contains no candidate data by design, and is the right thing to
  attach to a bug report.
- Logs: `%LOCALAPPDATA%\OMRFlow\logs\` for the application, and `logs\`
  inside the project folder for that project.
- **Application menu → Tools → Project Health / Recovery…** checks database
  integrity and manages backups.

---

## What to do next

- Read [Known Limitations](Known-Limitations) before trusting any of this
  with a real examination.
- Build a template from your own blank sheet:
  [Template Design](Template-Design).
- Understand what the **Calibrate** stage is for before you run a real batch:
  [Processing](Processing). Synthetic sheets do not need it; real scans do.
- If something went wrong: [Troubleshooting](Troubleshooting).
