# User Guide

OMRFlow's nine stages, in the order you work through them. Each page below
covers what the stage is for, what it needs before it can do anything, and
what it produces.

If you have not used OMRFlow before, start with the
[Quick Start](Quick-Start), which walks the whole workflow using synthetic
data.

## The stages

| | Stage | Page | Needs |
|---|---|---|---|
| 1 | Project | [Projects](Projects) | Nothing |
| 2 | Template | [Template Design](Template-Design) | A blank answer sheet, scanned |
| 3 | Calibrate | [Processing](Processing#calibrate-before-a-real-batch) | A template and a few representative real scans |
| 4 | Scan | [Scanning](Scanning) · [Processing](Processing) | A template and scanned sheets |
| 5 | Resolve | [Review & Resolution](Review-and-Resolution) | A processed batch |
| 6 | Attendance | [Attendance](Attendance) | A candidate list, per set |
| 7 | Answer Key | [Answer Keys & Scoring](Answer-Keys-and-Scoring) | The key for each set |
| 8 | Results | [Answer Keys & Scoring](Answer-Keys-and-Scoring#scoring) | A verified key and a reconciled roster |
| 9 | Reports | [Results & Reports](Results-and-Reports) | Calculated results and a result template per set |

Stages 6 and 7 are independent of each other and can be done in either
order, or while a batch is still processing.

With several scanners feeding one sitting, the Scan stage can watch their
folders and read new sheets as they arrive while you resolve on stage 5
([session mode](Scanning#several-scanners-at-once-session-mode) - in
development, not yet used by an operator). The session's name appears in the
headers of Resolve, Attendance, Results and Reports, and their figures are
**provisional** until you finish the scan session.

## Moving between stages

Everything above your work is one row at the top of the window, which is also
the window's title bar:

- **Click a stage** in the workflow ribbon to open it.
- **`‹` and `›`** move one stage back or forward. They stop at the ends, and
  at a stage the workflow has locked — they never skip past one.
- **Arrow keys, Home and End** move focus along the ribbon without changing
  the page; **Space** or **Enter** then opens the focused stage.
- **When the window is narrow** the ribbon shows only the stage you are on.
  Hover it, click it, or press **Down** to get a list of all nine; **Escape**
  closes the list. No stage is ever unreachable.
- **`−` and `+`** make the whole interface smaller or larger — text, buttons,
  icons, the ribbon, the footer and every dialog — from 80 % to 200 % in steps
  of 10 %. They are the same commands as **Application menu → View → Zoom −**
  and **Zoom +**; **View → Zoom 100%** goes back to the normal size. OMRFlow
  remembers your choice. This is not the sheet zoom: the Template, Calibrate,
  Scan and Resolve stages keep their own image zoom (Ctrl++, Ctrl+−, Ctrl+0 in
  the Template Designer), and changing the interface zoom does not change it.
- **If not all nine stages fit on your screen**, use **View → Ribbon Density →
  More Compact** to give each workflow step less room, or a smaller interface
  zoom. The ribbon scrolls or shows just the current stage when they still do
  not fit.
- **At a large zoom on a small screen**, a stage that no longer fits gets
  scroll bars rather than overlapping controls.
- **Drag the window** by the OMRFlow logo or the empty space in the row, and
  double-click there to maximise or restore it.

The footer says which build you are running and which examination is open.

## Things that apply everywhere

**Your reviewer name.** Set it in **Application menu → File → Settings**.
Corrections and reconciliation decisions are recorded against a named
person, and cannot be saved until it is set.

**Processing settings.** Also under *Settings*: how many CPU workers batch
recognition may use (*Automatic*, *Single core* or *Custom*). It is a
property of the machine, not of the examination, so it is remembered between
projects.

**Project health.** **Application menu → Tools → Project Health /
Recovery…** checks database integrity, foreign keys, stale jobs, missing
source scans and unresolved exceptions, and manages backups. It reports and
never silently repairs.

**Diagnostics.** **Application menu → Tools → Create Diagnostic Bundle…**
produces a support bundle containing no candidate data.

## Examination sets

If your examination is divided into question-paper sets, that division runs
through attendance, answer keys and reporting. Read
[Examination Sets](Examination-Sets) before setting up a real examination
with more than one paper.
