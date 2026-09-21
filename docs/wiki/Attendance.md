# Attendance

Matching the scripts you processed against the candidates who were supposed
to sit the examination — and deciding what to do about every mismatch.

Attendance is **per examination set**, and the workbook you import for a set
also becomes that set's **result template**. Its layout is therefore what
your final report looks like.

> The detailed reference is
> **[`docs/reconciliation.md`](https://github.com/sajidbuet/OMRflow/blob/main/docs/reconciliation.md)**.
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

**Reconcile** compares the roster against the scripts. The
**Reconciliation summary** reports what matched and what did not:

| Exception | Meaning |
|---|---|
| **Unknown candidate** | A script carries an ID that is not on the roster |
| **Duplicate script** | Two scripts claim the same candidate |
| **Missing script** | A candidate on the roster has no script |
| **Absent with script** | A candidate marked absent nevertheless has one |
| **Unknown or wrong set** | A script's set code is not one this project defines |

## Deciding

Select an entry under **Scripts**, then choose under **Your decision**:

| Button | Meaning |
|---|---|
| **Assign Script** | This script belongs to this candidate |
| **Set Script Aside** | Exclude it from results for now (**Put Back On The List** reverses this) |
| **Override Attendance** | Record an attendance state different from the imported one |
| **Accept As-Is** | The exception is understood and needs no change |

Every decision is recorded **beside** the imported and recognised values,
never over them, and attributed to the operator named in *Settings*.

## Why per-set

Candidates sitting different papers are different populations: they have
different rosters, different answer keys and different reports. Making
attendance per-set is what lets the rest of the pipeline stay honest about
which paper a mark came from. See [Examination Sets](Examination-Sets).

## Status of this area

🟡 **Per-set attendance is implemented and tested against synthetic rosters
only.** Real institutional workbooks vary in ways synthetic ones do not.
Check the reconciliation summary carefully on your first real import, and
please [report](https://github.com/sajidbuet/OMRflow/issues/new/choose) a
workbook that does not import — with names and roll numbers replaced. See
[Known Limitations](Known-Limitations).

## Related

- [Attendance Workbook Format](Attendance-Workbook-Format)
- [Examination Sets](Examination-Sets)
- [Results & Reports](Results-and-Reports)
