# The Scan workflow (Phase 3)

Step 2 of OMRFlow: read a stack of scanned answer sheets against a template
built in the Template designer, review what was read, optionally file the images
under their roll numbers, and export the results as CSV.

This document covers what the workflow does and the conventions it follows.
For the algorithms underneath, see [`IMAGE_PROCESSING.md`](IMAGE_PROCESSING.md);
for how the modules fit together, see [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## 1. The short version

1. **Load Template** - the `.omrt` the sheets were printed from.
2. **Add Scan(s)** or **Add Folder** - the images to read.
3. **Process All** (or **Process Selected**).
4. Click a row to review the sheet, its overlay and its recognised values.
5. Optionally tick **Rename processed scans using detected roll number** and
   choose an **Output Folder**.
6. **Export CSV**.

How many sheets are read at once is set in **File > Settings > Processing** and
is remembered between sessions; see [§10](#10-performance-and-responsiveness).

Nothing is written anywhere until step 5 or 6. Processing alone never touches a
file on disk.

---

## 2. Supported input formats

| Extension | Notes |
| --- | --- |
| `.png` | Lossless; what the sample sheet uses. |
| `.jpg`, `.jpeg` | Fine at ordinary scanner quality. |
| `.tif`, `.tiff` | Common from document scanners. |
| `.bmp` | Supported, rarely useful. |

Anything else in a folder is ignored rather than rejected: **Add Folder** picks
up the supported images and silently skips `notes.txt`, `Thumbs.db` and the
rest, so pointing it at a scanner's output directory works.

**PDF is not supported.** The brief allowed keeping it if an existing dependency
already provided it; none does, and adding one for Phase 3 was explicitly out of
scope. Convert to TIFF or PNG first.

Scans are sorted **naturally**, so `scan1, scan2, scan3, scan10` - not
`scan1, scan10, scan2, scan3`. Sheets are read in the order they are listed,
which is what makes duplicate-roll suffixes deterministic (§6).

Adding the same file twice does not duplicate the row.

---

## 3. Registration, and what happens when it fails

Every sheet is rectified onto the template's canonical page before anything is
measured: the four printed registration squares are located, the orientation
mark decides which way up the page is, and one homography corrects rotation,
translation, scale, skew and perspective together. Quarter turns (90°, 180°,
270°) are handled, as is modest arbitrary rotation.

The orientation mark is found wherever the template says it is, **including
inside another region**. It is not assumed to live in a margin.

Each sheet gets one of three statuses, shown in the scan list, in the results
panel and in the CSV:

| Status | Meaning |
| --- | --- |
| `registered` | All four markers found and the page geometry is plausible. |
| `registered_with_warning` | Rectified, but something is worth knowing - see below. |
| `failed` | The page could not be rectified. **No answers are reported.** |

A failure is never worked around. Three real corners and one invented one would
produce a plausible-looking rectification and a complete set of wrong answers,
so a missing marker is reported instead of extrapolated.

### About `MULTIPLE_CORNER_CANDIDATES`

Both the repository's real sample (`examples/ECE-0000.png`) and the synthetic
test page report this warning. It means more than one marker-like shape fell
inside a corner search region - which is normal, because printed sheets carry
other dark rectangles near their corners. The detector still scores and chooses,
and the chosen four rectify the page correctly. It is reported rather than
hidden because "the corner was ambiguous" is exactly the thing worth knowing if
a sheet later turns out to have been read wrongly.

---

## 4. What is recognised, and how uncertainty is expressed

Recognition never reduces an uncertain reading to a confident one.

### Questions

| Reported | Meaning |
| --- | --- |
| `b` | One mark. |
| `""` (empty) | No mark. |
| `b-d` | **Two marks. Both are kept**, never reduced to one. |
| `b?` | A mark too faint, or too close to its runner-up, to call. |
| `?` | Something was there but nothing could be called an answer, or the bubbles could not be measured. |

The GUI shows `?` as the status indicator and tints the row; the CSV carries the
value (`b-d`) in the question column and the sheet-level judgement in
`recognition_status`.

A multiple mark is **not** an error to be discarded. `b-d` says what is on the
paper; deciding what it is worth is the Resolve stage's job, in a later phase.

### Roll / student ID (numeric fields)

Each digit column is read independently. A field is only reported as a usable
identifier when **every** position resolved:

| Situation | Reported value | Used as a file name? |
| --- | --- | --- |
| All digits clear | `2103123` | Yes |
| One column blank | `21_3123` | No |
| One column double-marked | `21?3123` | No |
| Could not be sampled | `21?3123` | No |

Leading zeros are preserved - `00000000` is an eight-character string, never the
integer zero.

### Set code

Read from the symbols the template declares, in the template's order. It is a
**string of one or more printed positions**: `A`, `B`, `10`, `11`, `A1` are all
valid. The sample sheet's set code is `10`, and it is never reduced to `1` or to
the number ten.

### Alphanumeric / custom fields

Recognised against the symbols stored in the template. The alphabet is never
hard-coded, and symbol order is preserved exactly as the template defines it.

### How "uncertain" is decided

From measured quantities, not invented percentages: each bubble's fill ratio and
darkness, the relative darkness of the candidates in one group, the separation
between the strongest and second-strongest candidate, and the registration
quality. The thresholds live in the template's `RecognitionSettings`, never as
constants in the recognition code.

---

## 5. Roll-based renaming

Off by default. When **Rename processed scans using detected roll number** is
ticked and an output folder is chosen, each successfully recognised sheet is
**copied** into that folder under its roll number.

* The original files are never moved, renamed or modified.
* The original extension is preserved: `IMG_0034.jpg` → `2103123.jpg`.
* Nothing is written at all if renaming is off.
* Ticking it without choosing an output folder asks for one rather than
  guessing.

Copy-to-output-folder is the only mode implemented. Renaming originals in place
was left out deliberately: it is the one variant that can lose a scan.

---

## 6. Duplicate roll numbers

Two sheets can carry the same roll number - a candidate miscodes theirs, or a
sheet is scanned twice. **No scan image is ever overwritten because of it.**

| Occurrence | File name |
| --- | --- |
| 1st | `2103123.jpg` |
| 2nd | `2103123_a.jpg` |
| 3rd | `2103123_b.jpg` |
| 4th | `2103123_c.jpg` |
| ... | ... |
| 27th | `2103123_aa.jpg` |
| 28th | `2103123_ab.jpg` |

The letter part is bijective base-26, so the sequence never repeats however many
duplicates appear.

A letter rather than a number because `2103123_2.jpg` is indistinguishable from
a roll number that genuinely ends in `_2`, and from a "copy 2" produced by a
file manager. A letter cannot be mistaken for part of an identifier, sorts next
to the original, and makes "these three sheets all claimed one roll" obvious in
a directory listing.

### Files already in the output folder count too

The rule applies to names already on disk, not only to names issued during this
run. If the output folder already contains `2103123.jpg`, the next sheet with
that roll becomes `2103123_a.jpg`. If `2103123.jpg`, `_a` and `_b` are all
present, the next becomes `2103123_c.jpg`. A second run over the same folder, or
a second batch appended to an earlier one, therefore cannot replace the first
run's output.

On Windows and macOS the comparison is case-insensitive, because `2103123.JPG`
*would* overwrite `2103123.jpg` there.

### Unreliable roll numbers are not used as file names

A sheet whose roll number did not fully resolve (§4) is **not** filed under a
guessed name. It gets a review name instead:

```
UNRESOLVED_001.jpg
UNRESOLVED_002.jpg
UNRESOLVED_003.jpg
```

The scan list says why, in plain language: *"Roll number could not be determined
reliably. The scan was not renamed."* The image is still copied, so it remains
available for manual review - it is simply not pretending to be a roll number.

`UNRESOLVED` is deliberately shouty and unmistakably not an identifier: sorted
by name, these are exactly the files a human has to look at.

The naming rule is one reusable helper,
`omr_scanner.services.filename_manager.FilenameAllocator`, with its own unit
tests - it is not spread through the batch processor.

---

## 7. The scan list

| Column | Contents |
| --- | --- |
| Original file | The source image's name; the full path is the tooltip. |
| Roll | The recognised identifier, or blank. |
| Set | The recognised set code. |
| Status | `Pending`, `Complete`, `Review`, `Registration failed`, `Error`. |
| Output file | The name the image will be, or was, written under. |

The Output file column is a **preview**: it is filled in as soon as processing
decides the name, whether or not a file was written, so the effect of ticking
the rename box is visible before committing to it.

Rows are tinted by status, but the tint is never the only signal - the Status
column always says the same thing in words.

**One bad file does not stop a batch.** A corrupt or unreadable image is caught,
flagged on its own row, counted in the summary, and the run continues with the
next file.

---

## 8. Reviewing a sheet

Selecting a row shows the **rectified** page with an overlay:

* region boundaries, labelled;
* the bubbles that were read as marked;
* optionally every measured bubble, marked or not (**All bubbles**);
* uncertain and multiple marks distinguished.

The three overlay layers toggle independently. Zoom, fit-to-window, 100% and
pan (middle-drag, or space-drag) all work, and **the overlay is drawn over the
image, never burned into it** - nothing the preview does alters a pixel of the
source file.

The right-hand panel lists the recognised fields and every question with its
value and status, with rows needing attention tinted.

Manual correction of recognised values is **not** in Phase 3. The data model
already keeps the machine's reading separate from any correction, so it can be
added without disturbing the recogniser - that is the Resolve stage.

---

## 9. CSV export

UTF-8 with a byte-order mark (Excel on Windows otherwise reads a BOM-less UTF-8
CSV as the local code page and mangles every non-ASCII label - the sample sheet
is labelled in Bengali). Values are escaped by Python's `csv` module.

```
original_filename, output_filename, roll, set_code,
registration_status, recognition_status, warning_count, Q1, Q2, ... QN
```

* Question columns come from the **template**, in ascending question number, so
  two batches of the same examination always export the same columns - even if
  one of them happens to contain no answer to Q57.
* The base columns are stable; later phases and external tools read this file by
  column name.
* Export is deterministic: exporting the same batch twice produces
  byte-identical files.
* `output_filename` records the duplicate-suffixed name actually used, so the
  CSV and the folder agree.

---

## 10. Performance and responsiveness

### Watching a batch

Batches run off the GUI thread, and the Processing panel reports on them:

```text
████████████████████░░░░░░░░░░░░  63.4%

Processing OMR scans...
6,342 / 10,000 processed
Elapsed 00:18:42 · Remaining ~00:10:47
Speed 5.7 scans/sec · 12 workers · Finish ~15:42
Successful 6,301 · Review 28 · Failed 13
```

- **The bar counts finished sheets**, however they finished. A sheet that
  could not be read is a *finished* sheet: counting only successes is how a
  progress bar stalls at 97% for the rest of the afternoon while the last
  three hundred corrupted files quietly fail.
- **Remaining is an estimate**, and the `~` says so. It comes from recent
  measured throughput, not from dividing elapsed time by sheets done, so one
  slow sheet does not move it much and a genuine change of pace shows up
  within a few seconds.
- **It says "Calculating..." until it has evidence.** The first sheets of a
  multicore run are unrepresentative - worker processes are starting, OpenCV
  is loading - and an estimate from them is not cautious, it is wrong. Roughly
  ten completed sheets, or a few seconds of steady measurement, and the
  estimate appears.
- **Before the first sheet** the panel says `Preparing batch...`, because
  "0 / 10,000, remaining 00:00:00" reads like a broken bar rather than a
  starting one.
- **99.99% is not 100%.** With 9,999 of 10,000 done the bar is one short and
  says so; only every sheet reaching a terminal state finishes it.

A failed sheet never raises a dialog. It is counted in `Failed`, shown in the
scan list with a reason, and written to the log - because a thousand-sheet
batch with fifty bad files must not be fifty modal interruptions.

### Cancelling

**Cancel Processing** stops the run: no new sheet is started, and the ones
already inside a worker finish rather than being killed part-way through
writing a file. The button disables itself immediately and the panel switches
to `Cancelling batch processing...`, with the remaining time replaced by
`Cancelling...` - there is no honest estimate for "as long as the sheets in
flight take".

Everything already read is kept. The final state reports both halves:

```text
Batch cancelled.
6,342 / 10,000 processed · 3,658 not processed
Stopped after 00:18:42
```

### What a large batch costs

The template is parsed once per run, not per field. Batch runs discard the
rectified preview images (a hundred rectified pages is most of a gigabyte); the
preview for the sheet being *looked at* is recreated on demand and a handful are
cached.

Only file paths are queued - never images. A sheet is loaded when a worker
reaches it and released when it is done, so the memory a batch needs does not
grow with its length.

What *is* retained is one result per sheet, because the CSV export needs it.
A batch run therefore also declines the per-bubble evidence, which the page
never reads: on the repository's 100-question sample that is 6.7 KB per sheet
instead of 61.6 KB - about **68 MB rather than 631 MB** across ten thousand
sheets. Switching diagnostics on keeps the evidence, because the diagnostic
images are drawn from it.

The interface itself is fixed-size: one progress bar and five labels, whatever
the batch length, repainted about five times a second rather than once per
completed sheet.

### Reading several sheets at once

**File > Settings > Processing** decides how many sheets OMRFlow reads
concurrently. Each sheet is read from start to finish inside one worker - load,
register, measure, interpret - so the work divides cleanly and nothing is
shared.

| Mode | What it does | Use it when |
|---|---|---|
| **Automatic** *(default)* | OMRFlow picks the count: one logical CPU is left free for the window and the operating system, and it never exceeds 8 | normally |
| **Single core** | Exactly one sheet at a time, no worker processes at all | debugging, a low-memory machine, a benchmark baseline, reproducing a report exactly |
| **Custom** | The number you choose, from 1 to this machine's logical CPU count | you know your machine and want to cap or raise it |

The Scan page shows what the setting means for the list in front of you -
`124 scans - 8 parallel workers` - and never starts more workers than there are
scans: three sheets on a 32-thread machine use three workers, not thirty-one.

**Parallel processing never changes what is recognised.** Recognition is pure:
the same image and the same template give the same answer wherever it runs. The
scan list, the duplicate-name suffixes and the CSV all come out in scan-list
order regardless of which sheet finished first, because names are assigned - and
files copied - in the main process, in batch order, after the reading is done.
The automated suite asserts this by processing the same dataset at 1, 2 and 4
workers and comparing the exported CSVs byte for byte.

### Measured throughput

48 copies of `examples/ECE-0000.png` (2480x3508), development machine, 16
logical CPUs / 8 physical cores, Windows 11, reproduced with
`python scripts/benchmark_batch.py --scans 48 --workers 1,2,4,8,12,16`:

| Workers | Seconds | Scans/s | Speed-up | Peak RAM (all processes) |
|---|---|---|---|---|
| 1 | 13.95 | 3.44 | 1.00x | 127 MB |
| 2 | 8.81 | 5.45 | 1.58x | - |
| 4 | 5.54 | 8.67 | 2.52x | - |
| 8 | 4.13 | 11.63 | **3.38x** | 855 MB |
| 12 | 4.44 | 10.80 | 3.14x | - |
| 16 | 4.79 | 10.02 | 2.91x | 1,607 MB |

Throughput peaks at eight workers - the machine's physical core count - and
*falls* beyond it, while memory keeps rising by roughly 95 MB per worker. That
is why Automatic mode is capped at 8: past that point a batch costs more memory
and finishes no sooner. A user who knows their own hardware can still ask for
more in Custom mode.

Two caveats on these numbers. They are one machine, one sheet design and one
scanner resolution; and a *small* batch is dominated by worker start-up (each
worker is a fresh Python process importing NumPy and OpenCV), so a handful of
sheets can be slower on four cores than on one. Measure your own batch sizes
before assuming.

---

## 10b. Diagnostics, when something reads wrongly

**File > Settings > Diagnostics** writes, for every sheet processed, the scan
as loaded, the corrected page, an annotated overlay of what was decided, a
second overlay with every measured bubble labelled by its fill ratio, and the
full result as JSON - each scan in its own folder.

It is off by default and should stay off for ordinary work: it is several
full-page images per sheet, which on a real batch is gigabytes. Switching it on
does nothing until a folder is chosen, so it cannot scatter debug files into a
project.

The same thing is available without the GUI, which is usually easier when
investigating one sheet:

```bash
python -m omr_scanner.tools.recognise scan047.jpg --template sheet.omrt \
    --diagnostics out/diagnostics --verbose
```

See [`recognition_engine.md`](recognition_engine.md) for the developer-facing
detail: the result schema, the per-bubble evidence, the synthetic dataset
generator and the benchmark harness.

---

## 10c. Benchmark mode

*Tools > Developer / Testing > Run Recognition Benchmark* turns this same page
into a scoring harness against a **labelled** dataset. A banner appears, the
dataset's scans load into the ordinary scan list, and everything else works
exactly as it always does - same *Process All*, same processing settings, same
worker pool. When the run ends it is scored automatically and the results open.

Deliberately not a separate window. A benchmark of a different pipeline would
measure nothing worth knowing, and a second processing screen would be a second
place for the two to drift apart.

Two things change while benchmark mode is on: renaming is switched off (a
benchmark reads a dataset and must not rewrite its own input), and a report is
written to `<dataset>/benchmark_report/` when the run finishes, keeping the
previous run beside it for comparison.

Double-clicking a failing scan in the results selects it here, with its overlay,
which is the point of running the benchmark in this page at all.

Datasets come from *Tools > Developer / Testing > Generate Synthetic Test
Dataset*, or from `python -m omr_scanner.tools.make_dataset`. **Synthetic
results measure regression consistency and controlled edge cases, not
real-world accuracy.**

---

## 11. Known limitations

* **PDF input is not supported** (§2).
* **No manual correction** of recognised values yet (§8).
* Renaming only ever *copies*; there is no move/rename-in-place mode (§5).
* Arbitrary rotation is corrected to about ±15°, plus exact quarter turns.
  Beyond that the markers leave the corner search regions.
* Illumination gradients beyond roughly 0.45 defeat the default Otsu threshold.
* Cropping more than about 5% of the page width into the margin fails, by
  design.
* Only square registration markers are exercised.
* Multicore batches parallelise across *sheets*, never within one: a single
  scan takes as long as it always did, and a batch of one gains nothing.
* Cancelling cannot interrupt a sheet already inside a worker; with N workers,
  up to N sheets finish after Cancel is pressed.
* Validation rests on one real scanned sheet plus geometrically distorted
  copies of it, and on synthetic pages. **A larger corpus of real, independently
  filled sheets has not been processed**, so the accuracy numbers are not
  population statistics.
