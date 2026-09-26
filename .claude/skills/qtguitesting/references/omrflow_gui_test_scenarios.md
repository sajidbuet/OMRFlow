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
| `batchProgressPanel` | The progress readout as a whole |
| `progressBar` | Driven by completed/total, never by a percentage |
| `progressLabel` | The headline: Preparing / Processing / Complete / Cancelling |
| `progressCountsLabel` | "6,342 / 10,000 processed" |
| `progressTimingLabel` | "Elapsed 00:18:42 · Remaining ~00:10:47" |
| `progressRateLabel` | "Speed 5.7 scans/sec · 12 workers" |
| `progressOutcomeLabel` | "Successful 6,301 · Review 28 · Failed 13" |
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

### Developer testing tools (Phase 3)

| `objectName` | Widget |
| --- | --- |
| `generateDatasetAction`, `runBenchmarkAction` | The two `Tools > Developer / Testing` commands |
| `generateDatasetDialog` | The generation form |
| `datasetTemplateEdit`, `datasetOutputEdit`, `datasetNameEdit` | Where from, where to, what to call it |
| `datasetProfileCombo`, `datasetFamiliesList` | Profile, and the families a Custom profile uses |
| `datasetCountSpin`, `datasetSeedSpin`, `newSeedButton` | How many, and reproducibly |
| `datasetFormatCombo`, `datasetQualitySpin`, `datasetDpiSpin` | PNG/JPEG, quality, resolution |
| `datasetMetadataCheckBox`, `datasetBenchmarkCheckBox` | Extra manifests; benchmark afterwards |
| `generationProgressDialog` and its bar/labels | Progress, ETA, rate, cancel |
| `generationSummaryDialog`, `openDatasetFolderButton`, `runBenchmarkButton` | What was produced, and what to do next |
| `benchmarkBanner`, `benchmarkBannerLabel` | The Scan page in benchmark mode |
| `showBenchmarkResultsButton`, `exitBenchmarkButton` | Reopen the results; leave benchmark mode |
| `benchmarkResultsDialog`, `benchmarkTabs` | The results |
| `benchmarkSummaryTable`, `benchmarkCategoryTable`, `benchmarkErrorTable`, `benchmarkFailingScansList` | The four views |

**Never sleep waiting for a batch.** `ScanPage.batch_finished` carries the
`BatchReport`; wait on it (`qtbot.waitSignal`, or `ScanHarness.run_batch()` in
the scripts). Selecting a row starts a *separate* `PreviewWorker`, so anything
that measures or captures the preview must also wait for
`ScanPreviewView.has_page` - note it is a **property**, not a method.

### Durable batches (Phase 5)

| `objectName` | Widget |
| --- | --- |
| `resumeBatchButton` | Process only the scans this batch never finished |
| `retryFailedButton` | Re-read the failures, and only those |
| `batchStateLabel` | The stored batch's id and counts, or "no project open" |
| `scanStatusFilterCombo` | All / Completed / Needs review / Failed / Not processed |
| `scanFilterCountLabel` | "3 of 12" while a filter is active |

**A batch is only recorded when a project is open.** `build_scan_page(...)`
takes `with_project=True`, which creates a throwaway project under
`test-output/gui/projects/` and opens it on the page. Without it
`page.state.batch_id` stays `None`, `resume_batch()` refuses, and the state
label says the run will not be saved - all of which is correct behaviour, not a
harness failure.

`ScanHarness` gained `run_paths(paths)` for a **partial** run (the resume
scenarios need one; `run_batch()` always processes everything and therefore
leaves nothing to resume), plus `resume()`, `retry_failed()` and
`batch_summary`. `shutdown()` closes the page *before* the session, because the
page's own shutdown flushes the last results into the database.

**Assert that resume did less work**, not merely that the totals came out
right - a resume that silently re-read everything produces the same final
counts:

```python
harness = build_scan_page(scans, with_project=True)
first = harness.run_paths(scans[:2])        # partial run
assert harness.batch_summary.pending == 2
resumed = harness.resume()
assert resumed.total == 2                    # the two left, not all four
assert harness.batch_summary.processed == 4
```

### Resolve page (Phase 6)

