# Results and Reports

Turning calculated marks into the workbooks an examination office files.

> The detailed reference is
> **[`docs/reporting.md`](https://github.com/sajidbuet/OMRFlow/blob/main/docs/reporting.md)**.

## The idea

A report is not generated from a blank sheet. **Each set's own attendance
workbook becomes its result template**: OMRFlow copies it and writes into the
copy. Your column order, your fonts, your merged cells, your institution's
logo and any absentee markings you already made are what the report looks
like, because they *are* the report.

That is also why an absentee can appear on a report at all: nothing before
attendance records which set an absent candidate — who has no script — was
assigned to.

## Generating

On the **Reports** stage:

1. The set's attendance workbook is normally already bound as its result
   template. **Select Template…** to choose a different workbook.
2. **Validate** checks the template against the roster and results, and says
   what is missing rather than producing a partial report.
3. **Report Layout…** configures the header, logo, fonts and page setup.
4. **Preview** shows what will be written.
5. **Generate XLSX** writes the workbook to the project's `exports\` folder.
6. **Generate All Sets** does every set in one pass.
7. **Generate PDF** also needs **LibreOffice** installed separately. Without
   it the XLSX is still produced.

## What the workbook contains

| Sheet | Contents |
|---|---|
| **Roll-wise** | Your template, filled in: every candidate in your order, including absentees, with marks and ranks. Excel `RANK.EQ` formulas are generated dynamically rather than hard-coded to a row range |
| **Merit-wise** | A copy of the completed roll-wise sheet with **absentees removed** and the rest ordered by result |
| **Summary** | Counts and totals |
| **Answer Key** | The key used, so the report is self-documenting |
| **Processing Log** | What was recognised, what was corrected and by whom |

The merit-wise sheet is built by copying the finished roll-wise sheet, then
restoring the attributes Excel's copy operation drops and re-attaching the
images — so it keeps your formatting and your logo too.

## The Results Dashboard

The **Results** stage has two tabs. **Results** is the marking stage itself.
**Dashboard** is read-only: it analyses the same rows the Results table lists
and changes nothing. Choose **Overall Exam** or one set under *Analysis*.

| Shown | How it is calculated |
|---|---|
| Scored scripts, mean, median, mode, min, max | Final marks of *Scored* candidates only. Absent and cannot-be-scored candidates are left out, never counted as zero. One row per candidate, so duplicates, rejected and superseded sheets never count twice |
| Standard deviation, variance | Sample estimates (n − 1) |
| Q1, Q3 | Linear interpolation (as Excel `QUARTILE.INC`) |
| Histogram | Spans the whole mark scale; about 20 bins (5 marks on a 100-mark paper) |
| % correct, difficulty | Easy ≥ 80 %, Moderate 50–80 %, Difficult 20–50 %, Very difficult < 20 % (defaults in `services/result_analytics.py`) |
| Discrimination | Corrected point-biserial: correctness on the question against the number of the *other* questions answered correctly |
| KR-20, SEM | Over the 0/1 correctness matrix; SEM = SD × √(1 − KR-20), in questions correct |

A candidate is counted under their **effective** set, after any correction on
Resolve. **Question statistics are always per set.** Each set has its own
answer key and OMRFlow records no mapping between one set's question 37 and
another's, so *Overall Exam* shows marks and a set comparison, and the
question panels only when the examination has a single set. *Questions to
review* lists prompts (negative discrimination, very high or low % correct,
many blanks, a distractor chosen more than the key, a distractor nobody
chose), not verdicts. No pass mark is configured anywhere in OMRFlow, so no
pass rate is shown. If some results need recalculating the dashboard says so;
their marks are included, as they are on the Results table.

🟠 Implemented and tested with automated and scripted rendered checks on
synthetic data; not yet used by an operator on a real examination.

## Spreadsheet-injection safety

Text that came from a candidate list is treated as text, not as a formula.
A cell beginning `=` cannot be made to execute when the workbook is opened.

## Status of this area

🟡 **Set-aware reporting is implemented and tested against synthetic data
only.** No real institutional result template has been used. Check a
generated workbook against your own expectations before distributing it —
particularly the merit ordering and the exclusion of absentees. See
[Known Limitations](Known-Limitations).

## Related

- [Attendance](Attendance)
- [Answer Keys & Scoring](Answer-Keys-and-Scoring)
- [Result Workbook Format](Result-Workbook-Format)
- [Examination Sets](Examination-Sets)
