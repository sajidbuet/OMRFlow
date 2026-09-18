# Stored recognition results

One `ScanResult`, serialised, per interesting scenario. These exist so that
**Phase 4 and Phase 5 can be built and tested without running recognition** -
a review screen, a scoring engine or a conflict queue can load a fixture,
render it, and assert against it, with no OpenCV, no template and no scan.

That is also the test of the architecture: if a later phase cannot be written
against these, recognition is not as replaceable as it claims to be.

## The files

| Fixture | What it is | Outcome | Status codes |
|---|---|---|---|
| `perfect_scan.json` | Every field and question resolved | `complete` | `ALIGNMENT_WARNING` |
| `blank_answers.json` | Four questions left unanswered | `complete` | `ALIGNMENT_WARNING`, `BLANK` |
| `multiple_marks.json` | Two questions carry two marks each (`A-C`, `B-D`) | `review` | `ALIGNMENT_WARNING`, `MULTIPLE_MARK` |
| `low_confidence.json` | Three marks between the blank and fill thresholds | `review` | `ALIGNMENT_WARNING`, `LOW_CONFIDENCE` |
| `invalid_roll.json` | One roll digit column left empty (`12_317`) | `complete` | `ALIGNMENT_WARNING`, `ROLL_UNREADABLE` |
| `invalid_set.json` | Two set codes marked at once | `review` | `ALIGNMENT_WARNING`, `MULTIPLE_MARK`, `SET_UNREADABLE` |
| `mixed_ambiguity.json` | A blank, a double mark, a faint mark and a cross, on one sheet | `review` | `ALIGNMENT_WARNING`, `BLANK`, `LOW_CONFIDENCE`, `MULTIPLE_MARK` |
| `duplicate_roll_a.json` | Roll `120317`, answers all `B` | `complete` | `ALIGNMENT_WARNING` |
| `duplicate_roll_b.json` | **The same roll**, answers all `C` - the pair the duplicate-naming rule exists for | `complete` | `ALIGNMENT_WARNING` |
| `alignment_warning.json` | Registered, but the page sits hard against the image border | `complete` | `ALIGNMENT_WARNING` |
| `orientation_failure.json` | No orientation mark: which way up the page is cannot be established, so nothing is read | `registration_failed` | `ALIGNMENT_FAILED`, `ORIENTATION_FAILED` |

Every fixture carries `ALIGNMENT_WARNING` because every *synthetic* page earns
`MULTIPLE_CORNER_CANDIDATES` - its own printed bubbles and frames look enough
like registration markers to be worth mentioning, exactly as the real sample
sheet's do. That is honest engine behaviour, not a defect in the fixtures.

## Reading one

```python
import json
from pathlib import Path
from omr_scanner.services.recognition_models import ScanResult

payload = json.loads(Path("tests/fixtures/recognition/multiple_marks.json").read_text("utf-8"))
result = ScanResult.from_dict(payload)

result.identifier_value        # "120317"
result.answer(3).value         # "A-C"
result.answer(3).needs_review  # True
result.has_status("MULTIPLE_MARK")
```

## Regenerating

```bash
python scripts/build_recognition_fixtures.py            # committed form
python scripts/build_recognition_fixtures.py --pretty   # indented, for reading
```

The script renders a sheet for each scenario from the synthetic answer-sheet
template, **actually recognises it**, and writes the result. Nothing here is
hand-written JSON: a hand-written fixture drifts from what the engine emits,
and the tests written against it then verify a fiction.

Two things are normalised so that a rebuild produces no spurious diff: the
source path becomes a bare file name, and the timestamp is blanked. Floats are
rounded to six decimals, which is far finer than a fill ratio measured from
eight-bit pixels means, and is the difference between 500 KB and 850 KB of
committed JSON.

## When these change

Regenerating is expected when the result **schema** changes, and the diff
should be read rather than waved through: these files are a published contract,
and a surprising change in one is a change every later phase will feel.

A change in the *values* (a different answer, a different status) means the
recognition engine now reads these sheets differently. That may be an
improvement or a regression, but it is never routine.

## Roll numbers here are fictional

`120317` and its neighbours are invented. Committed fixtures must never carry a
real candidate's identifier; a real validation corpus lives outside the
repository (see `local_test_data/README.md`).
