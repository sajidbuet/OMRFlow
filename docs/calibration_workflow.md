# The Calibration workflow (Phase 4)

Step between Template and Scan: verify that a saved `.omrt` template actually
matches representative real scans, and tune its recognition thresholds,
**before** running a batch with it.

This document covers what the workflow does, the procedure for using it, and
how to recognise a bad calibration. For the recognition pipeline it drives,
see [`recognition_engine.md`](recognition_engine.md); for how the modules fit
together, see [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## 1. Why this exists

A template that looks correct in the Template Designer is not necessarily
correctly aligned with a real printed sheet: scanner scaling, printer
margins, paper deformation, rotation, exposure and pencil-versus-pen all
affect a real scan in ways a designer preview cannot show. Calibration closes
that gap by running the **real** recognition pipeline - the same one the Scan
page uses - against real scans, in a mode that shows every intermediate
measurement rather than only the final answer.

**What calibration confirms:**

- registration works on the scans tested;
- bubble geometry appears aligned with what is actually printed;
- the current thresholds are reasonable for the scans tested;
- no obvious systematic mismatch was detected.

**What it does not guarantee:** 100% recognition accuracy on every future
scan. A calibration result is a statement about the scans it was run against,
never a general accuracy claim - see [§7](#7-real-scans-vs-synthetic-scans).

---

## 2. The short version

1. **Load Template** - the `.omrt` to verify.
2. **Add Scan(s)** or **Add Folder** - several *representative* real scans
   (§6), not one.
3. **Run Test** (the selected scan) or **Run All Tests** (every scan).
4. Inspect the marker overlay, the bubble overlay, the quality summary and the
   calibration status for each scan.
5. Adjust a threshold if the evidence calls for it, and watch recognition
   update immediately - no scan is re-registered for this.
6. Repeat step 3 to confirm the change holds across every representative scan.
7. **Save to Template** once satisfied.
8. Only then process the production batch on the Scan page.

Nothing is written to the template file until step 7.

---

## 3. Representative scans

One perfect scan proves little. A representative sample should include, where
available:

- light marking and dark marking;
- pencil, and pen if the sheets accept it;
- a slightly rotated or skewed scan;
- sheets from more than one scanner or scanning session, if the batch will be;
- sheets that sit slightly differently on the platen.

This is guidance, not a requirement that the application fabricate any of
these - it describes what an operator should go and collect, not something
Calibration invents on its own.

---

## 4. What each control shows

### Overlay layers

| Toggle | Shows |
| --- | --- |
| **Markers** | Every registration marker's *expected* position (blue cross - the template's own declared centre, in canonical pixels) against its *detected* position (green square: close; red square: displaced, with the distance labelled). |
| **Regions** | Every zone rectangle, tinted by its status - the same overlay the Scan page draws. |
| **Selections** | Bubbles the engine took as the answer, or that need review. |
| **All bubbles** | Also outlines bubbles measured as empty - useful for checking that a bubble window sits where the printed circle actually is. |

The **field filter** (All / Student ID / Set Code / Questions / Other fields)
narrows the bubble overlay to one kind of field - useful on a template with
many questions.

### Bubble inspector

Click any bubble in the preview. The panel on the right shows its field
identity (e.g. "Question 42 - option C", "Roll number - position 3 - value
7"), its centre in canonical pixels, its raw fill score, the active fill and
blank thresholds, the measured darkness and contrast, and its classification
(FILLED / EMPTY / etc.) - exactly the numbers the recognition engine itself
computed, never a re-estimate.

### Recognition thresholds

Four sliders, each with an exact-entry spin box, one per threshold the engine
actually uses (`omr_scanner.domain.template.RecognitionSettings`):

| Threshold | What it controls |
| --- | --- |
| **Bubble fill threshold** | Fraction of a bubble's interior that must be dark for it to count as marked. |
| **Blank threshold** | Below this, a bubble is certainly empty. Between it and the fill threshold is the ambiguous band. |
| **Ambiguity margin** | Minimum separation between the best and second-best bubble in a group before a single mark counts as resolved rather than uncertain. |
| **Minimum confidence** | Below this decision score, a resolved value is still queued for human review. |

Changing any of them **reclassifies every already-opened scan immediately**:
no file is re-read, no marker is re-detected, no page is re-warped. Only the
already-measured bubble scores are re-decided against the new threshold - see
[§8](#8-architecture-why-threshold-changes-are-instant).

**Working value vs. saved value.** Moving a slider never touches the
template file. **Reset to Template** restores the values the template was
last saved with; **Defaults** restores the application's built-in defaults;
**Save to Template** is the one action that writes anything to disk, and it
asks for confirmation first, naming the calibration status it is about to
record.

### Quality summary and sample summary

Each tested scan gets a per-scan summary (registration, markers detected,
orientation, recognised Student ID and Set Code with their own warnings,
answer counts by kind, near-threshold bubble count, and every finding behind
the status). With more than one scan, a sample-level summary aggregates them
- registration success rate, how many scans carry ambiguity or multiple
marks, and a bubble-score separation label - and a compact table lists every
scan with its own status, mirroring the format an operator would use to
triage a real batch.

---

## 5. The validation status

Every run produces one of four explicit statuses
(`omr_scanner.services.calibration_service.CalibrationStatus`):

| Status | Meaning |
| --- | --- |
| **Validation passed** | Registered cleanly, no geometry problem, no systematic ambiguity. |
| **Validation passed with warnings** | Usable, but something is worth a look - a marginal alignment warning, an isolated unmeasurable bubble, ordinary review-worthy marks below the systematic threshold. |
| **Needs review** | An operator should look before trusting this template on a batch - a geometry-class alignment warning, a meaningful fraction of unusable bubbles, or ambiguity that looks systematic. |
| **Calibration failed** | The sheet did not register, or so many bubble windows fell off the page that geometry cannot be trusted at all. |

A sample's overall status is the **worst** of its individual scans - never an
average - because the point of testing several scans is to find the one that
does not behave like the others, and averaging would hide exactly that.

**Blank and multiple marks are not, by themselves, failures.** A genuinely
blank or double-marked question is real candidate data, and the summary
reports it as a count, not a defect. What moves the status is *how much*
ambiguity there is, and whether registration or geometry itself is suspect -
see the next section.

---

## 6. Recognising a bad calibration

| Symptom | Likely cause |
| --- | --- |
| Bubble circles consistently displaced from the printed bubbles | Template geometry does not match this scan's print - check the region placement in the Template Designer. |
| Marker boxes (overlay) not centred over the printed registration squares | The template's declared marker positions are wrong for this sheet design, or the wrong template was loaded. |
| Upper questions aligned but lower questions increasingly shifted | A pitch (row/column spacing) that does not match the actual print - not something Calibration edits; return to the Template Designer. |
| Student ID or Set Code rows offset from their bubbles | Same as above, for that specific field's grid. |
| Bubble windows overlapping the neighbouring bubble | The bubble size is too large for the printed spacing. |
| Bubble windows too small to catch a real mark | The bubble size is too small, or the mark itself is small/off-centre. |
| Filled and empty bubble scores strongly overlapping | "Poorly separated" in the sample summary - a genuine measurement problem (lighting, print contrast), not something a threshold slider can fix. |
| Frequent "needs review" across otherwise clean scans | Check the systematic-ambiguity finding; a threshold that is close to the sheet's actual ink levels needs adjusting, or the sheets are genuinely faint. |
| Registration succeeding on some scans and failing on very similar ones | Inconsistent print or scan quality across the batch - worth widening the representative sample rather than adjusting a threshold. |

**The one thing Calibration will never do:** show a plausible-looking, wrong
result. If registration fails, the status is *Calibration failed* and no
field, answer or bubble is reported at all - there is nothing to show, so
nothing is shown. See [§8](#8-architecture-why-threshold-changes-are-instant)
for exactly how that fail-safe works.

---

## 7. Real scans vs. synthetic scans

Calibration is meant to be run against **real** scans. The application's
separate synthetic dataset generator (*Tools > Developer / Testing*,
[`recognition_engine.md`](recognition_engine.md#7-synthetic-datasets)) is a
developer tool for regression testing the recognition engine itself, and
produces useful test material for Calibration too - controlled misalignment,
blank bubbles, multiple marks, ambiguous scores, missing markers, orientation
and perspective distortion all exercise the same overlay, threshold and
status machinery.

**Synthetic results never establish real-world accuracy**, and a calibration
run against only synthetic scans should not be read as validating a template
for production use. Load real, representative scans before relying on a
"Validation passed" for an actual examination.

---

## 8. Architecture: why threshold changes are instant

Calibration does not run a second recognition engine. Adding a scan opens a
`omr_scanner.services.recognition_service.CalibrationSession` - the *same*
`RecognitionEngine` the Scan page uses, doing the *same* registration and
bubble measurement. That is the one genuinely expensive step, and it happens
once per scan, in the background.

Every later threshold change calls `CalibrationSession.recompute()`
synchronously, on the GUI thread. It re-runs only the *decision* stage over
the measurements already taken - it never re-registers the page or
re-measures a bubble. That split (geometry/measurement vs. classification
threshold) is what the sliders' "immediate feedback" actually is, and it is
enforced structurally: a session simply has no method that re-registers.

A registration that failed is cached too. `recompute()` on a scan that could
not be registered returns the same failed result every time, because no
recognition threshold could change *why* a marker was not found - which is
also why the failed-scan path never has fields, answers or bubbles to show:
`ScanResult` for a failed registration carries none of them, by construction,
in every code path that produces one.

Overlay geometry - every bubble circle, every marker cross and square - is
drawn from the *same* `ScanResult` the recognition engine returned. The
marker overlay's "detected" position is the engine's own detected marker,
reprojected through the *same fitted homography* that produced the rectified
page; nothing about geometry is approximated or recomputed in the GUI layer.

---

## 9. Template compatibility and staleness

The four thresholds live in the template itself
(`omr_scanner.domain.template.RecognitionSettings`), exactly where they lived
before Phase 4 - Calibration adds no new template-level configuration beyond
a small calibration record (`docs/TEMPLATE_FORMAT.md`, "`calibration`").
Templates saved before Phase 4 load unchanged, simply "never calibrated".

Saving a calibration stamps the template with the run's status, sample count
and two fingerprints - one of its geometry, one of its recognition settings.
`OmrTemplate.is_calibration_current()` compares them against the template's
*current* state:

- editing a zone, a marker or the page geometry invalidates the geometry
  fingerprint;
- retuning any threshold - template-level or on a single zone - invalidates
  the recognition fingerprint.

The Calibration page shows "Validation out of date" the moment either
diverges, and the Scan page shows a small, non-blocking warning when loading
a template that has never been calibrated, or whose calibration has gone
stale. Neither prevents processing a batch - an experienced operator remains
free to proceed - but neither lets a stale "passed" go unremarked either.

---

## 10. Geometry problems are fixed in the Template Designer

Calibration tunes recognition **settings**; it does not edit **geometry**. If
a representative scan reveals that a region is physically misplaced, use
**Edit Template** to jump straight to the Template Designer with the same
document open, fix the region there, save, and return to Calibration to
re-run the test. There is deliberately no second geometry editor inside this
workflow - one editor, one source of truth for where a bubble is.
