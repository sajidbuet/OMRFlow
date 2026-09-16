# The `.omrt` template format

Version 1 - implemented in Phase 0 by `omr_scanner.domain.template`, loaded and
saved by `omr_scanner.services.template_service`. Phase 2 added the interactive
designer that *produces* these documents
(`omr_scanner.gui.template_designer`, `docs/template_designer.md`) plus one
additive field (`reference_image`, below); everything else on this page is
unchanged since Phase 0.

A template is a single UTF-8 JSON document with the extension `.omrt`. JSON was
chosen over a binary format so that templates diff cleanly in version control and
can be inspected or repaired with a text editor.

## Coordinate convention

Every position and length is **normalised** against the canonical page:

```text
x_normalized = pixel_x / image_width
y_normalized = pixel_y / image_height
```

- Origin `(0.0, 0.0)` is the top-left corner; `x` grows right, `y` grows down -
  the same convention as OpenCV and Qt, so no axis flipping happens at any layer
  boundary.
- Valid range is `[0.0, 1.0]`; values outside it are rejected at load time.
- Because `x` and `y` use different denominators, a single "radius" would be
  meaningless. Bubbles and markers therefore carry a `width` and a `height`.

The consequence that matters: **a template is resolution independent.** The same
document works for a 150 dpi and a 300 dpi scan, because recognition always runs
on the rectified canonical page.

## Document structure

```text
OmrTemplate
├── format, format_version, template_id, name, description
├── created_with, created_at, modified_at
├── page                    PageGeometry
├── registration_markers[4] RegistrationMarker   (one per corner role)
├── orientation_marker      OrientationMarker
├── zones[]                 Zone
│   ├── bounds              NormalizedRect
│   ├── field               numeric | alphanumeric | set_code | question_block | ignored
│   ├── grid                BubbleGrid (absent only for `ignored`)
│   ├── display_color       "#RRGGBB"
│   └── recognition         RecognitionSettings override, or null
├── recognition             RecognitionSettings (template defaults)
└── reference_image         path to the source sheet image, or null (Phase 2)
```

### `reference_image` - *added in Phase 2*

| Field | Type | Meaning |
|---|---|---|
| `reference_image` | string \| null | Path to the reference sheet image the template was designed against, **relative to the directory containing the `.omrt` file** - the same convention a project uses for scans (ADR-0002). `null` when no image is associated (a hand-written template, or one whose source image has since moved). |

This is what lets the designer reopen a template for further editing without
asking the user to relocate the source image. It is purely a designer
convenience: no code downstream of Phase 1 alignment reads it, and the image
itself is never embedded - a `.omrt` document stays small, diffable JSON.
Written by `omr_scanner.gui.template_designer.page.TemplateDesignerPage` on
every save; recomputed relative to the new location, so moving a template and
its image together (in the same relative arrangement) keeps the link intact.

Additive and optional, per the versioning rule below: a document written
before Phase 2 has no `reference_image` key and loads with the field `None`.

### `page` - PageGeometry

| Field | Type | Meaning |
|---|---|---|
| `width_mm`, `height_mm` | float > 0 | Physical paper size; reference for printing checks. |
| `canonical_width_px`, `canonical_height_px` | int > 0 | Size of the rectified image produced by normalisation. Fixes the working resolution and the aspect ratio. |

### `registration_markers` - four corner markers

| Field | Type | Meaning |
|---|---|---|
| `role` | `top_left` \| `top_right` \| `bottom_right` \| `bottom_left` | Corner in the canonical frame. All four must be present exactly once; order in the file is irrelevant. |
| `shape` | `filled_square` \| `filled_circle` \| `filled_rectangle` | Chooses the detector in Phase 1. |
| `center` | NormalizedPoint | Expected centre on the canonical page. |
| `size` | NormalizedSize | Printed extent. |
| `search_radius` | float (0, 0.5] | How far detection may look from `center` before declaring the marker missing. |

These four points define the perspective transform from a raw scan to the
canonical page.

### `orientation_marker`

Four corner markers are symmetric, so they cannot distinguish an upright sheet
from one fed upside down. One extra asymmetric marker breaks the symmetry.

| Field | Type | Meaning |
|---|---|---|
| `shape`, `center`, `size`, `search_radius` | as above | |
| `expected_near` | marker role | The corner this marker sits beside once the sheet is upright. |

### `zones`

| Field | Type | Meaning |
|---|---|---|
| `id` | string `[A-Za-z0-9_.-]+` | Stable identifier, unique in the template. Results and conflicts reference it, so renaming the label never breaks stored data. |
| `label` | string | Human readable name. |
| `bounds` | NormalizedRect | Region on the page. Must lie entirely on the page. |
| `field` | field definition | See below. |
| `grid` | BubbleGrid \| null | Required unless the field type is `ignored`. |
| `display_color` | `#RRGGBB` | Overlay colour in the designer and in diagnostic images. Named colours are rejected as ambiguous. |
| `recognition` | RecognitionSettings \| null | `null` means "inherit the template defaults". |

