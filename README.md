# OMRFlow

### Smart Mark Checker — Open-Source OMR Examination Processing

**OMRFlow** is an open-source desktop application for designing OMR templates, processing scanned answer sheets, resolving recognition conflicts, reconciling candidate attendance, evaluating MCQ examinations, and generating auditable examination results.

The project aims to provide a flexible, transparent, and locally operated alternative to proprietary Optical Mark Recognition (OMR) examination-processing systems.

> **Project status:** Early development / pre-release  
> OMRFlow is currently being developed in stages. It is **not yet recommended for production or high-stakes examination processing**.

---

## Why OMRFlow?

Many OMR systems are tied to:

- proprietary scanners;
- fixed answer-sheet formats;
- proprietary recognition software;
- rigid examination workflows;
- cloud services;
- limited access to recognition diagnostics; or
- closed result-processing pipelines.

OMRFlow is designed around a different model:

**ordinary image scanner + configurable template + transparent recognition + human verification + reproducible result processing**

The objective is not simply to detect filled bubbles.

OMRFlow is intended to manage the complete workflow:

```text
Template
   ↓
Scan
   ↓
Normalize
   ↓
Recognize
   ↓
Resolve Conflicts
   ↓
Reconcile Attendance
   ↓
Load Answer Key
   ↓
Calculate Results
   ↓
Verify
   ↓
Export Excel / PDF
```

---

# Planned Features

## Template Designer

Create reusable OMR templates using a visual editor.

Planned capabilities include:

- load a reference OMR image or PDF;
- draw and resize recognition zones;
- configure different field types;
- define registration markers;
- define page-orientation markers;
- save and reload templates;
- display bubble locations as graphical overlays;
- use normalized coordinates so templates are resolution-independent.

Planned field types include:

- numeric;
- alphanumeric;
- set code;
- candidate ID;
- MCQ questions;
- registration markers;
- orientation markers;
- ignored regions;
- custom fields.

Templates will use a versioned human-readable format with the proposed extension:

```text
.omrt
```

---

## Automatic Scan Alignment

Scanned sheets may differ slightly in:

- position;
- rotation;
- scale;
- skew;
- perspective;
- scanner margins.

OMRFlow will use registration markers to transform each raw scan into the canonical coordinate system defined by the active template.

Planned processing pipeline:

```text
Raw Scan
    ↓
Image Preprocessing
    ↓
Registration Marker Detection
    ↓
Orientation Detection
    ↓
Corner Ordering
    ↓
Perspective Transformation
    ↓
Scale Normalization
    ↓
Canonical OMR Image
```

The original scanned image will always be preserved.

---

## Confidence-Aware OMR Recognition

OMRFlow is intended to retain more information than a simple binary:

```text
MARKED / UNMARKED
```

Bubble measurements may include:

- mean intensity;
- dark-pixel ratio;
- filled-area ratio;
- local background intensity;
- relative darkness compared with neighbouring bubbles;
- recognition confidence.

This allows uncertain responses to be identified for human inspection rather than silently accepted.

---

## Missing and Multiple Marks

Numeric and set-code fields will explicitly represent ambiguous values.

For example:

```text
10018
```

may be recognized normally, while competing marks might be exported as:

```text
?10018-10028
```

A missing digit may appear as:

```text
?1__18
```

The underlying project database will retain structured recognition information rather than relying only on these display strings.

---

## Conflict Resolution

OMRFlow will provide a dedicated human-in-the-loop review interface.

The operator will be able to inspect:

- the complete scanned sheet;
- normalized sheet;
- highlighted problematic field;
- zoomed field image;
- detected alternatives;
- bubble confidence values.

Typical conflicts may include:

- multiple roll-number marks;
- missing digits;
- multiple set codes;
- low-confidence bubbles;
- duplicate candidate IDs;
- unknown candidate IDs;
- alignment failures;
- ambiguous question responses.

Manual corrections will preserve the machine-generated value in an audit trail.

---

## Candidate and Absentee Reconciliation

