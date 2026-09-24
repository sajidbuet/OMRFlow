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

Nothing is lost. Every sheet's result is committed as it finishes, so:

- **Resume Batch** continues from where it stopped, without reprocessing
  anything already done.
- **Retry Failed** re-attempts only the failures.

This holds for a cancellation, a crash, a power loss or closing the window.
See
[Recovery After Interrupted Processing](Recovery-After-Interrupted-Processing).

## Reprocessing

**Reprocess** re-reads sheets that already have results — after changing
thresholds, say. The superseded reading is **archived before** the sheet is
reset, so a sheet processed twice keeps both readings on record.

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
