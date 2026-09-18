# OMRFlow GUI test scenarios

Concrete scenarios against the repository's real OMR page. Each names the
invariant to assert, where an automated test for it already lives, and what to
capture when working by hand.

## The sample

`examples/ECE-0000.png` - a real scanned sheet, 2480 x 3508 px.

| Feature | Measured position (image px) |
| --- | --- |
| Top-left registration square | `(81, 229, 44, 44)` |
| Top-right registration square | `(2343, 236, 43, 45)` |
| Bottom-left registration square | `(70, 3169, 45, 42)` |
| Bottom-right registration square | `(2332, 3183, 44, 42)` |
| **Orientation dash** | `(158, 309, 86, 44)` - a 1.95:1 horizontal bar |
| Question layout | 5 columns of 20, Q1-100, choices a/b/c/d |

Markers are printed in **magenta**, not black - grayscale mid-tone, which is why
the orientation detector thresholds its own ROI rather than reusing the page-wide
binarisation.

**Rules.** Never modify, crop, resize or overwrite the file. Never hard-code
these numbers into `src/` - they are ground truth for *assertions*, not hints for
the detector. Resolve the path with `pathlib` from a fixed anchor, never from the
working directory.

## Stable object names

Prefer these over widget-tree walking or scene indices.

| `objectName` | Widget |
| --- | --- |
| `template_toolbar_file` | Toolbar row 1 (`page.toolbar`) |
| `template_toolbar_regions` | Toolbar row 2 (`page.toolbar_view`) |
| `bubble_radius` | Radius spin box - toolbar row 2, properties panel, and every region dialog |
| `template_properties_panel` | The properties panel |
| `geometry_x`, `geometry_y`, `geometry_width`, `geometry_height` | Numeric geometry fields |
| `bubble_geometry_group`, `bubble_inherit` | Per-region bubble controls |
| `auto_pitch` | "Fit bubble spacing to the region" checkbox |
| `action_*` | Every toolbar action, from its label (`action_student_id`, `action_validate`, ...) |

Scene items are keyed by domain id: `canvas._scene.region_items["questions_0"]`,
`"marker:top_left"`, `"orientation"`.

### Scan page (Phase 3)

| `objectName` | Widget |
| --- | --- |
| `scanPage` | The page itself |
| `loadTemplateButton`, `templateNameLabel` | Template group |
| `addScansButton`, `addFolderButton`, `clearScansButton` | Scans group |
| `processAllButton`, `processSelectedButton`, `reprocessButton`, `cancelButton` | Processing group |
| `workersLabel` | "124 scans - 8 parallel workers", above the Process buttons |
| `progressBar`, `progressLabel` | Progress reporting |
| `renameScansCheckBox`, `outputFolderButton`, `outputFolderLabel` | Output group |
| `exportCsvButton` | CSV export |
| `scanTable` | The scan list: Original file, Roll, Set, Status, Output file |
| `scanPreview` | The `ScanPreviewView` (a `QGraphicsView`) |
| `scanPreviewToolbar` | Zoom/overlay toolbar above the preview |
| `previousScanButton`, `nextScanButton` | Step through the list |
| `zoomInButton`, `zoomOutButton`, `fitButton`, `actualSizeButton` | Preview zoom |
| `overlayZonesCheckBox`, `overlayBubblesCheckBox`, `overlayEmptyCheckBox` | Overlay layers |
| `previewStatusLabel` | Page size and registration status |
| `resultFieldsTable`, `resultAnswersTable`, `resultSummaryLabel` | Recognised values panel |

### Settings dialog (Phase 3, multicore)

| `objectName` | Widget |
| --- | --- |
| `settingsDialog` | The dialog itself (`File > Settings...`, `settingsAction`) |
| `processingSettingsGroup` | The Processing section |
| `processingModeCombo` | Automatic / Single core / Custom |
| `workerCountSpinBox` | Parallel workers; range is `1 .. detected CPU threads` |
| `detectedCpuLabel`, `activeWorkersLabel` | The two computed read-outs |
| `processingExplanationLabel` | One sentence explaining the selected mode |

Construct `SettingsDialog(config, cpu_count=N)` with an explicit `cpu_count` -
the offered range depends on the machine, and fixing it is the only way an
assertion about that range means the same thing on two computers. Never
`exec()` it.

**Never sleep waiting for a batch.** `ScanPage.batch_finished` carries the
`BatchReport`; wait on it (`qtbot.waitSignal`, or `ScanHarness.run_batch()` in
the scripts). Selecting a row starts a *separate* `PreviewWorker`, so anything
that measures or captures the preview must also wait for
`ScanPreviewView.has_page` - note it is a **property**, not a method.

---

## Scenario 1 - Load the sample

Load `examples/ECE-0000.png` into the Template page.

