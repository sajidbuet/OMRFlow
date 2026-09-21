# Template Design

A template is a `.omrt` document describing where everything is on your
answer sheet: the four printed registration markers, the orientation mark,
and each region of bubbles. Recognition follows the template, so **a
template that does not match the printed sheet produces confident nonsense**
— which is why the Template stage has a *Validate* button and the Calibrate
stage exists.

> This page orients you. The detailed, verified walkthrough — every toolbar
> action, the region types, the bubble-grid generation and the validation
> rules — is **[`docs/template_designer.md`](https://github.com/sajidbuet/OMRflow/blob/main/docs/template_designer.md)**.
> The `.omrt` format itself is
> **[`docs/TEMPLATE_FORMAT.md`](https://github.com/sajidbuet/OMRflow/blob/main/docs/TEMPLATE_FORMAT.md)**.

## The order of work

1. **Load a reference image** — a clean scan of a *blank* sheet, at the
   resolution you will scan the real ones.
2. **Detect** finds the four registration markers. **Confirm** accepts them.
   Adjust any that are wrong by dragging; everything else is positioned
   relative to these, so they come first.
3. **Orientation** sets the mark that tells OMRFlow which way up a page is.
4. Draw the regions:
   - **Student ID** — the candidate/roll number grid
   - **Set** — the question-paper set code
   - **Questions** — an answer block. **Array** creates a column of them and
     **Distribute** spaces them evenly, which is faster and more accurate
     than drawing each by hand
   - **Custom** — any other bubble region
   - **Reference** — a region to ignore, such as a logo or an instruction box
5. **Edit Bubbles** adjusts individual bubbles where the generated grid does
   not quite land; **Reset** regenerates the grid for a region.
6. **Bubble radius** sets the sampling radius.
7. **Validate**, then save.

Undo and redo cover the whole session.

## Coordinates are normalised

A template stores positions relative to the canonical page the registration
markers define, not in pixels of your reference image. The same template
therefore works on scans at different resolutions, and a sheet that arrives
rotated or skewed is corrected to the canonical page before anything is
measured.

## Getting started from the example

`examples/templates/100_question_4_choice_example.omrt` is a complete
working template — a 100-question, four-choice sheet. Opening it and looking
at how its regions are laid out is the fastest way to understand what the
designer expects.

## Before using a template for real

Run the **Calibrate** stage against several representative real scans. The
template being geometrically valid is necessary and not sufficient: the
recognition *thresholds* also have to suit your paper, printer and scanner.
See [Processing](Processing#calibrate-before-a-real-batch).

## Related

- [Processing](Processing)
- [Recognition Symbols](Recognition-Symbols)
- `docs/template_designer.md` — the full walkthrough
- `docs/TEMPLATE_FORMAT.md` — the file format
- `docs/IMAGE_PROCESSING.md` — how a page is normalised
