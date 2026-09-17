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

> **Pre-release. Phases 0-2 of 11 are complete; Phase 3 is implemented and
> currently undergoing testing and stabilisation.**
> OMRFlow manages projects, rectifies a scanned sheet into its template's
> canonical page, has an interactive designer for building that template, and
> can now read the marks on a filled-in sheet, name/export the results, and
> file the processed images by roll number. None of the Phase 3 workflow has
> been validated on more than one real printed sheet.
> **Do not use it for examination processing.**

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
| 3 | Bubble mapping, recognition, batch scanning, renaming & CSV export | Implemented | In progress | 🧪 Testing |
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
  - deterministic CSV export;
  - optional renaming of processed scans using the detected roll number, with
    safe duplicate-roll handling (`2103123.jpg`, `2103123_a.jpg`,
    `2103123_b.jpg`, ...) and unresolved/uncertain roll numbers left
    unrenamed rather than misnamed;
  - per-sheet error isolation, so one bad file does not abort a batch.
- A PySide6 application shell with the eight workflow stages; **Project**,
  **Template** and **Scan** are implemented, and the remaining stages state
  which phase will implement them.

### Phase 3 testing status

Phase 3's functionality is implemented and exercised by an automated suite
(1,612 tests passing at the time of writing, plus the repository-local
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
- [x] Automated Qt GUI validation using `qtguitesting` (16/16 smoke checks,
  including the real sample recognised end-to-end through the GUI; two real
  defects were found this way and fixed)

Still open, and why Phase 3 is not marked complete:

- [ ] Validation against a broad corpus of independently, genuinely filled
  sheets (different handwriting, pencil/pen, erasures, real ambiguous marks) —
  currently one real sheet plus geometric variants of it
- [ ] Registration/alignment edge cases beyond Phase 1's documented limits
  (e.g. illumination gradients, rotation beyond ±15°, heavy cropping)
- [ ] Batch-processing stress testing at realistic exam volumes (hundreds of
  sheets)
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
pytest                   # 1,600+ tests
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
```

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
│   ├── config/               per-user settings and platform paths
│   ├── domain/               pure models: project, geometry, template,
│   │                         template_authoring (region generation, Phase 2)
│   ├── database/             SQLite schema, migrations, sessions
│   ├── services/             workflows the GUI calls: alignment, template,
│   │                         recognition, batch processing, filename
│   │                         allocation, scan import/export (Phase 3)
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
│   ├── reporting/            RESERVED - XLSX/PDF export (Phase 9; CSV export
│   │                         already exists in services/scan_export.py)
│   └── utils/                logging setup, atomic JSON
│
├── tests/
│   ├── unit/                 logic, domain models, layering rules
│   ├── integration/          services + database + file system
│   ├── gui/                  pytest-qt smoke tests, incl. the Scan workflow
│   └── fixtures/             test data (policy in docs/TESTING.md)
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
