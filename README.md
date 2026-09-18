# OMRFlow

### Smart Mark Checker — Open-Source OMR Examination Processing

**OMRFlow** is an open-source desktop application for designing OMR templates,
processing scanned answer sheets, resolving recognition conflicts, reconciling
candidate attendance, evaluating MCQ examinations and generating auditable
examination results.

The goal is a flexible, transparent, locally operated alternative to proprietary
OMR examination-processing systems:

**ordinary image scanner + configurable template + transparent recognition +
human verification + reproducible result processing**

---

## Development Status

> **Pre-release. Phases 0-2 of 11 are complete; Phase 3 (Recognition Engine
> v1) is implemented and architecturally stabilised, and is undergoing
> testing.**
> OMRFlow manages projects, rectifies a scanned sheet into its template's
> canonical page, has an interactive designer for building that template, and
> can now read the marks on a filled-in sheet, name/export the results, and
> file the processed images by roll number. None of the Phase 3 workflow has
> been validated on more than one real printed sheet.
> **Do not use it for examination processing.**
>
> **Phase 3 v1 is architecturally stabilized but recognition accuracy remains
> under active validation pending a large real-world OMR dataset.**

OMRFlow is being developed incrementally, in the defined phases listed in
[`development/ROADMAP.md`](development/ROADMAP.md). Each phase is implemented,
covered with automated tests, and stabilised before the project moves toward a
production-ready release — but **implemented is not the same as complete**: a
phase is only marked complete once both its implementation *and* its required
validation are satisfied. That distinction matters most right now for Phase 3,
which is implemented and passing its automated test suite, but has not yet been
validated against a broad, real-world set of filled sheets.

| Phase | Description | Development | Testing | Status |
|---|---|---|---|---|
| 0 | Architecture & repository foundation | Complete | Complete | ✅ Complete |
| 1 | OMR geometry & alignment engine | Complete | Complete | ✅ Complete |
| 2 | Template data model & template designer core | Complete | Complete | ✅ Complete |
| 3 | Recognition Engine v1: bubble mapping, recognition, batch scanning, renaming & CSV export | Implemented & architecturally hardened | In progress | 🧪 Testing |
| 4 | Template calibration & validation | Pending | Not started | ⏳ Pending |
| 5 | Batch scan processing pipeline (persistence, resume) | Pending | Not started | ⏳ Pending |
| 6 | Conflict detection & human resolution | Pending | Not started | ⏳ Pending |
| 7 | Candidate & attendance reconciliation | Pending | Not started | ⏳ Pending |
| 8 | Answer-key & scoring engine | Pending | Not started | ⏳ Pending |
| 9 | Result management & reporting | Pending | Not started | ⏳ Pending |
| 10 | Integration, recovery & production hardening | Pending | Not started | ⏳ Pending |
| 11 | Release, user documentation & packaging | Pending | Not started | ⏳ Pending |

Phase titles and descriptions for 4-11 are taken directly from
[`development/ROADMAP.md`](development/ROADMAP.md), which is the authoritative
plan; see that document for each phase's purpose, deliverables and exit
criteria.

### Status legend

- ✅ **Complete** — implementation and its required testing are both finished.
- 🧪 **Testing** — implementation substantially complete; validation and
  stabilisation ongoing.
- 🚧 **In development** — active implementation.
- ⏳ **Pending** — not yet started.

### What works today

- Create, close, reopen and validate a project (a folder with a metadata file,
  a SQLite database and the standard working sub-directories); invalid or
  damaged projects are refused with a readable message.
- Load, validate and save versioned `.omrt` template documents.
- **Geometric normalisation** (Phase 1): detect the four printed registration
  markers on a scan, resolve which way up the page is, and correct rotation,
  translation, scale, skew and perspective into the canonical page the
  template declares.
- **Interactive template designer** (Phase 2): load a reference sheet image,
  detect and adjust its registration markers, draw student ID / question-set /
  question / custom-bubble regions with generated bubble grids, fine-tune
  individual bubbles, undo/redo, validate and save. See
  `docs/template_designer.md`.
