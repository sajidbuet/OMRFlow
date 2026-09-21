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
