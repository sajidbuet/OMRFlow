# Processing

Reading imported sheets, and what to do before you trust the results.

> The detailed references are
> **[`docs/scan_workflow.md`](https://github.com/sajidbuet/OMRFlow/blob/main/docs/scan_workflow.md)**
> and
> **[`docs/calibration_workflow.md`](https://github.com/sajidbuet/OMRFlow/blob/main/docs/calibration_workflow.md)**.

## Calibrate before a real batch

**Do not skip this for real scans.** A template can be geometrically perfect
and still read badly, because how dark a "marked" bubble is depends on your
paper, your printer, your candidates' pencils and your scanner.

On the **Calibrate** stage:

1. Load the template.
2. Add several **representative real scans** — including a faint one, a heavy
   one and one with an erased mark, if you have them.
3. Run recognition in diagnostic mode. You see registration, detected versus
   expected marker positions, the bubble sampling geometry and the per-bubble
   fill scores.
4. Adjust the thresholds. Recognition updates immediately **without repeating
   registration**, so you can see the effect of a change at once.
5. You get an explicit verdict — passed, passed with warnings, needs review,
   or failed — rather than a confident-looking result from a template that
   does not actually fit.
6. Save the thresholds back into the template.

Synthetic sheets do not need this: they are rendered from the template, so
they fit it by construction. That is also why calibration cannot be validated
with them, and why the Scan stage warns when a template has not been
calibrated.

## Running a batch

On the **Scan** stage: **Process All**, or select rows and **Process
Selected**.

Processing runs in the background. The window stays responsive, and the
progress panel shows completed and total counts, percentage, elapsed time, a
smoothed estimate of the time remaining, live throughput and per-outcome
tallies.

### How many cores

**Application menu → File → Settings → Processing**: *Automatic*, *Single
core*, or *Custom* with a worker count. One whole sheet goes to each worker.

Results, output filenames and CSV row order are **identical on any number of
cores** — this is tested, not assumed. Use *Single core* if you want to rule
the pool out while diagnosing something.

## If a batch is interrupted

Nothing you saw counted is lost. Each sheet is saved **as one unit** - its
result together with the review conflicts it raises - as soon as it is read,
and the progress line counts only saved sheets (a sheet read but not yet saved
shows as *saving*). So:

- reopening the project shows the interrupted batch straight away, with
  *recognised / failed / pending* read from the database;
- **Resume Batch** continues from where it stopped, without reading anything
  already saved again;
- **Retry Failed** re-attempts only the failures.

This was tested by cancelling, by closing the window and by killing a real
OMRFlow process mid-run (0.1.1 phase 3). A real power cut was **not** tested;
it is covered only as far as SQLite and your storage are. See
[Recovery After Interrupted Processing](Recovery-After-Interrupted-Processing).

## Reprocessing

**Reprocess** re-reads sheets that already have results — after changing
thresholds, say. From the `0.1.1` development line it reads them into a **new
reprocess batch** of the same scan session and records that the new batch
**supersedes** the old one; the old batch stays, complete and inspectable.
(*Retry Failed* still re-reads failures in place, archiving the superseded
reading first.)

## Scan sessions

Every batch belongs to a **scan session** — the examination sitting. The first
*Process All* creates one automatically; a later *Process All* in the same
project adds a new batch to it, and the earlier batch is **sealed** (its list of
scans is final; it can still be resumed). The *Session* button on the Scan
stage offers New, Rename, Close, Reopen and Combine; a single-folder
examination never needs it. Attendance, Results, Reports and the Resolve
queue read the **whole session** — every batch — counting each sheet once:
a rescan replaces its original only when you confirm it, and another session
is never mixed in. Details: `docs/scan_workflow.md` §12b.

## Reading the outcome

The table shows each sheet's original file, roll number, set, status and
output filename. Click a row for the corrected sheet and the recognised
values.

Statuses and answer symbols are explained in
[Recognition Symbols](Recognition-Symbols). In short: `completed` needs
nothing, `warning` means look at it, `failed` means the page could not be
read at all — usually registration.

Anything flagged goes to the **Resolve** stage.

## Related

- [Scanning](Scanning)
- [Review & Resolution](Review-and-Resolution)
- [Recognition Symbols](Recognition-Symbols)
