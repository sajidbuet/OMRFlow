# Local validation data (never committed)

This folder is where a **real** OMR dataset goes: actual scanned answer sheets
and the answers a human verified on them. Everything inside it except this
README is ignored by git, deliberately and permanently.

Real sheets carry candidate identifiers, handwriting and sometimes names. They
are exactly the data this project promises stays on the machine that processes
it, and a public repository is the one place they must never be.

## Expected layout

The same shape the synthetic generator produces, so that one benchmark command
works on either:

```text
local_test_data/
└── <dataset name>/
    ├── images/
    │   ├── scan0001.jpg
    │   ├── scan0002.jpg
    │   └── ...
    ├── ground_truth/
    │   ├── scan0001.json
    │   ├── scan0002.json
    │   └── ...
    └── manifest.json          optional, but recommended
```

Ground-truth documents use the schema in
`src/omr_scanner/evaluation/ground_truth.py`:

```json
{
  "schema_version": 1,
  "scan": "scan0001.jpg",
  "roll": "2103123",
  "set_code": "A",
  "answers": { "1": "B", "2": "", "3": "A-C" },
  "ambiguous": [17],
  "human_verified": true,
  "reviewer": "initials or role, not a full name",
  "dataset_version": "2026-10",
  "notes": "Q17 half-erased; either reading is acceptable."
}
```

- `""` means the question was left blank.
- `"A-C"` means two bubbles were genuinely marked; both are recorded.
- `ambiguous` lists questions whose mark is genuinely borderline, where
  *flagging* the question is correct behaviour and is scored as such.
- A question left out of `answers` is not checked, which is how a partially
  verified sheet is recorded honestly.

## Running against it

```bash
python -m omr_scanner.tools.benchmark_recognition local_test_data/october2026 \
    --template templates/physics.omrt \
    --report local_test_data/october2026/report \
    --workers auto
```

Everything runs locally. Nothing is uploaded, no analytics are collected, and
no network request is made at any point in recognition, benchmarking or
reporting.

## Verifying ground truth

The only trustworthy ground truth for a real sheet is a person reading the
paper. Two practices are worth the effort:

1. **Do not transcribe from OMRFlow's own output.** Ground truth derived from
   the engine measures nothing except self-consistency.
2. **Record who checked it and when.** `human_verified`, `reviewer` and
   `dataset_version` exist so that a disagreement between a benchmark and a
   later re-check can be traced.

Use a role or initials rather than a full name in `reviewer`: this file travels
with the dataset, and the dataset may be shared inside an institution.

## Privacy

Before sharing a dataset with anyone - including a bug report - consider
whether the images carry names, signatures or anything else identifying, and
whether the roll numbers are real. If in doubt, reproduce the problem with
`python -m omr_scanner.tools.make_dataset` instead, which generates fictional
sheets that can be attached to anything.