| `objectName` | Widget |
| --- | --- |
| `resolvePage` | The page itself |
| `conflictQueuePanel` | The whole left-hand queue |
| `reviewBatchLabel` | Which batch is under review, or "no batch" |
| `conflictStateFilter` | Unresolved / Open / Resolved / Deferred / Withdrawn / All |
| `conflictTypeFilter` | One conflict type, or all of them |
| `conflictSearchBox` | Free text over student ID and file name |
| `conflictQueueTable` | The queue. One row per conflict |
| `reviewSummaryLabel` | Live counts for the batch |
| `reviewToolbar` | Navigation, zoom and history |
| `previousConflictButton`, `nextConflictButton`, `nextUnresolvedConflictButton` | Queue navigation |
| `reviewZoomOutButton`, `reviewZoomInButton`, `reviewFitButton`, `reviewRecentreButton` | View controls |
| `conflictHistoryButton` | Opens `conflictHistoryDialog` |
| `sheetConflictProgressLabel` | "conflict 2 of 7 on this sheet" |
| `reviewViewTabs` | Zoomed field / Normalised sheet / Original scan |
| `conflictZoomView`, `normalisedSheetView`, `originalSheetView` | The three views, all `ScanPreviewView` |
| `originalSheetNote` | Where on the original the field is - the original carries **no** overlay |
| `conflictDecisionPanel` | The whole right-hand decision panel |
| `machineEvidenceLabel` | What the machine saw, with each option's **fill score** |
| `reviewerNameLabel` | The configured reviewer, or a warning that there is none |
| `choiceButton_<LABEL>` | One per template option, plus `choiceButton_(blank)` |
| `correctedValueEdit`, `saveCorrectedValueButton` | Free text, shown **instead** of the buttons when the field has no fixed alphabet |
| `correctionReasonCombo`, `correctionReasonText` | Reason code, and the explanation "Other" requires |
| `acceptMachineValueButton`, `deferConflictButton`, `reopenConflictButton` | The other three actions |
| `conflictProvenanceLabel` | Where this conflict's current value comes from |
| `conflictHistoryDialog`, `conflictHistoryLabel` | The audit history |

Scan page additions: `reviewConflictsButton`, `batchConflictLabel`.

**Value buttons are built from the template, so do not hard-code them.** The
labels come from `conflict_policy.group_labels()`, which reads the same zone
grouping recognition used - a six-option template yields six buttons plus
`(blank)`. Find them with `page.findChild(QPushButton, f"choiceButton_{label}")`
after a conflict is selected, or walk `page._choice_buttons`.

**Three controls are mutually exclusive by design**, and a test that assumes
otherwise is asserting the wrong thing:

- a **field** conflict with a fixed alphabet shows `choiceButton_*`;
- a conflict with **no** fixed alphabet (a whole identifier, a duplicate roll
  number) shows `correctedValueEdit` instead - `group_labels()` returns empty;
- a **processing failure** (`ConflictType.is_processing_failure`) shows neither,
  because `A`/`B`/`C`/`D` is not an answer to a corrupt JPEG.

**Never sleep waiting for a sheet.** Selecting a conflict re-reads that one
sheet in a `SheetWorker` thread and emits `sheet_ready`. Wait on the
**condition**, not the signal: the page auto-selects row 0 when a batch loads,
so by the time a scenario calls `select()` the signal it wanted may already have
fired. `ReviewHarness.select()` in `_harness.py` waits for
`bundle is not None and page._loaded_scan_id == target.scan_id` for this reason.

**Walking one sheet's conflicts must decode its image once.** The worker is
started only when the selected conflict belongs to a *different* sheet. A
scenario that asserts otherwise has caught a real regression.

**Call `ResolvePage.shutdown()` before the session closes.** A `SheetWorker`
still running at interpreter teardown aborts the process with exit code 9.
`ReviewHarness.shutdown()` does this; `MainWindow.closeEvent` does it in the
application.

**A decision needs a reviewer name.** `build_review_page(...)` sets one. Without
it every correction is refused and the conflict stays `open` - correct
behaviour, not a harness failure.

**A resolved conflict leaves the filtered queue.** The table keeps its row
index, so after a decision the row at that index is a *different* conflict. The
page re-selects by conflict id (`_restore_selection`); a scenario that reads
`current_conflict()` straight after a decision without waiting for the rebuild
is reading the queue mid-flight.

### Attendance page (Phase 7)