- **Scan / recognition workflow** (Phase 3, *implemented, testing in
  progress*): load a template, import one scan or a whole folder of them,
  process them in the background without freezing the GUI, and review the
  result. Per the automated test suite and the recorded validation run in
  `development/PHASE_03_HANDOFF.md`, this currently includes:
  - orientation detection and marker-based registration, with a scan that
    cannot be confidently registered flagged rather than guessed at;
  - template-to-scan coordinate mapping, so recognition follows the template's
    own regions rather than hard-coded page positions;
  - roll/ID (numeric), set-code and question-block recognition;
  - explicit handling of blank and multiple marks (e.g. `B-D`) and uncertain
    reads, shown in the GUI and carried into the export rather than silently
    resolved;
  - a recognition preview/overlay over the corrected sheet;
  - **configurable multicore batch recognition**: several independent sheets
    read concurrently, one whole page per CPU worker, with the processing mode
    (**Automatic**, **Single core**, **Custom**) chosen in *File > Settings >
    Processing* and remembered between sessions. Results, output file names and
    CSV row order are identical to a single-core run on any number of cores;
  - deterministic CSV export;
  - optional renaming of processed scans using the detected roll number, with
    safe duplicate-roll handling (`2103123.jpg`, `2103123_a.jpg`,
    `2103123_b.jpg`, ...) and unresolved/uncertain roll numbers left
    unrenamed rather than misnamed;
  - per-sheet error isolation, so one bad file does not abort a batch.
- A PySide6 application shell with the eight workflow stages; **Project**,
  **Template** and **Scan** are implemented, and the remaining stages state
  which phase will implement them.

### Phase 3 architectural hardening

Phase 3 was subsequently hardened into a **replaceable recognition
subsystem**, so that Phases 4 and 5 can be built against it now and a future
Recognition Engine v2 can replace it without rewriting them:

- **Recognition API established.** One entry point -
  `RecognitionEngine.process(image, template)` - behind which no caller needs
  to know about thresholds, contours or homographies. The older
  `recognise_scan()` function remains supported.
- **Structured `RecognitionResult`.** Plain, versioned, JSON-serialisable data:
  the values, machine-readable status codes, the engine and template identity,
  scan-quality metrics, per-stage timings, and the **raw per-bubble
  measurements** behind every decision - so a future recalibration can re-score
  a batch without re-reading a single image.
- **Measurement separated from decision**, with every threshold still owned by
  the template.
- **Diagnostics.** Optional staged debug images and a headless annotated
  overlay per sheet, switchable from *File > Settings > Diagnostics* or the
  command line, and off by default.
- **Headless operation.** `python -m omr_scanner.tools.recognise` reads a scan
  or a folder with no GUI at all — asserted by a test that runs recognition in
  an interpreter where Qt was never imported.
- **Synthetic test generator.** Reproducible labelled datasets rendered from a
  real template, with controlled mark styles, geometry, exposure and structural
  damage, and ground truth written beside every image.
- **Benchmark framework.** Scores the engine against ground truth, classifies
  every disagreement by kind, writes machine-readable reports and compares a
  run against a stored baseline.
- **Regression fixtures.** Eleven stored recognition results covering the
  scenarios later phases must handle, usable with no engine present.
- **Multicore-ready processing** (see above), with the worker pool reading one
  whole page per process.
- **Real-dataset validation pending.** Everything above is infrastructure;
  accuracy is still a Phase 3 open question.

Details: [`docs/recognition_engine.md`](docs/recognition_engine.md).

### Phase 3 testing status

Phase 3's functionality is implemented and exercised by an automated suite
(1,935 tests passing at the time of writing, plus the repository-local
`qtguitesting` Qt GUI harness), but it has only been run end-to-end against
**one real scanned sheet** (`examples/ECE-0000.png`) — validated with correct
roll number, set code and all 100 answers — plus geometrically distorted copies
of that same sheet and synthetic pages. It has not yet been run against a
broad, independently filled corpus (varied handwriting, pencil vs. pen,
erasures, genuinely ambiguous marks, different scanners), which is the
condition under which Phase 3 will be marked complete. Full detail is in
[`development/PHASE_03_HANDOFF.md`](development/PHASE_03_HANDOFF.md).

Confirmed by the current automated suite:

- [x] Recognition-accuracy validation (real sample: 100/100 answers, roll and
  set code correct, 0 flagged; synthetic corpus covering blank/single/multiple/
  uncertain marks)
- [x] Rotation/orientation testing (±3° rotation, exact 90°/180°/270° turns,
  rescaling, translation, perspective distortion, JPEG re-encoding — the real
  sample reads identically under each)