**Verify:** the load succeeds; `canvas._image_size == (2480, 3508)`; the status
row reports that size; Fit and 100% both work; the canvas is visible.

**Automated:** `tests/gui/test_template_designer_page.py` (synthetic sheet);
`scripts/run_gui_smoke_tests.py` (the real sample).

---

## Scenario 2 - Question region container invariance

Draw a Question Region over the question area. Record `x, y, width, height` of
the *union* of its column zones.

Change **Columns 4 -> 1**, then **1 -> 5**.

**Invariant:**

```
outer x       unchanged
outer y       unchanged
outer width   unchanged
outer height  unchanged
```

...**and** the internal layout did change: a different number of zones, different
strip widths, different grid origins. Assert both halves.

**Automated:** `tests/unit/test_question_region_container.py` (model),
`tests/gui/test_template_designer_bubble_and_layout.py::TestQuestionRegionContainerThroughTheDialog`
(canvas items).

**Capture:** `template_ece0000_question_1column.png`,
`template_ece0000_question_5columns.png`.

---

## Scenario 3 - Bubble radius

With a Question Region selected, set the toolbar radius to 4, then 8, then 12 px.

**Verify:**

* the rendered overlay bubble diameter is `2 x radius` image pixels;
* the stored `grid.bubble_size` changes to match;
* every bubble **centre** is unchanged;
* the parent region does not move or resize;
* zooming does not alter any of it.

**Automated:** `tests/gui/test_template_designer_bubble_and_layout.py::TestBubbleRadius`;
`tests/unit/test_question_region_container.py::TestSetZoneBubbleSize`.

**Capture:** `template_ece0000_bubble_radius_small.png` (4 px) and
`..._large.png` (12 px) - the difference must be obvious side by side.

---

## Scenario 4 - Region resize anchoring

Place a region well away from `(0, 0)`. Select it. Drag the **bottom-right**
handle.

**Invariant:** `x` unchanged, `y` unchanged, width changed, height changed.

Dump model geometry, `item.pos()`, `boundingRect()` and `sceneBoundingRect()`
before and after; all four must agree on the position.

This scenario exists specifically to prevent the top-left-jump bug recurring.

**Automated:** `tests/gui/test_template_designer_region_geometry.py::TestResizeAnchoring`
- covers every handle, and asserts the region never lands near the origin.

**Capture:** `template_ece0000_region_before_resize.png`, `..._after_resize.png`.

---

## Scenario 5 - Numeric resize

In the properties panel change **Width** only.

**Invariant:** `x`, `y`, `height` unchanged.

Then change **Height** only: `x`, `y`, `width` unchanged.

**Automated:** `tests/gui/test_template_designer_bubble_and_layout.py::TestNumericResizePreservesPosition`.

---

## Scenario 6 - Orientation marker ROI

Position the orientation region's rectangle around the real dash and run
**Orientation** on the toolbar.

**Verify:** the search happens *inside* the rectangle; the dash is found; the
result is in full-image coordinates (`~(158, 309, 86, 44)`, not an offset within
the crop); the overlay lands on the printed mark.

A valid mark wholly inside the rectangle must never be rejected for being inside
it - that is the rectangle's entire purpose.

If detection fails, set `OMRFLOW_ORIENTATION_DEBUG_DIR` and re-run; the overlay
shows every candidate and why each was rejected.

**Automated:** `tests/integration/test_orientation_marker_detection.py` - seven
synthetic scenarios plus six ROI shapes on the real sheet.

**Capture:** `template_ece0000_orientation_roi.png`, `..._orientation_detected.png`.

---

## Scenario 7 - Question column array

Calibrate one question column, then Create Column Array for Q1-100 in 5 columns
of 20.

**Verify:** 5 zones; 100 questions, each covered exactly once; 400 answer
bubbles. Then move one column by hand and confirm the other four do not move.

**Note:** the array gesture uses `ColumnLayoutMode.FROM_PITCH` and *does* extend
past the reference rectangle - that is the point of "make N more like this one",
and is not a container-invariance violation. Scenario 2 covers the container.

**Automated:** `tests/unit/test_template_authoring.py::TestGenerateColumnArray`,
`tests/gui/test_template_designer_page.py::test_create_array_generates_five_columns_in_one_undo_step`.

---

## Scenario 8 - Column gap

Change the column gap in the Question Block dialog.

**Verify:** column positions change; the parent region's outer rectangle does
not; bubble *dimensions* do not (the pitch may, while "fit spacing to the region"
is on - that is the reflow).

**Automated:** `tests/unit/test_question_region_container.py::test_changing_the_column_gap_leaves_the_outer_rectangle_untouched`,
`tests/gui/test_template_designer_dialogs.py::test_editing_the_column_gap_changes_the_generated_x_positions`.

---

## Scenario 9 - Pan

Zoom to ~200%. Middle-drag, then right-drag.