| `objectName` | Widget |
| --- | --- |
| `activeRosterLabel` | Which candidate list is in force, or "no candidate list imported" |
| `importRosterButton`, `downloadSampleTemplateButton`, `reconcileButton` | The three roster commands |
| `reconciliationSummaryLabel` | Every count, and whether work remains |
| `reconciliationStatusFilter` | Everything / Exceptions only / one classification |
| `reconciliationResolutionFilter` | Any / Needs review / Resolved / Accepted as-is |
| `reconciliationSearchBox` | Free text over candidate ID and name |
| `reconciliationTable` | One row per candidate or unplaced script group |
| `reconciliationCountLabel` | "12 row(s) shown · 5 needing review" |
| `reconciliationDetailLabel` | What is wrong, and what the list said |
| `entryScriptsList` | Every script on this entry, **including set-aside ones** |
| `assignCandidateEdit`, `assignScriptButton` | Attribute the selected script by hand |
| `excludeScriptButton` | Set aside / bring back. Label changes with state |
| `overrideAttendanceButton` | Override the imported attendance |
| `dismissEntryButton` | Accept as-is / put back. Label changes with state |
| `reconciliationReasonCombo`, `reconciliationReasonText` | Reason code, and the explanation "Other" requires |
| `reconciliationHistoryLabel` | Every decision recorded about this entry |
| `reconciliationOperatorLabel` | Who decisions are recorded as, or a warning |

Import dialog: `rosterFileLabel`, `rosterSheetCombo`, `candidateIdColumnCombo`,
`candidateNameColumnCombo`, `attendanceColumnCombo`, `rosterPreviewTable`,
`rosterValidationLabel`, `confirmRosterImportButton`.

**The table opens on "Exceptions only".** That is the work. A scenario that
expects to find a *matched* candidate must call
`ReconciliationHarness.show_everything()` first, or it will fail looking for a
row that is deliberately filtered out.

**Reconciliation runs in a `QThread`.** Use `harness.reconcile()`, which pumps
events until `reconciled` fires - reading the table straight after calling
`page.reconcile()` reads the *previous* run's rows. Note that `reconciled`
carries **no payload**, so a slot connected to it must take no arguments;
`done.append` will raise `TypeError`.

**Do not name test scan files after roll numbers.** The Phase 3 pipeline logs
each scan's file name, so a fixture called `m_100001.png` puts `100001` into
the log through Phase 3 - and makes the Phase 7 privacy check assert the wrong
thing. `build_reconciliation_page` names its sheets `scan_a.png`…`scan_e.png`
for exactly this reason.

**Every decision needs a named operator.** `build_reconciliation_page(...)` sets
one; pass `operator=""` to exercise the refusal. Without it every action raises
and nothing is written - correct behaviour, not a harness failure.

**A decision re-runs reconciliation.** The page's `resolution_recorded` signal
fires after the table is rebuilt, so a test that drives the page should wait on
it. A test that calls `reconciliation_store` *directly* must not - the page
emits nothing it did not do.

**Call `AttendancePage.shutdown()` (or `close()`) before the session closes**,
and the same for `RosterImportDialog`. Both track *every* worker they start,
not just the most recent: the import dialog starts a reader on each column
change, and a superseded `QThread` is still a running thread. One alive at
teardown aborts the process with exit code 9 and no traceback.

### Answer Key and Results pages (Phase 8)

| `objectName` | Widget |
| --- | --- |
| `answerKeySetCombo` | The question-paper set. Editable - set codes are not one character |
| `answerKeyRevisionCombo` | Every stored revision for this set |
| `readKeyFromScanButton` | Recognise a solution sheet as a draft key |
| `answerKeyTextEdit` | The key, one character per question |
| `wrongQuestionEdit` | Question numbers withdrawn for this set |
| `answerKeyValidationLabel` | Every problem, or "Valid" |
| `saveAnswerKeyButton`, `verifyAnswerKeyButton` | Store a revision; lock it |
| `answerKeyTable`, `answerKeySummaryLabel` | The same key, question by question, plus the canonical string |
| `answerKeyReviewerLabel` | Who a verification is recorded as |
| `scoringPolicyLabel` | The rules in force and the verified keys |
| `configureScoringButton`, `checkBeforeScoringButton`, `calculateResultsButton` | The three Results commands |
| `resultsSummaryLabel`, `scoringProgressBar` | Counts; progress during a run |
| `resultsFilterCombo`, `resultsSearchBox`, `resultsTable`, `resultsCountLabel` | The results list |
| `resultsDetailLabel`, `resultDetailTable` | One candidate's provenance and per-question marks |
| `reviewAnswersButton`, `recalculateCandidateButton` | Open the Resolve stage; rescore one candidate |

Policy dialog: `correctMarkSpin`, `blankMarkSpin`, `negativeNoneRadio`,
`negativeFixedRadio`, `negativeOnePerThreeRadio`, `negativeOnePerFourRadio`,
`incorrectPenaltySpin`, `multipleSamePenaltyCheck`, `multiplePenaltySpin`,
`clampMinimumCheck`, `minimumScoreSpin`, `scoringPreviewLabel`.

**Nothing is marked against a draft key.** `ScoringHarness.write_key(...)`
saves a revision; `verify_key(...)` locks it. A scenario that forgets the
second gets every candidate `blocked`, which is correct behaviour and not a
harness failure.

