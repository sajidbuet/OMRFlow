# Result management and reporting (Phase 9)

The Reports stage turns Phase 8's stored marks into the files an examination
office actually files: a Rollwise workbook built from the office's own result
template, a Meritwise workbook, and the supporting sheets an audit expects -
per question-paper set, independently.

**Phase 9 reads. It never re-derives.** Every mark on a report comes from a
Phase 8 `CandidateResult`; every attendance fact comes from Phase 7's
reconciliation. If a mark or an attendance status looks wrong on a report, the
fix is on the Results or Attendance stage, and the report is regenerated - see
[§9](#9-regenerate-never-patch) below.

---

## 1. Why a report needs a template per set

OMRFlow's recognition pipeline only knows which set a candidate sat once their
script has been scanned and read. An absentee has no script, and therefore no
recorded set at all anywhere in Phases 1-8's data. For an absentee to appear on
*Set A*'s Rollwise report - which the sample template and every real result
sheet require - something has to say "this roll number belongs to Set A"
independently of any script.

That something is the office's own result/absentee template. Each set's
template is the authoritative roll-number roster for that set, present and
absent alike - which is also why §8 of the phase brief makes the template
authoritative for order, Roll No., name and existing absentee markers, and
Phase 7/8 authoritative for *whether* a candidate sat and *what* they scored.

## 2. The canonical answer string is unrelated

Nothing here reinterprets a scan or an answer key. A generated report shows
exactly the `CandidateResult.final_score` Phase 8 already computed and stored;
Phase 9 has no scoring logic of its own to disagree with it.

## 3. Associating a template

On the Reports stage, select a set, then **Select Template...** and choose an
`.xlsx` workbook. OMRFlow:

1. Finds the most likely worksheet, scanning the first fifteen rows for one
   that names a Roll No., Marks or similar column - so an institution's own
   title rows above the header do not stop it being found.
2. Suggests which column is which (Sl.No., Roll No., Name, Marks, Merit).
   **Roll No. and Marks are never guessed when ambiguous** - two equally
   plausible columns stop the dialog and ask, because a wrong guess on either
   would misattribute a mark, not merely a label.
3. Shows a preview of the real file so the choice can be checked against what
   is actually in it.

The association is stored per set; **Change Template** replaces it. Nothing
about the template is copied into the project - the file path and its
SHA-256 hash are recorded, so a report can always say which file it came from
and whether it changed.

### 3a. Usually you do not associate one by hand

Since per-set attendance, assigning a Set's attendance workbook in the
**Attendance** stage *also* makes that workbook the Set's result template
(`source_kind = "attendance"`). That is the intended route: the office's
attendance sheet already carries the heading, the post, the widths, the
borders and the logo that the result must carry, so the result is built by
copying it.

**Choosing a template here remains available**, and is what an operator does
when the attendance file cannot serve as one. The policy is explicit, never a
silent substitution:

| The Set's attendance file | Becomes the result template? | If not, what happens |
|---|---|---|
| `.xlsx`/`.xlsm` with a marks column | Yes, automatically | - |
| `.xlsx` with **no** marks column | No | The Set reports that there is nowhere to write a mark; add the column, or choose a different template here |
| `.xlsx` whose Roll No. or marks column is ambiguous | No | The Set reports it; choose the workbook here, where the mapping dialog asks |
| `.csv` | **No - a CSV has no layout** | The CSV is still fully used for candidates and reconciliation; a result needs an `.xlsx` template chosen here |

A Set with no template is refused generation by name
(`"No attendance/template workbook has been assigned to Set 11."`). It never
falls back to another Set's workbook, to the most recently used one, or to a
generic sheet.

## 4. Validating and previewing

**Validate** runs every readiness check (§8) and shows every issue at once -
never one dialog at a time. **Preview** generates a draft workbook where every
issue is a warning rather than a block, so an operator can see what a
candidate row will look like while still fixing what needs fixing.

## 5. Rollwise

Generation:

1. Copies the associated template's bytes to a new output file. **The
   original template is never opened for writing.**
2. Populates the marks and Merit/Rank cells in place, at exactly the row the
   template's own Roll No. is on - candidate order, names, serial numbers and
   any other formatting the template already has are untouched.
3. Writes a `RANK.EQ` formula into the Merit cell for every present, scored
   candidate - not a hard-coded number - so the workbook itself proves the
   rank rather than merely stating it.

### Absent candidates

An absent candidate's marks cell reads `ABSENT`; their Merit cell reads `---`,
both as literal text and as what the row's own formula evaluates to. They are
never dropped from the sheet.

### Present candidates

The marks cell holds the exact `final_score` as a number - never text, never
rounded beyond the two decimal places Phase 8's own display already uses.

## 6. Ranking

Standard competition ranking ("1224"): `90, 88, 88, 85` ranks `1, 2, 2, 4`.
Absent and unresolved candidates never participate. The application computes
the same rank independently of the Excel formula
(`omr_scanner.domain.reporting.compute_ranks`), and both are checked against
the same worked examples the phase brief gives - the formula is the report's
own representation, not the only place the number exists.

## 7. Meritwise, Summary, Answer Key, Processing Log

Every generated workbook also carries:

* **`meritwise`** - **a copy of the completed Rollwise sheet**, not a freshly
  built table: the institution heading, the post name, the logo, the column
  widths, the borders, the row heights and the page setup all come with it,
  and only the row membership and the row order differ. Absentees and anyone
  without a decided mark are removed; the rest are ordered by mark descending
  and Roll No. ascending as a deterministic tie-break, the serial column is
  renumbered 1..N, and the rank column is rewritten as `RANK.EQ` over this
  sheet's own row range - so **tied candidates still share a rank**, and the
  row order never implies a merit difference that does not exist.

  Rows freed by removing absentees are blanked in place rather than deleted,
  which keeps anything printed *below* the table - a signature line, a
  footer - exactly where the template put it. A merged cell spanning several
  **candidate** rows cannot survive reordering and is reported as a warning
  on the generation; merges in heading rows are unaffected.
* **Summary** - registered/present/absent/scored/unresolved counts, the
  maximum/highest/lowest/mean/median score, the scoring configuration in
  force, the answer-key revision, and when it was generated. A statistic that
  cannot be computed shows `N/A`, never a fabricated value.
* **Answer Key** - the exact stored key for *this set*, at the revision it was
  scored under. Never another set's key.
* **Processing Log** - the generation audit: counts and hashes, never a
  candidate name, Roll No. or answer string.

## 8. Readiness

Before a set's report can be finally exported, OMRFlow checks:

* a template is associated and readable;
* the set has a verified answer key;
* every template row matches a registered candidate, and vice versa;
* no duplicate Roll No. in the template, and no duplicate registration;
* the template's absence marker agrees with reconciliation;
* every present candidate has a final score;
* no unresolved reconciliation exception remains.

Every failing check is a message naming what is wrong - never a stack trace.
**Final Export** refuses while any of these stand; **Preview** shows them as
warnings instead.

## 9. Regenerate, never patch

Changing the answer key, the scoring configuration, an effective answer, the
associated template or the report layout does not touch a previously
generated file. The next generation reads Phase 7/8's *current* stored state
and writes a fresh workbook; nothing here opens an old report and edits a
cell. This is the same rule Phase 8's scoring engine follows, for the same
reason: a patched number is not one anybody can defend later.

## 10. Report layout

**Report Layout...** configures, per project or per set:

* header/subtitle/examination-name/footer text - only written into blank rows
  the template already has above its own header, and skipped with a warning
  (never by disturbing a candidate row) if there is no room;
* a logo, size-bounded and never distorted;
* font family and sizes;
* page size, orientation, margins, fit-to-width/scale, centring, and whether
  the header row repeats on every printed page.

Every field left at its default changes nothing - the default is always to
keep the template exactly as supplied.

### The marks-column header

If the scoring configuration's maximum score does not match what the marks
column header already says (`Total (90)` on a paper now marked out of 100),
OMRFlow corrects the header text automatically unless that is switched off, so
a report never silently claims the wrong maximum.

## 11. PDF export

PDF export uses [LibreOffice](https://www.libreoffice.org/)'s headless
conversion (`soffice --headless --convert-to pdf`), which is what actually
recalculates the `RANK.EQ` formulas before rendering - Python spreadsheet
libraries never evaluate a formula themselves. **Microsoft Excel is not
required.** If LibreOffice cannot be found, XLSX generation still works and
PDF export reports plainly that it is unavailable, naming what to install.

PDF export always regenerates the XLSX first, then exports exactly one sheet
(Rollwise or Meritwise) from that fresh copy - never a stale file, and never a
PDF mixing both reports' rows.

## 12. Never overwrite silently

A generated file's name follows `<Project>_<Set>_<Report>.xlsx` (or `.pdf`).
If that name already exists, OMRFlow writes `..._1`, `..._2`, and so on -
**the existing file is never replaced without an explicit choice to do so.**

## 13. Per-set isolation

Every set's template, key, scoring inputs and generated workbook are kept
separate by the set code throughout - there is no code path that falls back
to "the current key" or "the current template" when a set's own is missing;
missing means blocked, not borrowed from elsewhere. See the phase's per-set
isolation tests for the specific defect this guards: a deliberately different
key for each of two sets, checked to prove neither's candidates can end up
scored, or reported, against the other's paper.

### 13a. …and per-set candidates

Since per-set attendance, isolation extends to the candidates themselves.
Each set has **its own roster**, and everything that identifies a candidate,
a reconciliation state or a stored mark has always hung off a roster:

```text
project_set (set_id) -> candidate_roster (set_id) -> registered_candidate
                                                  -> reconciliation_entry
                                                  -> candidate_result
```

So Roll `10001` can be registered in Set 10 *and* Set 11 and mean two
different people with two different marks. `report_store.generate_for_set`
resolves both the roster and the template from the `set_id` - not from the
code, and not from any project-wide "current" value - so there is no reachable
path by which one set's candidate list or mark can reach another set's
workbook. `tests/integration/test_multi_set_reports.py` asserts exactly that,
including on a roll deliberately shared between two sets with a different mark
in each.