- [x] Duplicate roll-number handling (`_a`/`_b`/`_c`/... and beyond 26 via
  `_aa`)
- [x] Existing-output-filename collision handling (a pre-existing file is
  never overwritten)
- [x] CSV export validation (column order, question ordering, Unicode,
  escaping, duplicate-name recording, determinism)
- [x] Automated Qt GUI validation using `qtguitesting` (20/20 smoke checks,
  including the real sample recognised end-to-end through the GUI; three real
  defects were found this way and fixed)
- [x] Multicore recognition validation (a batch read across worker processes
  produces the same results, in the same order, as one read on a single core;
  also checked through the GUI on the real sample)
- [x] Single-core vs multicore result consistency (the same dataset processed
  at 1, 2 and 4 workers exports **byte-identical** CSVs — values, statuses,
  confidences, output names and row order)
- [x] Parallel duplicate-roll collision testing (eight sheets recognising to
  one roll number, read on four workers, produce `2103123.png`, `_a` ... `_g`;
  nothing overwritten, every CSV row naming the file actually written)
- [x] Worker failure isolation (corrupt, missing and unregistrable images fail
  individually; a worker process that dies outright becomes one failed scan,
  not a failed batch; no worker process outlives a run, a cancellation or the
  window)
- [x] Multicore GUI responsiveness (the event loop keeps delivering queued
  progress signals throughout a four-worker run; the progress bar advances
  monotonically to the scan count and the completion summary is shown)
- [x] Performance benchmarking (48 real scans at 1/2/4/8/12/16 workers, with
  peak memory; measured results and the reasoning behind the automatic worker
  cap are recorded in `docs/scan_workflow.md` §10)
- [x] Recognition API and result contract (engine runs headlessly with no Qt
  imported; `ScanResult` serialises, round-trips and refuses a newer schema)
- [x] Synthetic dataset generation (reproducible from a seed; labels verified
  by recognising the generated sheets; clean profile scores 100%)
- [x] Benchmark harness and error categorisation (summary metrics, per-error
  CSV, baseline comparison — all exercised by tests)
- [x] Diagnostics (staged images and overlay produced on demand, nothing
  written by default, and generating them provably does not change a result)
- [x] Stored result fixtures for Phases 4-5 (11 scenarios, loadable with no
  recognition engine present)

Still open, and why Phase 3 is not marked complete:

- [ ] Validation against a broad corpus of independently, genuinely filled
  sheets (different handwriting, pencil/pen, erasures, real ambiguous marks) —
  currently one real sheet plus geometric variants of it
- [ ] Registration/alignment edge cases beyond Phase 1's documented limits
  (e.g. illumination gradients, rotation beyond ±15°, heavy cropping)
- [ ] Batch-processing stress testing at realistic exam volumes (hundreds of
  sheets); the largest measured run so far is 48 scans
- [ ] Multicore validation on hardware other than the 16-thread Windows
  development machine (core counts, memory limits and `spawn` behaviour all
  differ; Linux and macOS are untested)
- [ ] **Threshold calibration against real marks.** The defaults come from one
  real sheet and synthetic pages
- [ ] **Confidence calibration.** What the engine reports is a bounded
  *decision score*, not a probability; the benchmark reports accuracy by band
  so it can be checked once there is data to check it against
- [ ] Difficult handwriting and mark-shape analysis. A synthetic mixed dataset
  already shows tick-shaped marks producing a ~33% false-blank rate against 0%
  for crosses and scribbles, because the measurement is coverage-based; whether
  real ticks behave the same way is exactly what the corpus is for
- [ ] Recognition accuracy and performance optimisation, once there is real
  data to optimise against
- [ ] Final Phase 3 regression sign-off once the above are addressed

### Development philosophy

OMRFlow follows an incremental development process. Major functionality is
introduced in phases, followed by automated testing, GUI validation, regression
testing and stabilisation before a phase is considered complete. This does not
mean every phase must be completely frozen before work on the next begins —
Phase 4 (template calibration) and Phase 5 (persistent batch processing) both
build on Phase 3's recognition engine, for example — but a phase's own status
in the table above reflects its own testing state, not merely whether its code
exists.

### Pending development

Phases 4-11 have not started. Their titles, purposes and deliverables as
currently planned are documented in
[`development/ROADMAP.md`](development/ROADMAP.md); this project does not
promise a delivery date for any of them, and scopes may be refined as earlier
phases surface real requirements.

