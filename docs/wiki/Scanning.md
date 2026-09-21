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
