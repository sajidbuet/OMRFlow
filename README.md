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

## Development status

> **Pre-release. Phase 0 of 11 is complete.**
> OMRFlow currently manages projects. It cannot yet read answer sheets.
> **Do not use it for examination processing.**

**What works today**

- Create a project (a folder with a metadata file, a SQLite database and the
  standard working sub-directories).
- Close and reopen a project; invalid or damaged projects are refused with a
  readable message.
- Load, validate and save versioned `.omrt` template documents.
- A PySide6 application shell with the eight workflow stages, seven of which
  state which phase will implement them.

**What does not exist yet**

Image processing, marker detection, orientation and perspective correction,
bubble recognition, the template designer, batch processing, conflict
resolution, attendance reconciliation, answer keys, scoring and reporting.

Current detail: [`development/CURRENT_STATE.md`](development/CURRENT_STATE.md).
Plan: [`development/ROADMAP.md`](development/ROADMAP.md).

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
pytest                   # 128 tests
ruff check .
mypy
```

All three must pass before a development phase is considered complete.

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
│   ├── domain/               pure models: project, geometry, template
│   ├── database/             SQLite schema, migrations, sessions
│   ├── services/             workflows the GUI calls
│   ├── gui/                  PySide6 window and workflow pages
│   ├── imaging/              RESERVED - pixel algorithms (Phase 1/3)
│   ├── recognition/          RESERVED - value interpretation (Phase 3/6)
│   ├── reporting/            RESERVED - CSV/XLSX/PDF export (Phase 9)
│   └── utils/                logging setup, atomic JSON
│
├── tests/
│   ├── unit/                 logic, domain models, layering rules
│   ├── integration/          services + database + file system
│   ├── gui/                  pytest-qt smoke tests
│   └── fixtures/             test data (policy in docs/TESTING.md)
│
├── resources/
│   ├── templates/            illustrative .omrt example
│   └── icons/                RESERVED
│
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
| [`docs/IMAGE_PROCESSING.md`](docs/IMAGE_PROCESSING.md) | The planned recognition pipeline (not implemented) |
| [`docs/TESTING.md`](docs/TESTING.md) | Testing strategy and the test-fixture policy |
| [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) | How to use what currently exists |
| [`docs/decisions/`](docs/decisions/) | Architecture decision records |

---

## Planned capabilities

The following describe the finished system. None of it works yet.

**Template designer.** Load a reference sheet, draw recognition zones, define the
four registration markers and the orientation marker, and configure field types:
numeric, alphanumeric, set code, candidate id, MCQ question blocks and ignored
regions. Templates use resolution-independent normalised coordinates and are
stored as versioned `.omrt` documents.

**Automatic scan alignment.** Registration markers drive rotation, skew,
perspective and scale correction, transforming every raw scan into the canonical
coordinate system of the template. Original scans are never modified.

**Confidence-aware recognition.** Bubbles are measured (mean intensity,
dark-pixel ratio, filled area, local background, relative darkness), not merely
thresholded, so uncertain responses can be flagged instead of silently accepted.

**Missing and multiple marks.** Ambiguity is represented explicitly rather than
guessed. A competing pair of roll numbers may display as `?10018-10028` and a
missing digit as `?1__18`; the database retains the structured measurements
behind those strings.

**Conflict resolution.** A review interface showing the original sheet, the
normalised sheet, the highlighted field, the zoomed region and the detected
alternatives. A manual correction never overwrites the machine value - it is
recorded alongside it with a timestamp and a reason.

**Candidate and attendance reconciliation.** Compare registered candidates,
recorded attendance and detected scripts; surface unknown roll numbers, duplicate
scripts, absent-with-script and present-without-script cases.

**Answer keys and scoring.** Keys entered manually or read from solution sheets,
independent per question paper set, verified before use. Configurable marks for
correct, incorrect and blank answers, with negative marking optional.

**Reports.** Roll-wise (including absentees) and merit-wise Excel workbooks with
a user-editable layout, plus PDF export.

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

## License

Intended to be released as open-source software under a permissive licence
(Apache 2.0 or MIT). The final choice will be made before the first public
release.

---

## OMRFlow

**From scanned marks to verified results.**
