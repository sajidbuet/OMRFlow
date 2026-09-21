# Recognition Symbols

What OMRFlow writes when a mark is not a single clean answer. These values
appear in the Scan table, the Resolve queue, CSV exports and the Processing
Log sheet of a generated report.

The rule behind all of them: **OMRFlow never resolves an ambiguous mark
silently.** If it cannot read a bubble confidently, it says so and sends it
to a person.

## Answer values

| Symbol | Meaning |
|---|---|
| `A`, `B`, `C`, `D`, … | One bubble marked, confidently. The answer |
| `_` | **Blank** — no bubble marked, and the darkest candidate is confidently below the blank threshold |
| `B-D` | **Multiple marks** — two or more bubbles marked. *Every* mark is kept, joined by `-`, so nothing is discarded |
| *(empty)* | No value recorded for this question |

A multiple-mark value lists all the marks it found, in order: `A-C`, `B-D`,
even `A-B-C-D`. It is never reduced to "the darkest one" — which bubble a
candidate meant is not a decision software should make.

## Mark statuses

Each recognised field carries a status as well as a value:

| Status | Meaning |
|---|---|
| `resolved` | Read confidently. The value stands |
| `blank` | Confidently nothing marked |
| `multiple` | Two or more marks, all kept |
| `uncertain` | Read, but not confidently — either the darkest mark is too close to the next darkest, or the darkest sits between the blank and marked thresholds. **The value is still recorded**, and the field is flagged for review |
| `unreadable` | The field could not be measured at all |

`uncertain` is the status the **Calibrate** stage exists to reduce: it usually
means the recognition thresholds do not match your scanner and paper. See
[Processing](Processing).

## Sheet statuses

Each sheet in a batch has one of these:

| Status | Meaning |
|---|---|
| `pending` | Imported, not yet processed |
| `queued` | Waiting for a worker |
| `processing` | Being read now |
| `completed` | Read, with nothing needing attention |
| `warning` | Read, but something needs review — an uncertain field, a multiple mark, or a registration concern |
| `failed` | Could not be processed. Usually registration: the four printed markers could not be found, so the page could not be aligned |
| `cancelled` | The batch was stopped before this sheet was read |

`completed` and `warning` both mean the sheet *was* read. `warning` is not an
error; it is OMRFlow asking you to look.

## What to do about each

| You see | Do this |
|---|---|
| Many `uncertain` fields | Run the **Calibrate** stage against representative scans and adjust the thresholds |
| Many `failed` sheets | Check the scan resolution, that the whole page including all four markers is on the image, and that the template matches the printed sheet |
| `multiple` marks | Resolve each in the **Resolve** stage. This is a candidate behaviour, not a recognition fault |
| `_` where you expected an answer | Usually genuine. Check one against the paper to be sure the threshold is not too strict |

## Related

- [Processing](Processing) — the Calibrate stage and thresholds
- [Review & Resolution](Review-and-Resolution) — deciding flagged fields
- `docs/recognition_engine.md` — how the decision is actually made, with the
  thresholds named