**Verify:** both pan the viewport; the model is byte-identical before and after;
no region is selected or moved; a left-drag still moves the selected object; a
plain right-click (below the drag threshold) does not pan.

**Automated:** `tests/gui/test_template_designer_canvas_panning.py`,
`tests/gui/test_template_designer_bubble_and_layout.py::TestPanningDoesNotAlterTheDocument`.

---

## Scenario 10 - Save and reload

Change a region's geometry, the bubble radius and a question-column offset. Save
the template. Reload it.

**Verify:** semantic and geometric equality within floating-point tolerance -
including `default_bubble_radius`, which a document written before that field
existed loads as `None` and falls back for.

**Automated:** `tests/integration/test_template_service.py`.

---

## Scenario 11 - Scan the sample end to end

Load `examples/templates/ece_0000_sample.omrt`, import `examples/ECE-0000.png`,
process it, then select the row.

**Verify:** roll `00000000`, set code `10` (two printed positions - never
reduced to `1` or to the integer ten), 100 answers reading
`aaaabbbbccccdddd` then `abcd` repeating, and a registration status that is
*named* rather than silently clean. Both this sample and the synthetic page
report `registered_with_warning` / `MULTIPLE_CORNER_CANDIDATES`, because real
sheets carry other dark rectangles near their corners; the reservation is
reported, and the recognised values show the four chosen corners still
rectified the page correctly.

**Automated:** `tests/gui/test_scan_page.py`,
`tests/integration/test_sample_sheet_recognition.py`.

---

## Scenario 12 - Duplicate roll numbers

Import three sheets that recognise to the same roll number, choose an output
folder, tick `renameScansCheckBox`, process.

**Verify:** `00000000.png`, `00000000_a.png`, `00000000_b.png` all exist,
`scanTable`'s "Output file" column shows all three, and **no earlier file was
overwritten**. Repeat with one of those names pre-created in the output folder:
the new scan must become the next free suffix, and the pre-existing file's bytes
must be unchanged.

**Automated:** `tests/gui/test_scan_page.py` (`TestHDuplicateRolls`,
`TestIExistingFileCollision`), `tests/unit/test_filename_manager.py`.

---

## Scenario 13 - A batch survives one bad file

Import two readable sheets with a corrupt image between them. Process all.

**Verify:** both good sheets still recognise, the corrupt one is flagged
`Error` or `Registration failed` in `scanTable`, and the run reports it in the
summary rather than aborting.

**Automated:** `tests/gui/test_scan_page.py` (`TestFBatch`).

---

## Scenario 14 - Processing settings and multicore batches

Open `File > Settings...`. Select each mode in `processingModeCombo` and watch
`workerCountSpinBox` and `activeWorkersLabel`.

**Invariants:**

```
Automatic     selector disabled, active = min(cpu - 1, 8)
Single core   selector disabled, active = 1
Custom        selector enabled,  active = the spin box value
range         1 .. detected CPU threads; 0 and cpu+1 unreachable
```

Then, on the Scan page with several scans imported, process them once in Single
core and once in Custom and compare.

**Invariant:** the scan list, every recognised value and every output name are
identical; rows stay in import order; `progressBar` advances monotonically to
the row count; `progressLabel` reports `Completed n / N` and the worker count;
no worker process survives the run.

**Automated:** `tests/gui/test_processing_settings_gui.py`,
`tests/integration/test_parallel_batch.py`, and `scripts/run_gui_smoke_tests.py`
(the settings section, multicore/single-core equality, and the orphan-process
check).

---

## Screenshots to keep

Written to `test-output/gui/` by `scripts/capture_gui_states.py`:

```
template_empty.png
template_ece0000_loaded.png
template_ece0000_question_region.png
template_ece0000_question_1column.png
template_ece0000_question_5columns.png
template_ece0000_bubble_radius_small.png
template_ece0000_bubble_radius_large.png
template_ece0000_region_selected.png
template_ece0000_region_before_resize.png
template_ece0000_region_after_resize.png
template_ece0000_orientation_roi.png
template_ece0000_orientation_detected.png
scan_empty.png
scan_template_loaded.png
scan_processed.png
scan_overlay_zoom.png
scan_overlay_all_bubbles.png
scan_duplicate_rolls.png
scan_exported.png
scan_multicore_four.png
scan_multicore_single.png
settings_processing_automatic.png
settings_processing_single_core.png
settings_processing_custom.png
```

`scan_overlay_zoom.png` is the one worth reading closely: it is the answer area
at 1:1 with the recognition overlay on top, so a template-to-scan mapping that
is a few pixels out is visible. At fit scale it never is.

These are diagnostic evidence, not baselines. Fonts, anti-aliasing, Qt styles and
DPI differ between machines, so an ordinary unit test must never fail because two
of them differ. Compare with `scripts/compare_gui_images.py`, which reports mean
difference and changed-pixel percentage rather than byte equality.