A candidate list may be imported from CSV or Excel.

OMRFlow will compare:

```text
Registered Candidates
        ↕
Recorded Attendance
        ↕
Detected OMR Scripts
```

It will identify cases such as:

- present candidate with script — normal;
- absent candidate without script — normal;
- absent candidate with script — conflict;
- present candidate without script — conflict;
- unknown roll number;
- duplicate script;
- invalid candidate ID.

---

## Answer-Key Processing

Answer keys may be entered manually or read from solution OMR sheets.

Different question-paper sets can have independent keys, for example:

```text
Set A
Set B
Set C
Set D
```

The recognized key will be displayed for verification before results are calculated.

The internal data model is intended to permit multiple correct answers in future versions even if the initial release assumes a single correct answer.

---

## Configurable Scoring

Users will be able to configure:

```text
Correct answer       +1.00
Incorrect answer     -0.25
Blank answer          0.00
```

Negative marking may be disabled.

Initial releases will use uniform marks across all questions.

More complex section-wise scoring may be introduced later.

---

## Result Processing

For each candidate, OMRFlow will determine:

- candidate ID;
- question-paper set;
- attendance state;
- number correct;
- number incorrect;
- number blank;
- positive marks;
- negative marks;
- final marks;
- rank;
- processing status;
- remarks.

Results can be reviewed before final export.

---

## Excel and PDF Reports

Planned Excel output includes at least:

### Roll Wise

Contains all registered candidates, including absent candidates.

### Merit Wise

Candidates ordered according to examination result.

Additional sheets may include:

- Summary;
- Answer Key;
- Processing Log.

Ranking can also be represented with Excel formulas such as:

```excel
=RANK.EQ(H2,$H$2:$H$2500,0)
```

A user-editable Excel template will allow institutions to customize:

- headers;
- logos;
- fonts;
- borders;
- column dimensions;
- page layout;
- institution details.

PDF reports will also be supported.

---

# Project-Based Processing

OMRFlow uses projects so that examination processing can be stopped and resumed safely.

A project may resemble:

```text
My_Examination/
│
├── project.json
├── database.sqlite
│
├── templates/
├── scans_original/
├── scans_aligned/
├── answer_keys/
├── candidate_lists/
├── exports/
└── logs/
```

SQLite is intended to be the authoritative working data store.

CSV and XLSX are primarily interchange and reporting formats.

---

# Technology Stack

OMRFlow is being developed primarily in Python.

| Component | Technology |
|---|---|
| Language | Python 3.12+ |
| Desktop GUI | PySide6 / Qt |
| Image Processing | OpenCV |
| Numerical Processing | NumPy |
| Database | SQLite |
| ORM | SQLAlchemy 2.x |
| Data Validation | Pydantic |
| Tabular Processing | pandas |
| Excel | openpyxl |
| Testing | pytest |
| GUI Testing | pytest-qt |
| Linting | Ruff |
| Type Checking | mypy |

The initial target platform is Windows.

The architecture is intended to remain sufficiently platform-independent to allow Linux and macOS support later.

---

# Architecture

OMRFlow deliberately separates the user interface from recognition algorithms.

```text
┌───────────────────────────────┐
│           PySide6 GUI         │
└───────────────┬───────────────┘
                │
                ▼
┌───────────────────────────────┐
│       Application Services    │
└───────────────┬───────────────┘
                │
        ┌───────┼─────────┐
        ▼       ▼         ▼
     Domain   Imaging   Recognition
        │       │         │
        └───────┼─────────┘
                ▼
        SQLite / Reporting
```

Major source areas are expected to include:

```text
src/omr_scanner/
│
├── domain/
├── database/
├── imaging/
├── recognition/
├── services/
├── gui/
├── reporting/
└── utils/
```

### Architectural rule

OMR algorithms must **not** be implemented directly inside GUI event handlers.

For example:

```text
GUI
 ↓
ScanService
 ↓
AlignmentEngine
 ↓
RecognitionEngine
 ↓
Database
```

