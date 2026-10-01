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

- optionally, **Printed on sheet as** — what the sheet's set-code bubbles
  carry for this set, when that is not its code (see below).

Sets are persistent and uniquely identified, so renaming a description does
not break anything already keyed to the set. A set that something references
cannot be deleted; OMRFlow says what is blocking it.

### Set codes ignore case, width and surrounding spaces

From the `0.1.1` development line, `a`, `A`, ` A ` and a full-width `Ａ` are
**one** set code everywhere — Project Configuration, Resolve, Attendance,
Reject & Rescan, the Answer Key stage, Results and Reports. Defining `a` when
`A` exists is refused. Codes are identifiers, not numbers: `05` and `5` stay
two different sets. The code is stored as you typed it and shown that way.

### When the sheet prints a different mark

Some examinations call their sets 10, 11, 12 while the answer sheet's set
field only offers A–D. Set **Printed on sheet as** for each set — Set `10`
printed as `A`, Set `11` as `B`. Then:

- recognition still reads what is on the paper (`A`), and that raw reading is
  kept on the scan;
- every stage after Resolve treats the sheet as **Set 10**; Resolve shows it as
  *Set 10 (A on sheet)*, and typing `10` in the full set-code editor records
  `A` on the sheet;
- a solution sheet marked `A` is accepted as Set 10's key;
- the scan-results CSV gains a `set` column (the logical set) beside
  `set_code` (the set as read).

A mark must be printable on the project's template, and may not be another
set's code or mark — otherwise a sheet reading it would name two sets.

### Projects that already contain `A` and `a`

A project created by an earlier build may define two sets that now name the
same set. OMRFlow keeps both, **never merges them**, and shows them in red in
Project Configuration; Project Health reports them by name. Until you rename or
remove one, reconciliation of their attendance, answer-key verification,
*Calculate Results* and their reports are refused, and a sheet reading the
shared code is treated as an undefined set.

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
| **0.1.1-A — Canonical set identity and printed marks** | 🟡 **Implemented; automated tests passing.** Not synthetically campaign-validated, not used with a real scanner or real paper, not production qualified |

**Part 2 awaits real-data validation**: no real attendance workbook and no
real scanned cohort has been processed end to end. It is the least proven
part of OMRFlow. See [Known Limitations](Known-Limitations) and the
[Development Roadmap](Development-Roadmap#examination-sets--a-cross-phase-enhancement).

## Related

- [Projects](Projects)
- [Attendance](Attendance)
- [Results & Reports](Results-and-Reports)
