# Attendance Workbook Format

The workbook you import for a set is both its **candidate roster** and its
**result template** — OMRFlow copies it and writes results into the copy. So
its layout is your report's layout.

## Getting a correct one

On the **Attendance** stage, **Download Sample Template…** writes a correctly
shaped workbook. Starting from it is the reliable route.

## What OMRFlow expects

| | |
|---|---|
| Formats | `.xlsx` and CSV |
| Header row | **Found, not assumed.** A title or institution heading above the candidate table is fine — OMRFlow scans the first rows and scores each as a candidate header |
| Required columns | A candidate/roll identifier, and a name |
| Absentee markings | Existing markings in your workbook are recognised and preserved |
| Images and logos | Preserved through to the generated report |
| Merged cells, fonts, column widths | Preserved |

One workbook per examination set. See
[Examination Sets](Examination-Sets).

## Why your formatting survives

The report is built by copying your workbook, not by writing a new one. This
is deliberate: an examination office's result format is usually prescribed,
and a tool that reformats it creates work rather than saving it.

It is also why **Pillow** is a hard dependency — without it the Excel library
silently drops the images in a workbook it loads and saves, which would
delete your institution's logo from its own official report.

## Status

🟡 **Synthetic testing only.** Real institutional workbooks vary in ways
synthetic ones do not: several header rows, trailing totals, unexpected
columns, merged blocks. If yours does not import, please
[report it](https://github.com/sajidbuet/OMRflow/issues/new/choose) — with
every name and roll number replaced by fabricated ones, keeping the
structure, which is the part that matters.

## Related

- [Attendance](Attendance)
- [Result Workbook Format](Result-Workbook-Format)
- `docs/reconciliation.md` — the import and matching rules