rather than:

```text
GUI button
 ↓
OpenCV processing inside MainWindow
 ↓
Excel output
```

This separation improves testing, maintainability, and reproducibility.

---

# Development Roadmap

Development is divided into controlled phases.

## Phase 0 — Architecture & Repository Foundation

- repository structure;
- application architecture;
- project model;
- database foundation;
- minimal GUI;
- test infrastructure;
- developer documentation.

## Phase 1 — OMR Geometry & Alignment Engine

- registration-marker detection;
- orientation detection;
- rotation correction;
- perspective correction;
- scale normalization;
- geometric regression tests.

## Phase 2 — Template Data Model & Template Designer Core

- `.omrt` specification;
- zone models;
- visual template editor;
- drag-and-drop regions;
- template save/load.

## Phase 3 — Bubble Mapping & Recognition Engine

- bubble geometry;
- fill metrics;
- numeric fields;
- set codes;
- question recognition;
- confidence scoring.

## Phase 4 — Template Calibration & Validation

- test scans;
- diagnostic overlays;
- threshold adjustment;
- recognition quality summaries.

## Phase 5 — Batch Scan Processing Pipeline

- folder processing;
- worker execution;
- progress reporting;
- error isolation;
- batch database persistence.

## Phase 6 — Conflict Detection & Human Resolution

- conflict queue;
- image zoom;
- manual corrections;
- audit history.

## Phase 7 — Candidate & Attendance Reconciliation

- candidate import;
- absentee processing;
- script reconciliation;
- duplicate/missing candidate detection.

## Phase 8 — Answer-Key & Scoring Engine

- solution OMR;
- answer-key editor;
- negative marking;
- scoring engine.

## Phase 9 — Result Management & Reporting

- roll-wise reports;
- merit-wise reports;
- ranking;
- configurable Excel templates;
- PDF export.

## Phase 10 — Integration, Recovery & Production Hardening

- autosave;
- crash recovery;
- reprocessing;
- performance optimization;
- comprehensive validation.

## Phase 11 — Release, Documentation & Packaging

- user documentation;
- developer documentation;
- Windows packaging;
- release testing.

---

# Testing Philosophy

OMRFlow treats testing as a core requirement rather than a final development step.

The project will use:

```text
tests/
├── unit/
├── integration/
├── gui/
└── fixtures/
```

Image-processing tests will use both synthetic and anonymized real-world fixtures.

---

## Synthetic OMR Testing

A canonical synthetic OMR sheet can be transformed using known parameters:

```text
Canonical Sheet
      ↓
Rotation
Perspective
Scale
Translation
Brightness
Blur
Noise
      ↓
Synthetic Scan
      ↓
OMRFlow
      ↓
Recovered Sheet
```

Because the original geometric transformation is known, alignment accuracy can be quantitatively measured.

This makes regression testing possible without depending entirely on manually collected scans.

---

## Real-World Regression Tests

Anonymized real scans will eventually test conditions such as:

- light pencil marks;
- dark marks;
- partially filled bubbles;
- crossed-out bubbles;
- multiple marks;
- scanner shadows;
- skew;
- page translation;
- low contrast;
- blurred scans;
- imperfect corner markers.

Sensitive examination or candidate information should **never** be committed as public test data.

---

# Recognition Diagnostics

A future diagnostic mode is planned to expose intermediate processing stages:

```text
Original
   ↓
Grayscale
   ↓
Threshold
   ↓
Detected Markers
   ↓
Perspective Transform
   ↓
Normalized Image
   ↓
Bubble Locations
   ↓
Bubble Scores
   ↓
Recognition Result
```

The intention is to make recognition behaviour understandable rather than treating the OMR engine as a black box.

---

# Auditability

OMRFlow is intended for workflows where manual changes may affect examination results.

Important changes should therefore retain audit information such as:

```text
Machine value
Corrected value
Timestamp
Operation
Reason
```

Example:

```text
Machine interpretation: ?10018-10028
Manual correction:      10018
Reason:                  Multiple mark resolved
```

The original machine result should not be silently overwritten.

---

# Installation

OMRFlow is currently under active development.

Clone the repository:

```bash
git clone https://github.com/YOUR_USERNAME/omrflow.git
cd omrflow
```

Create a Python virtual environment:

```bash
python -m venv .venv
```

Activate it on Windows:

```powershell
.venv\Scripts\Activate.ps1
```

Install the project in development mode:

```bash
pip install -e .
```

Development dependencies may be installed using the mechanism defined in `pyproject.toml`.

---

# Running OMRFlow

During development:

```bash
python -m omr_scanner
```

or use the entry point defined by the current project configuration.

Consult:

```text
docs/DEVELOPMENT_GUIDE.md
```

for the current instructions.

---

# Running Tests

Run all tests:

```bash
pytest
```

Run lint checks:

```bash
ruff check .
```

Run static type checks:

```bash
mypy src/omr_scanner
```

All applicable tests should pass before a development phase is considered complete.

---

# Documentation

Planned documentation includes:

```text
docs/
├── ARCHITECTURE.md
├── DEVELOPMENT_GUIDE.md
├── DATA_MODEL.md
├── TEMPLATE_FORMAT.md
├── IMAGE_PROCESSING.md
├── TESTING.md
├── USER_GUIDE.md
└── decisions/
```

Development state and phase handoffs are maintained separately:

```text
development/
├── ROADMAP.md
├── CURRENT_STATE.md
├── PHASE_00_HANDOFF.md
├── PHASE_01_HANDOFF.md
└── ...
```

The purpose of the handoff documents is to allow both human developers and coding agents to continue development without reconstructing previous implementation decisions.

---

# Contributing

Contributions are welcome once the initial architecture has stabilized.

Useful future contribution areas include:

- computer vision;
- geometric alignment;
- OMR algorithms;
- Qt/PySide6 interface development;
- accessibility;
- automated testing;
- documentation;
- performance optimization;
- internationalization;
- examination reporting.

Before submitting a pull request:

```bash
pytest
ruff check .
mypy src/omr_scanner
```

Please avoid combining unrelated architectural changes and features in the same pull request.

---

# Design Principles

OMRFlow follows several core principles:

> **Readable over clever**

> **Explicit over implicit**

> **Configurable over hard-coded**

> **Modular over monolithic**

> **Testable over tightly coupled**

> **Human-verifiable over black-box automation**

The software should be understandable and modifiable by researchers, educators, institutional IT teams, students, and future software agents.

---

# Data Privacy

OMRFlow is intended to operate locally.

Candidate identities, answer sheets, attendance information, answer keys, and examination results should not require transmission to an external cloud service for normal operation.

Users remain responsible for protecting examination and candidate data according to their institution's policies and applicable law.

Public bug reports and test fixtures must not contain confidential candidate data.

---

# Security and Examination Integrity

OMRFlow should not be treated as authoritative merely because processing completed successfully.

For high-stakes examinations, institutions should establish independent procedures for:

- template validation;
- answer-key verification;
- unresolved-conflict review;
- attendance reconciliation;
- result verification;
- output approval;
- archival of original scans.

The software is intended to support such controls, not replace institutional responsibility.

---

# Project Status

OMRFlow is currently in **pre-release development**.

Features described in the roadmap may not yet be implemented.

Do not use unreleased versions for consequential examination processing without independent validation.

---

# License

OMRFlow is intended to be released as open-source software.

The final license should be selected before the first public release.

A permissive license such as **Apache License 2.0** or **MIT License** is recommended depending on the desired contribution and redistribution model.

---

# Acknowledgements

OMRFlow is being developed as an open and extensible platform for optical mark recognition, examination processing, and reproducible academic assessment workflows.

Contributions, testing data that can legally be shared, bug reports, documentation improvements, and algorithmic research are welcome.

---

## OMRFlow

**From scanned marks to verified results.**
