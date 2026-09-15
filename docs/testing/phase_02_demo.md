# Phase 2 functional demonstration

This is a real, executed run of the twelve-step demonstration required for
Phase 2 completion, driven against the actual `TemplateDesignerPage` code -
the same methods the toolbar buttons and canvas signals call - via a script
rather than mouse clicks, because the development environment's Qt platform
plugin (`offscreen`) renders text as placeholder boxes rather than real glyphs
(see `development/phase_02_implementation_notes.md`) and is therefore
unsuitable for a screenshot-driven walkthrough. Every value below is copied
verbatim from that run; none is hand-typed.

The equivalent manual, mouse-driven version of every one of these steps is
`docs/testing/phase_02_manual_test.md`, to be run against the real windowed
application before the designer is trusted for a real sheet.

## Setup

```python
spec = next(s for s in WORKFLOW_PAGES if s.key == "template")
page = TemplateDesignerPage(spec)
```

## 1. Start the application

```text
Page constructed: Template | implemented: True
```

`WorkflowPageSpec.is_implemented` is `True` for the Template stage - it is a
real page, not a placeholder (`gui/pages/catalog.py`).

## 2. Load a sample/generated OMR sheet

A synthetic sheet from Phase 1's generator (`omr_scanner.imaging.synthetic`)
stands in for a scanned reference image:

```text
Loaded demo_sheet.png: 1240x1754 px
```

## 3. Detect four registration markers

```text
top_left       confidence=0.94 confirmed=False center=(0.050,0.035)
top_right      confidence=0.94 confirmed=False center=(0.950,0.035)
bottom_right   confidence=0.94 confirmed=False center=(0.950,0.965)
bottom_left    confidence=0.94 confirmed=False center=(0.050,0.965)
All markers confirmed: True
```

All four corners found (Phase 1's detector, reused through
`marker_detection_service`, never reimplemented), each independently, each
starting unconfirmed (amber in the real UI) until **Confirm Detected Markers**
is applied.

## 4. Create a 7-digit Student ID region

```text
Created zone 'student_id': 70 bubbles (7 digits x 10 values)
```

## 5. Create an A/B/C/D question-set region

```text
Created zone 'set_code': 4 bubbles, symbols=('A', 'B', 'C', 'D')
```

## 6. Create Questions 1-100 with choices A/B/C/D

```text
Created 4 column region(s), 400 bubbles total
  questions_0: Q1-25
  questions_1: Q26-50
  questions_2: Q51-75
  questions_3: Q76-100
```

Four separate zones, contiguous and non-overlapping question numbers - one
`Zone` per printed column, per `docs/TEMPLATE_FORMAT.md`.

## 7. Adjust one region

The Student ID box is dragged (via the same `geometry_committed` signal path a
mouse drag produces):

```text
Student ID bounds moved: x 0.0806 -> 0.1048, y 0.1140 -> 0.1226
```

## 8. Adjust one individual bubble

Fine-tune mode is entered for the Student ID region and its first bubble is
dragged:

```text
Bubble (row=0, col=0) now has an override; total overrides in zone: 1
```

## 9. Validate the template

```text
Errors: (none)
Warnings: (none)
```

## 10. Save the template

```text
Saved: True -> demo_scratch\demo_template.omrt
Dirty after save: False
```

## 11. Close/reload it

A **second, independent** `TemplateDesignerPage` instance opens the saved file
- nothing is carried over from the page that saved it:

```text
Reloaded template: Phase 2 demo
Reloaded zones: ['student_id', 'set_code', 'questions_0', 'questions_1', 'questions_2', 'questions_3']
```

## 12. Verify logical bubble count and geometry are unchanged

```text
Total bubbles before save: 474, after reload: 474, match: True
Student ID geometry unchanged after reload: True
Bubble override survived the round trip: True

DEMO COMPLETE - all assertions held.
```

474 = 70 (student ID) + 4 (question set) + 400 (questions), matching every
number reported in steps 4-6. The reloaded document was also independently
re-verified through `services.load_template` and compared for full equality
against the reloading page's in-memory document - not merely "it loaded
without an exception".

## Reproducing this run

```bash
# from the repository root, with the project's virtual environment active
python - <<'PY'
# See the script embedded in this document's source history, or reconstruct it
# from the steps above; each step is a direct call into TemplateDesignerPage
# and omr_scanner.domain.template_authoring with no GUI event loop required.
PY
```

In practice this was run as a standalone script during development; the
commands above are the pattern rather than a literally re-runnable heredoc,
since the full script is import-heavy. `tests/gui/test_template_designer_page.py`
exercises the same sequence of operations, broken into one assertion per test,
and **is** re-run on every `pytest` invocation.
