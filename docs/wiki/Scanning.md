# Scanning

This page is about getting images *into* OMRFlow. Reading them is
[Processing](Processing).

## Scanner settings

OMRFlow has no preferred scanner. What matters is the image:

| | Recommendation |
|---|---|
| Resolution | **150–300 dpi.** Lower loses bubble definition; higher mostly costs time and disk |
| Colour | Greyscale is enough. Colour is accepted |
| Format | PNG, JPEG, TIFF and the other formats OpenCV reads |
| Page | **The whole sheet, including all four registration markers.** A cropped edge is the most common cause of a sheet failing to register |
| Orientation | Any. OMRFlow detects which way up a page is from the orientation mark |
| Skew | Tolerated and corrected. Feed the sheets straight anyway |
| Compression | Avoid heavy JPEG compression; the artefacts land on the bubble edges |

Use the same resolution for the reference image in your template and for the
real scans. It is not strictly required — coordinates are normalised — but it
keeps the calibration you tuned meaningful.

## Importing

On the **Scan** stage:

1. **Load Template…** — the template these sheets were printed from. The
   panel names it and warns if it has not been calibrated since it last
   changed.
2. **Add Scan(s)…** for individual files, or **Add Folder…** for a whole
   folder. A folder is read in natural order, so `sheet2` comes before
   `sheet10`.
3. **Clear Scan List** empties the list without touching the files.

The batch you process joins the project's **scan session** (created by the
first *Process All*, named in the Scan stage header). Scans added later go into
a **new batch of the same session**; a rescan for a sheet whose batch is sealed
goes into a new *rescan* batch. See [Processing](Processing#scan-sessions).

If OMRFlow was closed or stopped part-way through a batch, reopening the
project brings the Scan stage back on that batch - the same session, the same
batch, its saved results and what is still pending - before you press
anything; **Resume Batch** reads only what is left. See
[Recovery After Interrupted Processing](Recovery-After-Interrupted-Processing).

## Several scanners at once: session mode

> In development (`0.1.1`, branch `feat/0.1.1-phase8-operational-gui`, not
> released). Tested by automated GUI tests and scripted local runs only - not
> yet used in a scanning room, on a network share or with a real scanner.

Nothing above changes for a single, finite set of scans. When several scanners
feed one examination, **Session → Add Scanner Source…** adds a scanner's
output folder (a local folder or a typed / pasted network path such as
`\\scan-pc-2\out`) to the scan session, and the Scan stage shows the
**session panel**:

- what the session is doing - *Processing*, *Caught up - watching for new
  scans*, *Waiting for Scanner B (unreachable since 10:42)*, *Processing
  paused*, *Intake paused*. **Caught up does not mean finished**: the session
  stays open until you choose *Finish Scan Session*;
- three progress lines - *Recognition*, *Conflicts*, *Rescans* - never one
  percentage; recognition can go *down* when new sheets arrive;
- live counts, warnings (for example a scanner whose sheets mostly fail to
  align - check its alignment and template; an uncalibrated warning, not a
  diagnosis), and a folded **Sources** table with each scanner's state;
- **Start Continuous Scan**, **Pause / Resume Processing**, **Stop → Finish
  Current and Stop** (the normal safe stop) or **Cancel Queued Work…**,
  **Pause / Resume Intake**, **Finish Scan Session…** (lists everything that
  still blocks closing, with *Go to …* buttons) and **Reopen Session…**.

The list under the preview becomes the whole session, a page at a time, with
filters by status, scanner, batch, quality, conflict and rescan state. Sheets
are named by the file name they arrived with.

Closing OMRFlow while scanning stops safely and leaves the session **open**;
reopening the project shows exactly where it stopped before anything starts.
Details: `docs/scan_workflow.md` §12c.

## Duplicate and changed scans

OMRFlow hashes each imported image's contents. It can therefore tell you
that:

- you have imported **the same image twice**, even under a different name;
- a source image that was processed earlier is now **missing**;
- a source image has **changed** since it was read.

Scans are referenced rather than copied into the project, so keep the source
folder. If you move it, see [Moving Projects](Moving-Projects).

## Output filenames

With renaming enabled, a recognised sheet is copied to an output folder named
by its roll number. **An existing file is never overwritten** — a collision
gets a distinct name and is reported, because two sheets claiming the same
roll number is exactly the kind of thing you need to be told about.

## Exporting

**Export CSV** writes the batch's recognised values, including the
[symbols](Recognition-Symbols) for blank, multiple and uncertain marks, so
nothing is silently flattened.

## Related

- [Processing](Processing)
- [Recognition Symbols](Recognition-Symbols)
- `docs/scan_workflow.md` — the detailed reference