**`verify_key` goes through the store, not the button.** The page's Verify
button opens a confirmation dialog, which these scripts never drive. The
refusal it enforces - no reviewer name, no verification - is exercised by
`scoring_store` directly.

**Scoring runs in a `QThread`.** Use `harness.score()`, which pumps events
until `scored` fires. That signal carries **no payload**, so a slot connected
to it must take no arguments; `done.append` will raise `TypeError`.

**Assert marks exactly.** A result's `final_score` is a `Fraction`. Compare
against `Fraction(...)`, not a float, and use `format_mark` only for the
message a check prints.

**Do not name test scan files after roll numbers**, for the same reason as
Phase 7: the Phase 3 pipeline logs each scan's file name.
`build_scoring_pages` names its sheets `scan_a.png`…`scan_d.png`.

**A template must be broadcast before either page is useful.** Both need the
question count and the option labels;
`MainWindow.broadcast_template(template)` is the route, and
`build_scoring_pages` calls `set_template` directly.

### Calibration page (Phase 4)

| `objectName` | Widget |
| --- | --- |
| `calibrationPage` | The page itself |
| `testScanList` | Representative scans |
| `addTestScanButton`, `addTestScanFolderButton`, `removeTestScanButton`, `clearTestScansButton` | Sample management |
| `runCalibrationButton`, `runAllCalibrationButton` | Register/measure the selected scan, or every scan |
| `calibrationImageView` | The preview - a `ScanPreviewView`, same widget class as the Scan page |
| `markerOverlayToggle`, `regionOverlayToggle`, `bubbleOverlayToggle`, `scoreOverlayToggle` | Overlay layers |
| `sampleWindowOverlayToggle` | The **sampled** ellipse - smaller than the printed bubble, see below |
| `bubbleCenterOverlayToggle` | The sampled centre of every bubble |
| `calibrationViewModeCombo` | Registered page / Original scan |
| `fieldFilterCombo` | All / Student ID / Set Code / Questions / Other fields |
| `fieldDiagnosticsLabel` | Per-position Student ID / Set Code detail and flagged questions |
| `thresholdStateLabel` | Whether the working thresholds match the template's saved ones |
| `bubbleThresholdSlider`/`SpinBox`, `blankThreshold*`, `ambiguityMargin*`, `minConfidence*` | The four `RecognitionSettings` thresholds, working value |
| `resetCalibrationButton` (to template), `resetToDefaultsButton`, `saveCalibrationButton`, `exportCalibrationReportButton` | Threshold lifecycle |
| `calibrationStatusLabel`, `calibrationSummaryPanel` | Per-scan verdict and quality summary |
| `calibrationSampleSummaryLabel`, `calibrationSampleTable` | Aggregate, multi-scan summary |
| `bubbleDiagnosticPanel`, `bubbleInspectorLabel` | Click-to-inspect one bubble |
| `advancedDiagnosticsButton`, `advancedDiagnosticsPanel` | Registration residuals, homography-derived numbers |

**Never sleep waiting for a run.** `CalibrationPage.run_finished` fires once
every scan a `run_selected()`/`run_all()` call touched has been attempted -
wait on it (`qtbot.waitSignal`, or `CalibrationHarness.run_all()` in the
scripts) exactly as the Scan page's `batch_finished` is awaited. A **threshold
change never starts a worker** - `session.recompute()` runs synchronously on
the GUI thread - so a test asserting immediate feedback must *not* wait on
`run_finished` for it.

**`.isVisible()` is unreliable for a page under test.** A page built with
`qtbot.addWidget(page)` and never `.show()`n reports every descendant as
invisible regardless of its own `setVisible` state, because Qt folds in the
(also-hidden) top-level ancestor. Use `widget.isVisibleTo(page)` instead - see
`tests/gui/test_calibration_page.py`.

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

## Scenario 15 - Large-batch progress, ETA and cancellation

The progress panel is a pure function of a
`ProgressSnapshot`, so most of it is checked by constructing one and calling
`page._render_progress(...)` - no ten-thousand-sheet batch required.

**Invariants:**

```
6,342 / 10,000   -> bar value 6342, maximum 10000, format "63.4%"
9,999 / 10,000   -> value != maximum   (never rounds up to finished)
no ETA yet       -> "Remaining Calculating...", no "~" anywhere
cancelling       -> "Remaining Cancelling...", Cancel disabled, label changed
complete         -> value == maximum, "100.0%", "Completed in HH:MM:SS"
10,000 rows      -> exactly one QProgressBar in the page
```

