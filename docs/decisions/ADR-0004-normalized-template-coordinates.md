# ADR-0004: Templates use normalised coordinates and a stored bubble pitch

- **Status:** Accepted
- **Date:** 2026-09-15
- **Phase:** 0

## Context

A template describes where things are on an answer sheet: four corner markers, an
orientation marker, a roll-number grid, question blocks. Those positions have to
survive being applied to scans of different resolutions, from different scanners,
possibly years apart.

Two questions had to be settled before any template could be written:

1. In what units are positions expressed?
2. Are bubble centres stored individually or derived from a pitch?

## Decision

**Positions are normalised fractions of the canonical page**, origin top-left,
`y` growing downward:

```text
x_normalized = pixel_x / image_width
y_normalized = pixel_y / image_height
```

**Bubble centres are derived from a grid** - an origin plus a row pitch and a
column pitch - with optional explicit overrides for individual cells.

A consequence of normalising `x` and `y` by different denominators: a single
"radius" is meaningless, so bubbles and markers carry a `width` and a `height`.

## Rationale

### Why normalised rather than pixels or millimetres

- **Resolution independence.** The same template applies to 150 dpi and 300 dpi
  scans. A pixel-based template would silently break the day someone changes the
  scanner setting - one of the most likely operational mistakes.
- **It matches what the pipeline produces.** Alignment rectifies every scan to
  the canonical page declared by the template, so normalised coordinates are
  valid by construction after that step.
- **Against millimetres:** they are the natural unit for the *printed* sheet, but
  every recognition step would then need a mm-per-pixel factor that depends on
  scanner calibration - an extra source of error for no gain. Physical size is
  still recorded in `page.width_mm`/`height_mm` for printing checks.
- **Top-left origin with downward `y`** matches both OpenCV and Qt, so no layer
  boundary ever flips an axis. Axis flips are a classic source of mirrored
  results that "almost" work.

### Why a pitch rather than explicit centres

- **Sheets are printed on a regular grid.** A 200-question paper has well over a
  thousand bubbles; storing each centre makes the document large, unreadable and
  almost impossible to review in a diff.
- **Calibration becomes tractable.** Phase 4 adjusts a handful of numbers
  (origin and pitch) instead of nudging a thousand points, and a small print
  offset is corrected in one edit.
- **Overrides keep the door open.** Genuinely irregular sheets are still
  expressible through per-cell overrides, so the compact representation is not a
  ceiling.

### Validation that follows from this

Because centres are derived, a wrong pitch would silently measure the
neighbouring field - the worst kind of failure, since it produces plausible
values rather than an error. Loading therefore checks that the centre of the last
cell of every grid still falls inside its zone bounds.

## Consequences

**Positive**

- Templates are small, readable and reviewable in version control.
- A template survives scanner and resolution changes.
- One pure function, `BubbleGrid.bubble_center`, is the single definition of
  bubble geometry, shared by the future designer, the recognition engine and the
  diagnostic overlays. It is implemented and unit-tested already.

**Negative**

- Normalised numbers are less intuitive to read than millimetres. Mitigated by
  the designer (Phase 2), which will work in pixels on a displayed sheet and
  convert on save.
- A non-uniform grid needs an override entry per irregular cell; a sheet that is
  irregular throughout would be verbose. No such sheet is known; revisit if one
  appears.
- Aspect ratio is fixed by the canonical page size, so a template is tied to one
  paper shape. That is inherent to the sheet design, not an artefact of this
  decision.
