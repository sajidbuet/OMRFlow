# Attendance

Matching the scripts you processed against the candidates who were supposed
to sit the examination — and deciding what to do about every mismatch.

Attendance is **per examination set**, and the workbook you import for a set
also becomes that set's **result template**. Its layout is therefore what
your final report looks like.

> The detailed reference is
> **[`docs/reconciliation.md`](https://github.com/sajidbuet/OMRFlow/blob/main/docs/reconciliation.md)**.
> The workbook's expected shape is
> [Attendance Workbook Format](Attendance-Workbook-Format).

## Importing a candidate list

On the **Attendance** stage, choose a set under **Attendance by set**, then:

- **Download Sample Template…** writes a correctly shaped workbook to start
  from. Use it if you are unsure what OMRFlow expects.
- **Choose / Replace Attendance File…** imports your workbook (CSV or Excel)
  for the selected set.
- **Assign Existing List To Set…** binds a list you have already imported to
  another set.

A workbook with an institution title above the candidate table is fine —
OMRFlow searches for the header row rather than assuming row 1.

## Reconciling

**Reconcile** compares the selected set's candidate list against the scanned
scripts. A row of count chips shows the result; click a chip to show only
those rows, and click it again to go back to every exception.

| Status | Meaning |
|---|---|
| **Matched** | Expected present, exactly one script |
| **Missing script** | Expected present, but no script matched |
| **Absent but script found** | Marked absent, yet a script reads as this candidate - treated as a contradiction, never as proof of attendance |
| **Unknown candidate ID** | A script carries an ID that is not on this set's list |
| **Student ID not yet resolved** | Part of a script's ID could not be read |
| **Duplicate scripts** | More than one script under one ID |
| **Absent, confirmed** | Marked absent, and no script |

**Each set reconciles its own scripts.** Only scripts whose set code is the
selected set's are compared with its list; another set's scripts are simply
not part of it. A script whose set code is still unresolved - or was read as a
set this project does not define - is counted in the summary and settled on
the **Resolve** stage, then joins its set.

Every status is written as a word with a symbol beside it (✓ ⚠ ! ○); colour
is only the third way of saying it. Search matches a candidate ID, a name, or
the ID a script was **recognised** as - including the machine's reading of a
script that has since been corrected.

Replacing a set's attendance file takes effect at once: the old list's
candidates are not kept, the full path of the file is shown in the tooltip
(so two files with the same name in different folders are distinguishable),
and a workbook that was only the set's result template *because* it was the
previous attendance file stops being its template.

## Investigating

Select an exception. The right-hand pane states the problem - candidate,
attendance, scripts, and a possible explanation - and lists:

- **Scripts** - the scripts filed under this entry. **Inspect / Correct
  Script** (or **Enter** on the table) opens one.
- **Where to look** - for a *missing script*, scripts that might be the
  candidate's: unread IDs, unknown IDs, scripts filed under an absent
  candidate, duplicates, and scripts whose set code is unresolved - ranked by
  how many digits differ, allowing one missing or extra digit. Equally likely
  suggestions are marked *equally close*. For an *absent
  but script found*, the candidates expected present with no script whose ID
  is similar - the people most likely to have filled in the wrong roll
  number.

These are **suggestions only**. Nothing is reassigned until you look at the
scan and correct it.

The **Scan** section shows the **original scan** as it arrived and, on a
second tab, the sheet as it was read with the Student ID and set-code bubbles
outlined. Below it, type the **complete** Student ID or set code and press
**Apply**. Changing a position the machine read confidently asks for
confirmation first. The correction is recorded in the same review ledger the
Resolve stage uses: the machine's reading is kept, your value becomes the one
every later stage uses, the reason and a note are recorded, and **Undo
Correction** takes it back as one action. Reconciliation re-runs immediately
and the table stays where you were.

## Other decisions

| Button | Meaning |
|---|---|
| **Confirm Script Assignment** | File this script under a candidate *without* changing its Student ID |
| **Keep This Script** | For a duplicate: keep the selected copy; every other copy is rejected / excluded (you confirm, with each copy named) |
| **Reject / Exclude…** | The sheet must not take part in this exam's results (wrong form, accidental or blank scan, duplicate). Kept, never deleted; listed under **Rejected** |
| **Defer** | Decide later. Kept and marked *Deferred*; not scored or in results while deferred; you can carry on meanwhile |
| **Restore** | Return an excluded or deferred sheet to active review, exactly as it was |
| **Bring Script Back** | Only for a script *set aside* by an earlier build |
| **Override Attendance** | Record an attendance state different from the imported one |
| **Accept As-Is** | Investigated, nothing more can be done - for example, no scan was found |

Every decision is recorded **beside** the imported and recognised values,
never over them, and attributed to the operator named in *Settings*.

**Ctrl+F** jumps to the search box. **Ctrl+Down** / **Ctrl+Up** (or the
*Next unresolved* / *Previous unresolved* buttons) move to the next or
previous row that still needs review in the current view; at the end they
continue from the top and say so. The position of the divider between the
table and the detail pane is remembered.

## Why per-set

Candidates sitting different papers are different populations: they have
different rosters, different answer keys and different reports. Making
attendance per-set is what lets the rest of the pipeline stay honest about
which paper a mark came from. See [Examination Sets](Examination-Sets).

## Status of this area

🟡 **Per-set attendance and the investigation workflow are implemented and
tested against synthetic rosters and synthetic scans only.** Real
institutional workbooks, and real sheets filled in by real candidates, vary in
ways synthetic ones do not.
Check the reconciliation summary carefully on your first real import, and
please [report](https://github.com/sajidbuet/OMRFlow/issues/new/choose) a
workbook that does not import — with names and roll numbers replaced. See
[Known Limitations](Known-Limitations).

## Related

- [Attendance Workbook Format](Attendance-Workbook-Format)
- [Examination Sets](Examination-Sets)
- [Results & Reports](Results-and-Reports)
