# Image processing plan

> **Status: nothing in this document is implemented.** The `omr_scanner.imaging`
> and `omr_scanner.recognition` packages contain documentation only. This file
> records the intended pipeline and the constraints it must satisfy, so that
> Phase 1 and Phase 3 start from an agreed design rather than an improvised one.

## Pipeline

```text
raw scan
  -> grayscale / preprocessing
  -> registration-marker detection
  -> orientation determination
  -> point ordering
  -> perspective transform
  -> canonical normalized sheet
  -> field extraction
  -> bubble measurement
  -> recognition
```

### 1. Grayscale / preprocessing - *Phase 1*

Convert to grayscale, suppress scanner noise, and produce a binary image
suitable for marker detection. Intended to be adaptive rather than a fixed global
threshold, because scanner illumination varies across a page and between
machines.

The original image is never modified in place; every stage returns a new array.

### 2. Registration-marker detection - *Phase 1*

Locate the four printed corner markers. The template supplies, for each marker,
the expected normalised centre, its size and a `search_radius`; detection looks
only inside that window. Searching the whole page would be both slower and more
likely to lock onto a printed logo.

Failure modes that must be reported rather than guessed around: a marker missing
entirely (torn or dirty corner), two candidates in one window, or a candidate
whose size is far from the expected one.

### 3. Orientation determination - *Phase 1*

Four corner markers are symmetric under a 180° rotation, so the corner pattern
alone cannot tell an upright sheet from an inverted one. The separate
orientation marker resolves it: after a provisional corner assignment, the sheet
is accepted only if the orientation marker appears beside the corner named by
`expected_near`. Otherwise the assignment is rotated and re-tested.

### 4. Point ordering - *Phase 1*

Assign the four detected points to the four corner roles in the canonical frame.
Ordering by angle about the centroid is stable under rotation and mild
perspective, which sorting by raw coordinates is not.

### 5. Perspective transform - *Phase 1*

Compute the homography from the four ordered detected points to their canonical
positions and warp the scan into the canonical page size declared by the
template. This corrects rotation, skew, perspective, scale and translation in a
single operation.

### 6. Canonical normalized sheet - *the contract*

The output is an image of exactly `canonical_width_px` x `canonical_height_px`
in which the template's normalised coordinates are valid.

This image is the boundary between geometry and recognition. Everything
downstream works in template coordinates, so a recognition change can never
silently depend on scanner resolution, and recognition can be tested on
synthetic canonical pages with no alignment step at all.

Aligned sheets are written to `<project>/scans_aligned` as derived artefacts:
deletable and regenerable. Originals in `<project>/scans_original` are never
touched.

### 7. Field extraction - *Phase 3*

For each zone, compute every bubble centre from its `BubbleGrid` (already
implemented as pure geometry in `omr_scanner.domain.template`) and cut a
measurement window of `bubble_size` around it.

### 8. Bubble measurement - *Phase 3*

Produce a *measurement*, not a verdict. Intended metrics per bubble: mean
intensity, dark-pixel ratio, filled-area ratio, local background intensity, and
darkness relative to the other bubbles in the same group.

Relative comparison matters: a candidate who marks lightly in pencil throughout
produces bubbles that are all faint, and an absolute threshold would read the
sheet as blank.

### 9. Recognition - *Phase 3, conflicts in Phase 6*

Turn measurements into values using the template's `RecognitionSettings`:

- above `fill_ratio_threshold` -> marked;
- below `blank_ratio_threshold` -> empty;
- in between, or a gap smaller than `ambiguity_margin` between the best and
  second best candidate -> ambiguous.

Ambiguity is preserved, never resolved by guessing. A field value carries its
confidence and its competing alternatives so Phase 6 can present them to a human.

## Constraints on the implementation

1. **No Qt in `imaging`.** The layer must run in a headless worker and in tests
   without a display. Turning an array into a `QImage` for display is the GUI's
   job.
2. **No thresholds in code.** Every tunable value comes from the template.
3. **Functions take arrays and plain data**, not an `OmrTemplate`. Passing only
   the geometry a function needs keeps it unit-testable with synthetic input.
4. **Failures raise `ImagingError`/`RecognitionError`**, never return a
   sentinel. A sheet that could not be aligned must be visible as a failure, not
   as a blank result.
5. **Diagnostics are first class.** A future diagnostic mode exposes each
   intermediate stage (grayscale, threshold, detected markers, warped page,
   bubble windows, per-bubble scores) so recognition behaviour is inspectable
   rather than a black box.

## How this will be tested

The design is driven by what can be measured (see `docs/TESTING.md`):

- **Synthetic round trip.** Render a canonical sheet, apply a *known* rotation,
  scale, translation and perspective, feed the result through the pipeline, and
  assert the recovered transform matches the applied one within tolerance. Because
  the ground truth is known, alignment accuracy becomes a number, not an opinion.
- **Degradation sweeps.** Repeat with added blur, noise, brightness shifts and
  partially damaged markers to find where the pipeline stops working - and record
  that boundary rather than discovering it during an examination.
- **Anonymised real scans.** Light pencil marks, crossed-out bubbles, scanner
  shadows and low contrast, kept as regression fixtures.