### Field definitions

Discriminated by `type`.

**`numeric`, `alphanumeric`, `set_code`** - one character per column:

| Field | Meaning |
|---|---|
| `symbols` | Permitted symbols in printed order, e.g. `["0" ... "9"]`. At least two, unique, non-empty. |
| `character_count` | Number of character positions (e.g. 5 digits). |
| `symbol_axis` | `vertical` (symbols run down the page, one column per character - the usual roll-number layout) or `horizontal`. |

`rows` and `columns` are derived: with `symbol_axis: vertical`,
`rows = len(symbols)` and `columns = character_count`; the two swap for
`horizontal`.

`symbols` entries are arbitrary, non-empty strings, never coerced to a
number - `"01"` stays `"01"`, and a symbol may hold more than one character
(`"10"`, `"101"`, `"*"`). This is what lets a `set_code` field describe either
of the two schemes real answer sheets use, with no extra format concept:

* **Enumerated values** (`character_count: 1`): each `symbols` entry is one
  complete printed choice, one bubble per entry - `symbols: ["A","B","C","D"]`
  or `symbols: ["10","11","12"]` are both this case. Every `set_code` region
  from before positional codes existed is already `character_count: 1`, so it
  reads back as this case unchanged.
* **Positional code** (`character_count > 1`): each character position is its
  own bubble column, e.g. `character_count: 2, symbols: ["0"..."9"]` encodes
  any two-digit code "00".."99" as two independently-marked digit columns.

**`question_block`** - a run of consecutive MCQ questions:

| Field | Meaning |
|---|---|
| `first_question` | Number printed beside the first question (1-based, as the candidate sees it). |
| `question_count` | How many consecutive questions the block holds. |
| `answer_labels` | Option labels in printed order, e.g. `["A","B","C","D"]`. |
| `symbol_axis` | `horizontal` (options run across, one row per question - the usual layout) or `vertical`. |
| `group_id` | *(added after Phase 2)* String, or `null`. Ties sibling columns generated together as one logical, multi-column Question Region - see below. |

Long papers are described as several blocks, one per printed column - this was
already true from Phase 2 onward, and is what lets each printed column be
selected, dragged and repositioned independently on the canvas without any
extra machinery: a "question column" *is* a `question_block` zone, nothing
more.

`group_id` - *(additive, optional, does not bump `format_version`; a document
written before it existed loads with `group_id: null` on every column,
exactly as if each were generated on its own)* - lets the designer offer
group-wide operations (Distribute Columns Evenly, measuring whether the
columns are evenly spaced) without depending on zone-id naming conventions.
Every column produced by one call to `generate_question_columns` or
`generate_column_array` shares one `group_id`; a column with `group_id: null`
is simply an ungrouped standalone column, still fully valid.

