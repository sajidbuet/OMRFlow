# Examination Sets

A **set** is one question paper within one examination. If your candidates
all sat the same paper, you have one set and can mostly ignore this page. If
they sat different papers — by post, subject or category — the division runs
through attendance, answer keys and reporting, and getting it right at the
start saves a great deal later.

## Where a set shows up

| Stage | What is per-set |
|---|---|
| **Project** | The sets are defined here: a code and a description each |
| **Template** | The sheet carries a **Set** region, so recognition reads which paper a candidate sat |
| **Scan** | Each sheet's recognised set code appears in the table |
| **Attendance** | **One attendance workbook per set** |
| **Answer Key** | **One verified key per set** |
| **Results** | Marks are computed against the candidate's own set's key |
| **Reports** | **One report per set**, built on that set's own attendance workbook |

## Defining them

**Application menu → File → Project Configuration…**

Each set has:

- a **code** — the machine-readable key, which **must match what is printed
  on the answer sheets**, because that is what recognition reads;
- a **description** — for people.

Sets are persistent and uniquely identified, so renaming a description does
not break anything already keyed to the set. A set that something references
cannot be deleted; OMRFlow says what is blocking it.

## Per-set attendance

Each set gets its own attendance workbook, on the **Attendance** stage. That
is not merely organisational: candidates who sat different papers are
different populations, with their own rosters, their own keys and their own
merit orders. A single combined roster would make "rank" meaningless.

The workbook you import for a set also becomes that set's **result
template** — see [Results & Reports](Results-and-Reports).

## Set-aware reporting

**Generate All Sets** on the **Reports** stage produces one workbook per set,
each built from that set's own template and roster. A set missing either is
refused **by name**, rather than generating something incomplete.

## Status

This is a cross-phase enhancement delivered in two parts, at different
maturities:

| Part | Status |
|---|---|
| **1 — Project configuration** (examination name, sets) | ✅ **Implemented and tested** |
| **2 — Per-set attendance and set-aware reporting** | 🟡 **Implemented; synthetic testing only** |

**Part 2 awaits real-data validation**: no real attendance workbook and no
real scanned cohort has been processed end to end. It is the least proven
part of OMRFlow. See [Known Limitations](Known-Limitations) and the
[Development Roadmap](Development-Roadmap#examination-sets--a-cross-phase-enhancement).

## Related

- [Projects](Projects)
- [Attendance](Attendance)
- [Results & Reports](Results-and-Reports)