Then with a real batch of a dozen sheets: the bar advances monotonically, a
failed scan still advances it, the outcome tallies add up to the processed
count, and no `QMessageBox` appears for an unreadable file.

**Throttling:** `_on_progress` must not touch a widget - fifty calls in a row
leave the labels unchanged - and `_on_scan_done` must only mark its row dirty,
with `_flush_dirty_rows()` doing the drawing.

**Automated:** `tests/gui/test_batch_progress_gui.py`,
`tests/unit/test_batch_progress.py` (the estimator, headless, including a
10,000-job simulation), and `scripts/run_gui_smoke_tests.py` (panel rendering
and the no-per-scan-widgets check).

---

## Scenario 16 - Developer testing tools

*Tools > Developer / Testing*: generate a labelled dataset, then score
recognition against it.

**The menu:** `Tools` and its `Developer / Testing` submenu exist, and both
`generateDatasetAction` and `runBenchmarkAction` are enabled with no project
open - a testing tool gated behind a project is a testing tool nobody reaches.

**The generation dialog** (constructed, never `exec()`d):

```
format PNG              -> datasetQualitySpin disabled
format JPEG             -> datasetQualitySpin enabled
profile Custom, none    -> request() is None   (a custom profile needs families)
profile Custom + a tick -> request().families == (that family,)
no template path        -> request() is None
```

Qt hands item data back as a plain value, never the enum object, so
`selected_profile()` / `selected_format()` coerce it. A test that compares
`currentData() is DatasetProfile.CUSTOM` will fail for that reason and not
because anything is broken.

**Generation:** `DatasetWorker` emits `sheet_done` once per sheet and
`finished_dataset` once, including after a cancel - what was written is a real
dataset, and its manifest carries `cancelled: true` and the count actually
produced. A missing template becomes a `failed` signal, never an exception on
the thread.

**Benchmark mode:** `page.enter_benchmark_mode(dataset)` shows
`benchmarkBanner`, loads the dataset's scans into the ordinary list and turns
renaming off. `Process All` then runs the normal pipeline and
`benchmark_finished` carries the scored report. A baseline dataset must score
1.0000; anything less is a defect in the generator or the engine. Set
`page.benchmark_auto_show = False` so no modal appears in a headless run.

**Results:** `benchmarkSummaryTable`, `benchmarkCategoryTable` and
`benchmarkErrorTable` exist; the category table has one row per tag, and
`scan_requested` connected to `page.select_scan_named` selects that row.

**Automated:** `tests/gui/test_developer_tools.py` (20 tests) and
`scripts/run_gui_smoke_tests.py` (the menu, the dialog, the results dialog, and
one end-to-end generate-and-benchmark against the real template).

---

## Scenario 17 - Calibration (Phase 4)

Load `examples/ece_0000_sample.omrt` and `examples/ECE-0000.png` into the
Calibration page, register and measure it, adjust a threshold, then repeat
against a deliberately mismatched template.

**The clean path:**

```
load template -> add the real sample -> run_selected()
  -> entry.result.registration != FAILED
  -> len(entry.result.markers) == 4
  -> entry.report.status in {PASSED, PASSED_WITH_WARNINGS}
```

**Overlay geometry is exact, not approximate** - the major Phase 4 requirement.
For every bubble the overlay draws, `(overlay bubble.x, overlay bubble.y) ==
(result bubble.x, result bubble.y)`: the page never recomputes a position, it
reads `entry.result.bubbles` straight from the engine. Same for markers -
`marker.canonical_x/y` is the *detected* marker reprojected through the
engine's own fitted transform, `marker.expected_x/y` is the template's own
declared centre in canonical pixels, and on a clean scan the two are within
about a pixel of each other (`MARKER_MISMATCH_PX = 3.0` is where the overlay
switches from green to red).

**Threshold feedback is immediate and never re-registers:**

```
before = entry.result.answer(N)     # e.g. status "uncertain", faint mark
page.fill_spin.setValue(before.top_fill - 0.05)
after = entry.result.answer(N)      # same top_fill, different status/value
after.canonical_width == before.canonical_width   # registration untouched
after.markers == before.markers
```

No `CalibrationWorker` run happens for this - `CalibrationSession.recompute()`
executes synchronously on the GUI thread. A test that waits on `run_finished`
for a threshold change will simply hang; don't.

**The sampled region is not the printed bubble.** `BubbleView.width/height` is
the *printed* bubble; `sample_half_width`/`sample_half_height` is the ellipse
the sampler actually read, at `BubbleMetricsConfig.sample_radius_ratio` (0.62)
of the printed half-axes - on the real sample, **22.3 x 22.3 px inside a
printed 36.0 x 36.0 px bubble**. The `Sampling` overlay draws the second, the
`Selections` overlay the first. Anything that draws one while meaning the other
shows an operator a region recognition never looked at, which is the single
most dangerous thing this page can do. Assert
`0 < bubble.sample_half_width < bubble.width / 2`.