`column_gap` (a template-authoring parameter, not a field stored per zone -
each column's actual position is what `bounds`/`grid.origin` already record)
is defined as the **empty horizontal distance between the bounding boxes of
two adjacent columns**, never a centre-to-centre distance:

```text
next_column_x = current_column_x + current_column_width + column_gap
```

A template never stores `column_gap` itself; it stores the result - each
column's own `bounds` and `grid`, which may be perfectly evenly spaced or may
carry irregular, manually-adjusted offsets (from dragging one column by hand
to match a real, imperfectly-printed sheet). Both cases are represented
identically: independent zone geometry, nothing more.

**`ignored`** - a region deliberately excluded from recognition (logos, printed
instructions). Carries no grid.

### `grid` - BubbleGrid

OMR sheets are printed on a regular pitch, so a pitch is stored instead of
thousands of coordinates. This keeps templates small, diffable and easy to nudge
during calibration.

| Field | Meaning |
|---|---|
| `origin` | Centre of the bubble at row 0, column 0. |
| `row_pitch` | Normalised vertical distance between consecutive row centres. |
| `column_pitch` | Normalised horizontal distance between column centres (0 when the field has a single column). |
| `bubble_size` | Bounding size of one bubble; the measurement window in Phase 3. |
| `overrides` | Explicit `{row, column, center}` entries replacing the computed centre, for genuinely irregular sheets. |

Centre of cell `(row, column)`:

```text
x = origin.x + column * column_pitch
y = origin.y + row    * row_pitch
```

unless an override exists for that cell.

### `recognition` - RecognitionSettings

**Declared but not consumed before Phase 3.** Stored in the template, not in
application settings, because thresholds are only meaningful for the sheet design
and print quality they were tuned against.

| Field | Default | Meaning |
|---|---|---|
| `fill_ratio_threshold` | 0.55 | Dark-pixel ratio at or above which a bubble counts as marked. |
| `blank_ratio_threshold` | 0.25 | Ratio below which a bubble is certainly empty. Must be smaller than `fill_ratio_threshold`; values between the two are ambiguous. |
| `ambiguity_margin` | 0.12 | Minimum separation between the best and second best candidate in a group. A smaller gap raises a multiple-mark conflict. |
| `min_confidence` | 0.60 | Confidence below which a value is queued for human resolution instead of being accepted. |

## Validation rules

A document is rejected at load time when:

- `format` is not `omrflow-template`;
- `format_version` is greater than the supported version (opening a newer
  template is refused rather than guessed at);
- any coordinate lies outside `[0, 1]`, or a zone extends past a page edge;
- fewer than four registration markers are present, or a corner role is missing;
- two zones share an `id`;
- a non-ignored zone has no grid, or an ignored zone has one;
- the bubble grid overflows its zone - i.e. the centre of the last cell falls
  outside `bounds`. This catches the dangerous case where a wrong pitch would
  silently measure the neighbouring field;
- `blank_ratio_threshold >= fill_ratio_threshold`;
- `display_color` is not `#RRGGBB`;
- symbols or answer labels are empty or duplicated.

## Versioning

`format_version` starts at 1 and is incremented only for breaking changes.
Additive, optional fields do not bump it. A build refuses to open a document
with a higher version; a lower version is upgraded in memory by the loader when
that becomes necessary.

## Example

A complete, valid document is shipped at
`resources/templates/example_answer_sheet.omrt` and is validated by the test
suite, so it cannot drift from the implementation. It describes an A4 sheet with
a 5-digit roll number, a 4-option set code, questions 1-20 and one ignored
region. Abridged:

A second example, `examples/templates/100_question_4_choice_example.omrt`
(Phase 2), demonstrates a larger, more typical sheet - a 7-digit student ID, an
A-D question set and questions 1-100 in four columns of A-D choices (474
bubbles total) - built and validated by
`omr_scanner.domain.template_authoring`, with its `reference_image` pointing at
the synthetic sheet shipped alongside it.

```json
{
  "format": "omrflow-template",
  "format_version": 1,
  "template_id": "0f7d2b5e-6c2a-4c1d-9f3e-2b8a51c7d401",
  "name": "Example A4 answer sheet",
  "page": {
    "width_mm": 210.0,
    "height_mm": 297.0,
    "canonical_width_px": 1240,
    "canonical_height_px": 1754
  },
  "registration_markers": [
    {
      "role": "top_left",
      "shape": "filled_square",
      "center": { "x": 0.05, "y": 0.035 },
      "size": { "width": 0.03, "height": 0.021 },
      "search_radius": 0.05
    }
  ],
  "orientation_marker": {
    "shape": "filled_rectangle",
    "center": { "x": 0.14, "y": 0.035 },
    "size": { "width": 0.05, "height": 0.012 },
    "expected_near": "top_left",
    "search_radius": 0.06
  },
  "zones": [
    {
      "id": "roll_number",
      "label": "Roll number",
      "bounds": { "x": 0.08, "y": 0.12, "width": 0.24, "height": 0.36 },
      "field": {
        "type": "numeric",
        "symbols": ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"],
        "character_count": 5,
        "symbol_axis": "vertical"
      },
      "grid": {
        "origin": { "x": 0.1, "y": 0.15 },
        "row_pitch": 0.035,
        "column_pitch": 0.05,
        "bubble_size": { "width": 0.024, "height": 0.017 },
        "overrides": []
      },
      "display_color": "#1E88E5",
      "recognition": null
    },
    {
      "id": "questions_1_20",
      "label": "Questions 1-20",
      "bounds": { "x": 0.08, "y": 0.55, "width": 0.3, "height": 0.4 },
      "field": {
        "type": "question_block",
        "first_question": 1,
        "question_count": 20,
        "answer_labels": ["A", "B", "C", "D"],
        "symbol_axis": "horizontal"
      },
      "grid": {
        "origin": { "x": 0.11, "y": 0.575 },
        "row_pitch": 0.018,
        "column_pitch": 0.035,
        "bubble_size": { "width": 0.022, "height": 0.015 },
        "overrides": []
      },
      "display_color": "#2E7D32",
      "recognition": null
    }
  ],
  "recognition": {
    "fill_ratio_threshold": 0.55,
    "blank_ratio_threshold": 0.25,
    "ambiguity_margin": 0.12,
    "min_confidence": 0.6
  }
}
```

> The example's coordinates are illustrative. They have not been calibrated
> against a printed sheet, and no scan has ever been processed with them.

## Not in version 1

Recorded so later phases do not silently reinvent them: multi-page templates,
rotated (non-axis-aligned) zones, barcode/QR regions, handwriting regions,
per-question mark overrides, and embedded reference images.