Current detail: [`development/CURRENT_STATE.md`](development/CURRENT_STATE.md).
Plan: [`development/ROADMAP.md`](development/ROADMAP.md).
Phase 3 handoff: [`development/PHASE_03_HANDOFF.md`](development/PHASE_03_HANDOFF.md).

---

## Installation

Requires **Python 3.12 or newer**.

```powershell
git clone https://github.com/sajidbuet/OMRflow.git
cd OMRflow
python -m venv .venv
.venv\Scripts\Activate.ps1          # Linux/macOS: source .venv/bin/activate
pip install -e .                    # add ".[dev]" for the development tools
```

## Running

```bash
python -m omr_scanner                  # from a source checkout
omrflow                                # console entry point
omrflow "C:/Exams/Physics Midterm"     # open a project on start-up
```

## Testing

```bash
pip install -e ".[dev]"
pytest                   # 1,900+ tests
ruff check .
mypy
```

All three must pass before a development phase is considered complete. Passing
tests are a precondition for completion, not proof of it by themselves — see
[Development Status](#development-status) above for what "complete" requires
for a given phase.

## Developer tools

Not user-facing, but the quickest way to see the alignment engine work:

```bash
# Render a synthetic sheet, distorted by a known transform
python -m omr_scanner.tools.make_test_sheet scan.png --rotate 6 --perspective 0.02 --seed 7

# Align it, print what was measured, and write diagnostic overlays
python -m omr_scanner.tools.align_image scan.png --output aligned.png --debug debug/

# Measure batch throughput on this machine at several worker counts
python scripts/benchmark_batch.py --scans 48 --workers 1,2,4,8
```

Phase 3 recognition, entirely without the GUI:

```bash
# Read one scan or a folder; write JSON results, overlays, or a full debug dump
python -m omr_scanner.tools.recognise scans/ --template sheet.omrt \
    --json-dir out/results --overlay-dir out/overlays --workers auto

# Generate a reproducible labelled test dataset from a real template
python -m omr_scanner.tools.make_dataset out/dataset --template sheet.omrt \
    --count 24 --profile mixed --seed 20260918

# Score recognition against that dataset, classifying every disagreement
python -m omr_scanner.tools.benchmark_recognition out/dataset \
    --template sheet.omrt --report out/benchmark

# ...and compare a change against the run before it
python -m omr_scanner.tools.benchmark_recognition out/dataset \
    --template sheet.omrt --baseline out/benchmark/summary.json
```

Diagnostics (the corrected page, an annotated overlay, every bubble's
measurement, and the result as JSON) come from `--diagnostics out/diagnostics`
on the command line, or *File > Settings > Diagnostics* in the application.
Both are off by default. Full detail:
[`docs/recognition_engine.md`](docs/recognition_engine.md).

---

## Repository structure

```text
OMRflow/
├── pyproject.toml            packaging + pytest/Ruff/mypy configuration
├── CHANGELOG.md
│
├── src/omr_scanner/
│   ├── main.py               entry point: CLI, logging, start-up
│   ├── errors.py             application exception hierarchy
│   ├── config/               per-user settings, platform paths and the
│   │                         CPU-worker policy (Phase 3)
│   ├── domain/               pure models: project, geometry, template,
│   │                         template_authoring (region generation, Phase 2)
│   ├── database/             SQLite schema, migrations, sessions
│   ├── services/             workflows the GUI calls: alignment, template,
│   │                         recognition, batch processing (incl. the
│   │                         multicore worker pool), filename allocation,
│   │                         scan import/export (Phase 3)
│   ├── gui/                  PySide6 window and workflow pages
│   │   ├── template_designer/  interactive .omrt editor (Phase 2)
│   │   └── scan/                scan/recognition workflow page (Phase 3,
│   │                            testing in progress)
│   ├── imaging/              pixel algorithms: alignment, plus per-bubble
│   │                         fill-metric measurement (Phase 3)
│   ├── tools/                developer command line utilities
│   ├── recognition/          value interpretation: decide, fields (Phase 3,
│   │                         testing in progress; conflict resolution is
│   │                         Phase 6)
│   ├── evaluation/           QA above the engine: ground-truth schema,
│   │                         synthetic dataset generator, benchmark and
│   │                         error categories (Phase 3)
│   ├── reporting/            RESERVED - XLSX/PDF export (Phase 9; CSV export
│   │                         already exists in services/scan_export.py)
│   └── utils/                logging setup, atomic JSON
│
├── tests/
│   ├── unit/                 logic, domain models, layering rules
│   ├── integration/          services + database + file system
│   ├── gui/                  pytest-qt smoke tests, incl. the Scan workflow
│   └── fixtures/             test data (policy in docs/TESTING.md), incl.
│                             recognition/ - stored results for Phases 4-5
│
├── local_test_data/          where a real validation corpus goes; ignored by
│                             git except its README (Phase 3)
│
├── resources/
│   ├── templates/            illustrative .omrt example
│   └── icons/                RESERVED
│
├── examples/
│   ├── templates/            worked .omrt examples (Phase 2), incl. the
│   │                         template for the real sample sheet (Phase 3)
│   └── ECE-0000.png          the one real scanned sheet Phase 3 has been
│                             validated against
├── docs/                     architecture, data model, formats, ADRs
└── development/              roadmap, current state, phase handoffs
```

## Documentation

| Document | Contents |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Layers, dependency direction, GUI/service separation, error and logging rules |
| [`docs/DEVELOPMENT_GUIDE.md`](docs/DEVELOPMENT_GUIDE.md) | Setup, commands, conventions, where each kind of setting belongs |
| [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) | Entities across all phases and their relationships |
| [`docs/TEMPLATE_FORMAT.md`](docs/TEMPLATE_FORMAT.md) | The `.omrt` format, with a worked example |
| [`docs/template_designer.md`](docs/template_designer.md) | The interactive template designer: workflow, shortcuts, architecture |
| [`docs/IMAGE_PROCESSING.md`](docs/IMAGE_PROCESSING.md) | The alignment and recognition pipeline: algorithms, accuracy, failure modes and limits |
| [`docs/scan_workflow.md`](docs/scan_workflow.md) | The Scan / recognition workflow: importing, batch processing, renaming and CSV export (Phase 3) |
| [`docs/recognition_engine.md`](docs/recognition_engine.md) | The recognition subsystem for developers: the engine API, the `ScanResult` contract, diagnostics, synthetic datasets and the benchmark harness (Phase 3) |
| [`docs/TESTING.md`](docs/TESTING.md) | Testing strategy and the test-fixture policy |
| [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) | How to use what currently exists |
| [`docs/decisions/`](docs/decisions/) | Architecture decision records |

---

## Capabilities

The following describes the finished system. Each capability is tagged with
the phase that delivers it and its current state; anything tagged **planned**
does not exist yet. Phase 3 items are implemented but still "testing in
progress" in the sense described in [Development Status](#development-status)
above.

**Template designer** *(Phase 2, implemented)*. Load a reference sheet, draw
recognition zones, define the four registration markers and the orientation
marker, and configure field types: numeric, alphanumeric, set code, candidate
id, MCQ question blocks and ignored regions. Templates use
resolution-independent normalised coordinates and are stored as versioned
`.omrt` documents.

**Automatic scan alignment** *(Phase 1, implemented)*. Registration markers
drive rotation, skew, perspective and scale correction, transforming every raw
scan into the canonical coordinate system of the template. Original scans are
never modified.

**Confidence-aware recognition** *(Phase 3, implemented; testing in
progress)*. Bubbles are measured (local fill ratio against a locally estimated
paper/ink level, relative darkness among competing candidates), not merely
thresholded against a fixed value, so uncertain responses are flagged instead
of silently accepted.

**Missing and multiple marks** *(Phase 3, implemented; testing in progress)*.
Ambiguity is represented explicitly rather than guessed: an unmarked response
is blank, and multiple marks on one question are reported with both kept (for
example `B-D`), never collapsed to a single answer. A roll number or set code
that could not be read reliably is flagged rather than filed under a guessed
value.

**Multicore batch processing** *(Phase 3, implemented; testing in progress)*.
Independent sheets are read concurrently, one complete page per CPU worker
process, while the Qt interface stays in the main process and stays responsive.
The processing mode is a user setting - **Automatic** (OMRFlow chooses,
leaving the machine room to breathe), **Single core** (deterministic
troubleshooting, low-memory machines, benchmark baselines) or **Custom** (a
worker count of your own, up to the CPU threads detected). Parallel execution
never changes what is recognised: output names are assigned centrally, in batch
order, by a single allocator in the main process, so duplicate roll numbers,
the scan list and the CSV come out identically however the work was divided.

**Batch scanning, export and safe renaming** *(Phase 3, implemented; testing
in progress)*. Import one scan or a whole folder; process in the background
without freezing the GUI; export a deterministic CSV; optionally copy
processed scans into an output folder named after the detected roll number,
with duplicate rolls safely suffixed (`2103123.jpg`, `2103123_a.jpg`,
`2103123_b.jpg`, ...) so no file is ever overwritten, and unresolved rolls left
unrenamed rather than misnamed. See `docs/scan_workflow.md`.

**Conflict resolution** *(planned, Phase 6)*. A review interface showing the
original sheet, the normalised sheet, the highlighted field, the zoomed region
and the detected alternatives. A manual correction never overwrites the
machine value - it is recorded alongside it with a timestamp and a reason.

**Candidate and attendance reconciliation** *(planned, Phase 7)*. Compare
registered candidates, recorded attendance and detected scripts; surface
unknown roll numbers, duplicate scripts, absent-with-script and
present-without-script cases.

**Answer keys and scoring** *(planned, Phase 8)*. Keys entered manually or read
from solution sheets, independent per question paper set, verified before use.
Configurable marks for correct, incorrect and blank answers, with negative
marking optional.

**Reports** *(planned, Phase 9)*. Roll-wise (including absentees) and
merit-wise Excel workbooks with a user-editable layout, plus PDF export. (CSV
export of raw recognition results already exists, from Phase 3.)

**Auditability.** Anything that can change a result is recorded append-only:
machine value, corrected value, timestamp, operation, reason.

---

## Technology

| Component | Technology |
|---|---|
| Language | Python 3.12+ |
| Desktop GUI | PySide6 / Qt |
| Image processing | OpenCV (headless), NumPy |
| Database | SQLite via SQLAlchemy 2.x |
| Validation | Pydantic 2 |
| Tabular / Excel | pandas, openpyxl |
| Testing | pytest, pytest-qt |
| Lint / types | Ruff, mypy (strict) |

Initial target platform is Windows; the application core avoids
platform-specific logic, so Linux and macOS support remains open.

## Design principles

> **Readable over clever** · **Explicit over implicit** · **Configurable over
> hard-coded** · **Modular over monolithic** · **Testable over tightly coupled**
> · **Human-verifiable over black-box automation**

OMR algorithms must never live inside GUI event handlers:

```text
GUI  ->  Service  ->  Alignment / Recognition  ->  Database
```

The layering is not merely documented - `tests/unit/test_architecture.py` fails
the build if the GUI imports OpenCV or SQLAlchemy, or if the imaging layer
imports Qt.

## Data privacy and examination integrity

OMRFlow runs locally. Candidate identities, answer sheets, attendance,
answer keys and results never need to leave the machine, and candidate data is
kept out of application logs by policy.

Successful processing is not the same as a correct result. For high-stakes
examinations, institutions should retain independent procedures for template
validation, answer-key verification, unresolved-conflict review, attendance
reconciliation, result verification, output approval and archival of original
scans. The software supports such controls; it does not replace institutional
responsibility.

Never include confidential candidate or examination data in bug reports or test
fixtures.

## Contributing

Contributions are welcome once the architecture has stabilised. Before opening a
pull request, run `pytest`, `ruff check .` and `mypy`, and read
[`docs/DEVELOPMENT_GUIDE.md`](docs/DEVELOPMENT_GUIDE.md). Please keep unrelated
architectural changes and features in separate pull requests.

### Development status maintenance

The [Development Status](#development-status) table and its testing checklist
should be updated whenever a phase is started, implemented, enters testing, or
is completed — including moving individual checklist items from unchecked to
checked only once repository evidence (a passing automated test, a documented
validation run) actually confirms them, never merely because related code or a
test file now exists. Testing status and known pending work should stay
synchronised with `development/CURRENT_STATE.md` and the relevant
`development/PHASE_NN_HANDOFF.md`, so this README remains an accurate
high-level status page rather than a second, drifting source of truth.

## License

Intended to be released as open-source software under a permissive licence
(Apache 2.0 or MIT). The final choice will be made before the first public
release.

## Developer

OMRFlow is developed by [Dr. Sajid Muhaimin Choudhury](https://www.sajid.bd).

---

## OMRFlow

**From scanned marks to verified results.**