**Miscalibration is never a confident pass** - the load-bearing check, and it
has *two* distinct shapes:

```
# 1. Markers displaced: registration itself fails.
#    Shift every marker centre by +0.3 normalised (default search_radius=0.05).
entry.result.registration is RegistrationStatus.FAILED
entry.result.fields == ()          # nothing measured
entry.result.answers == ()
entry.result.bubbles == ()
entry.report.status is CalibrationStatus.FAILED

# 2. Only the ZONES displaced: registration SUCCEEDS and nothing falls off
#    the page. Shift every zone's bounds and grid origin by +0.02 normalised.
entry.result.registration is not RegistrationStatus.FAILED   # markers fine
entry.report.bubbles_unusable == 0                            # all on-page
entry.report.status in {NEEDS_REVIEW, FAILED}                 # still not a pass
```

The second case is the one every registration-level check is blind to: the
sampling windows land on bare paper, so every group reads a *confident* blank.
It is caught by `NO_MARKS_DETECTED` (nothing marked anywhere) or by
`SYSTEMATIC_AMBIGUITY` (widespread review items), depending on how far the
displacement falls. On the real sample at +0.02 the verdict is `needs_review`
with "marks detected in 2 of 110 response positions".

**Automated:** `tests/integration/test_calibration_workflow.py` (18 tests, real
engine, real synthetic sheets, including both miscalibration shapes and the
small-vs-large marker offset pair), `tests/gui/test_calibration_page.py`
(37 tests, A-J), `tests/unit/test_calibration_service.py` (34 tests, the
judgement rules in isolation) and `scripts/run_gui_smoke_tests.py` (object
names, an end-to-end run against the real sample, both miscalibration checks,
and the sampled-vs-printed geometry check).

---

## Scenario 18 - Conflict review (Phase 6)

Process a batch with a project open, then settle the sheets whose **identity**
the machine could not read. The whole scenario is about one claim: **the
interface must never show a value different from the one the engine produced,
and a human decision must never replace it.**

The queue holds only the student ID, the set code and sheets that could not be
read. An ambiguous *answer* is a recognition result and never appears here, so
a scenario must stage a bad roll-number column or set code to get a conflict at
all — `ConflictType.ANSWER_MULTIPLE` is a legacy value this build never writes.

```python
harness = build_review_page(scans, with_project=True, reviewer="Dr. Smoke Test")
harness.run_batch()                          # detection runs when it finishes
conflict = harness.select(ConflictType.IDENTIFIER_MULTIPLE)
```

A scenario worth running beside it: a sheet with several double-marked answers
and a clean roll number and set code must produce **zero** rows in this queue,
while its answers still appear in the results panel and the exported CSV.

**Verify, in order:**

1. The queue lists one row per conflict, and `reviewSummaryLabel` counts agree
   with `review_store.count_conflicts()` - the GUI must not count separately.
2. Selecting a conflict loads **all three** views. `conflictZoomView` is
   centred on the disputed group and **only** that group is ringed; a scenario
   that finds two groups highlighted has caught a real defect.
3. `originalSheetView` carries **no overlay**, and `originalSheetNote` says
   where on the original the field is. Canonical coordinates do not apply to a
   non-canonical image, so drawing them there would be a lie.
4. `machineEvidenceLabel` reports the machine's value, its status, its decision
   score and each option's **fill score** - the words "fill score", never
   "probability".
5. Correct it. Then assert **both** halves of the invariant:

```python
harness.correct("B", reason=ReasonCode.DOMINANT_MARK)
provenance = review_store.provenance_for(db, conflict.conflict_id)
assert provenance.value == "B"                    # the effective value
assert provenance.machine_value == "B-D"          # still intact, both marks
assert provenance.source is ValueSource.HUMAN
assert provenance.reviewer == "Dr. Smoke Test"
```

6. Clear the reviewer name and correct again: the correction is **refused** and
   the conflict is still `open`. Attribution is not optional.
7. Attempt to rewrite the ledger. Both must raise:

```python
with database.session() as session:
    session.execute(text("UPDATE audit_event SET actor = 'someone else'"))   # aborts
    session.execute(text("DELETE FROM audit_event"))                          # aborts
```

8. Reopen the decision: the effective value falls back to the machine's, and
   the superseded correction is **still in the history** with its own reviewer,
   reason and timestamp.

