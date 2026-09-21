# Result Workbook Format

A generated report is **your attendance workbook, filled in** — see
[Attendance Workbook Format](Attendance-Workbook-Format) for why.

Written to the project's `exports\` folder.

## Sheets

| Sheet | Contents |
|---|---|
| **Roll-wise** | Your template's layout and order, with every candidate including absentees, their marks and their ranks. Rank cells use generated Excel `RANK.EQ` formulas over the actual data range, not hard-coded rows |
| **Merit-wise** | A copy of the completed roll-wise sheet with absentees removed and the remainder ordered by result |
| **Summary** | Counts and totals for the set |
| **Answer Key** | The key used, so the workbook documents its own basis |
| **Processing Log** | What was recognised, what a reviewer corrected, and who decided |

## What is preserved from your template

Column order, fonts, merged cells, column widths, page setup, existing
absentee markings, and images including your institution's logo.

The merit-wise sheet is derived by copying the *finished* roll-wise sheet and
then restoring the attributes Excel's own copy operation drops and
re-attaching the images — so it keeps the formatting too, rather than being a
plain table.

## Configuring

**Report Layout…** on the **Reports** stage sets the header, logo, fonts and
page setup.

## PDF

**Generate PDF** requires **LibreOffice** installed separately; OMRFlow
drives it to convert the workbook. Without LibreOffice the XLSX is still
generated — only the PDF step is unavailable.

## Safety

Text originating from a candidate list is written as text, never as a
formula, so a cell beginning `=` cannot execute when the workbook is opened.

## Status

🟡 **Synthetic testing only**, and not tested at stress scale — the stress
dataset has no roster, key or result template, and inventing them would
measure a fabricated scenario. **Check a generated workbook against your own
expectations before distributing it**, particularly the merit ordering and
the exclusion of absentees.

## Related

- [Results & Reports](Results-and-Reports)
- `docs/reporting.md` — the detailed reference