**What is easy to get wrong here.** Asserting only that the corrected value
appears. A page that overwrote the machine value would pass that and fail the
entire phase - check `machine_value` every time.

**Automated:** `tests/unit/test_conflict_policy.py` (40 tests, detection in
isolation), `tests/unit/test_review_store.py` (48, the ledger rules and the
queue at 10,000 conflicts), `tests/integration/test_conflict_review.py` (24,
scenarios A-E against real rendered sheets), `tests/gui/test_resolve_page.py`
(37) and `scripts/run_gui_smoke_tests.py` (a named decision recorded end to
end, both tamper refusals, and the missing-reviewer refusal).

---

## Scenario 19 - Candidate reconciliation (Phase 7)

Import a candidate list, reconcile the batch against it, and resolve what does
not line up. The whole scenario is about one claim: **a discrepancy is
something to review, never something to silently discard.**

```python
harness = build_reconciliation_page(operator="Dr. Smoke Test")
harness.show_everything()        # the table opens on exceptions only
entries = harness.entries()
```

**Verify, in order:**

1. Every classification the phase names is produced from one batch:

```python
assert entries["100001"].status.value == "matched"
assert entries["100002"].status.value == "duplicate_script"
assert entries["100003"].status.value == "absent_confirmed"
assert entries["100004"].status.value == "present_without_script"
assert entries["100005"].status.value == "absent_with_script"
assert entries["999999"].status.value == "unknown_id"
```

2. **No script has vanished.** Every scan in the batch appears in exactly one
   entry, including the one belonging to nobody.
3. Resolve the unknown ID onto `100004`, then assert **all three** values:

```python
assert entries["100004"].status.value == "matched"           # the effect
assert script.machine_candidate_id == "999999"               # the engine, kept
assert history[0].reviewer == "Dr. Smoke Test"               # who is answerable
```

4. Override `100005`'s attendance and assert the *imported* value survives:

```python
assert entry.effective_attendance.value == "present"
assert entry.candidate.imported_attendance.value == "absent"   # the file, kept
```

5. Assign a script to a candidate who already has one: the entry must become
   `duplicate_script` **immediately**, not at export time.
6. Set one of a duplicate pair aside: the entry becomes `matched`,
   `script_count` falls to 1, and `len(entry.scripts)` stays at 2 - the script
   is set aside, never deleted.
7. Clear the operator name and try again: the decision is refused and the entry
   keeps its previous state.
8. Save the sample template, read it straight back, and confirm it carries only
   placeholder names.
9. Capture the root logger across a whole import-reconcile-assign cycle and
   assert **no candidate ID or name appears in it**.

**What is easy to get wrong here.** Asserting only that the effective value
changed. An implementation that overwrote the imported attendance or the
machine's roll number would pass that and fail the entire phase - check the
source values every time.

**Automated:** `tests/unit/test_candidate_import.py` (87),
`tests/unit/test_reconciliation.py` (44),
`tests/unit/test_reconciliation_store.py` (55),
`tests/unit/test_candidate_privacy.py` (18),
`tests/integration/test_reconciliation_workflow.py` (19, real recognition over
real sheets), `tests/gui/test_attendance_page.py` (56) and
`scripts/run_gui_smoke_tests.py` (five checks: every classification, the
machine-and-imported-values check, the missing-operator refusal, the sample
download, and the log capture).

---

## Scenario 20 - Answer keys and scoring (Phase 8)

Write a key, verify it, mark a batch, and change a rule. The scenario is about
one claim: **a mark is reproducible from stored inputs, and changing an input
recomputes rather than patches.**

```python
harness = build_scoring_pages(operator="Dr. Smoke Test")
total = harness.plan.question_count
harness.write_key("A", "A" * total)
```

**Verify, in order:**

1. Score *before* verifying the key. Every candidate who is not absent must be
   `blocked`. A draft key produces no marks, ever.
2. Verify, score, and check the acceptance outcomes:

```python
harness.verify_key("A")
harness.score()
found = harness.results()
assert found["200001"].final_score == total          # perfect paper
assert found["200002"].final_score == total - 3      # three wrong
assert found["200003"].status.value == "absent"
assert found["200003"].final_score is None           # no mark, not zero
assert found["200004"].status.value == "blocked"     # no key for their set
```

3. Check the **provenance**, not just the number:

```python
assert found["200001"].answer_key_revision == 1
assert "Set A / revision 1" in found["200001"].describe_provenance()
```

4. Change the policy. The result must go **stale and keep its mark**:

```python
harness.results_page.apply_policy(ScoringPolicy(..., mode=NegativeMarking.FIXED))
stale = harness.results()["200002"]
assert stale.is_stale
assert stale.final_score == before.final_score     # not patched
```

5. Rescore, and check the mark was **re-derived** from unchanged answers:

```python
harness.score()
after = harness.results()["200002"]
assert after.final_score == Fraction(total - 3) - 3 * Fraction(1, 4)
assert after.answer_string == before.answer_string
assert after.policy_revision == before.policy_revision + 1
```

6. Corrupt a stored score directly in the database, leaving every input
   untouched, rescore, and assert the mark comes back correct rather than
   adjusted. **This is the check that distinguishes recomputation from
   patching**, and an implementation that patched would pass every other step
   above.
7. Withdraw a question the candidate answered wrongly and confirm it pays full
   credit - and that the detail table says *"Wrong question - full credit"*.

**What is easy to get wrong here.** Asserting only the total. A mark that is
numerically right but records the wrong key revision is exactly the result
nobody can defend six months later - check `answer_key_revision` every time.

**Automated:** `tests/unit/test_scoring.py` (71),
`tests/unit/test_answer_key.py` (43),
`tests/unit/test_scoring_store.py` (55),
`tests/integration/test_scoring_workflow.py` (22, real recognition over real
sheets), `tests/gui/test_scoring_pages.py` (56) and
`scripts/run_gui_smoke_tests.py` (five checks: the draft-key refusal, the
acceptance outcomes, staleness, the corrupted-mark recomputation, and the
wrong-question rule).

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
scan_progress_large_batch.png
scan_progress_cancelling.png
settings_processing_automatic.png
settings_processing_single_core.png
settings_processing_custom.png
devtools_generate_dialog.png
devtools_benchmark_mode.png
devtools_benchmark_results.png
calibration_loaded.png
calibration_run_clean.png
calibration_overlay_markers.png
calibration_sampling_top.png
calibration_sampling_middle.png
calibration_sampling_bottom.png
calibration_threshold_ambiguous.png
calibration_mismatched_failed.png
calibration_displaced_zones.png
calibration_displaced_zoomed.png
review_conflict_open.png
review_zoomed_field.png
review_original_scan.png
review_conflict_resolved.png
reconciliation_exceptions.png
reconciliation_absent_with_script.png
reconciliation_attendance_overridden.png
```

`scan_overlay_zoom.png` is the one worth reading closely: it is the answer area
at 1:1 with the recognition overlay on top, so a template-to-scan mapping that
is a few pixels out is visible. At fit scale it never is.

The three `calibration_sampling_*.png` images are captured at the **top, middle
and bottom** of the same sheet on purpose. A scale or perspective error
accumulates down the page, so a template that looks perfectly aligned in the
Student ID block can be most of a bubble out by question 100. Checking one
region proves nothing about the others.

`review_conflict_resolved.png` is the Phase 6 equivalent of a zoomed overlay:
the one image where the phase's whole invariant is either visibly true or
visibly false. It must read like *"Effective value: **B** — corrected by
**<name>** at <time>. Machine read **B-D**, which is kept."* A resolved
conflict that shows only the corrected value has lost the machine's
observation, and no amount of passing unit tests makes that acceptable.

`reconciliation_attendance_overridden.png` is the Phase 7 equivalent, and it
is the one image where that phase's invariant is visibly true or visibly false.
The row must read *"Expected present (list said marked absent)"* and the detail
*"Candidate list said: **Marked absent** (cell read 'abs')"* beside *"A
reviewer recorded: **Expected present**"*. A row showing only the new value has
lost the imported one, and no amount of passing unit tests makes that
acceptable.

Reading it beside `reconciliation_exceptions.png` also catches the arithmetic:
the summary's *Resolved* count and the table's *needing review* count must move
in opposite directions when a decision is recorded. That comparison is how the
dead `Resolved 0` count was found.

`review_zoomed_field.png` is worth reading at 1:1 beside
`scan_overlay_zoom.png`: both draw the recognition engine's own coordinates,
and a highlight that sits a few pixels off the printed bubbles means the
reviewer is being shown the wrong evidence to decide from.

`calibration_displaced_zoomed.png` is the counter-example to read beside them:
same real scan, zones shifted by 2 per cent of the page, registration still
succeeding - the sampled ellipses sit visibly below and right of the printed
bubbles, and the verdict reads "Needs review" rather than a pass.

These are diagnostic evidence, not baselines. Fonts, anti-aliasing, Qt styles and
DPI differ between machines, so an ordinary unit test must never fail because two
of them differ. Compare with `scripts/compare_gui_images.py`, which reports mean
difference and changed-pixel percentage rather than byte equality.
