# ADR-0001: Desktop application in Python with PySide6

- **Status:** Accepted
- **Date:** 2026-09-15
- **Phase:** 0

## Context

OMRFlow processes examination answer sheets: scanned images, candidate
identities, answer keys and results. It is used by examination offices and
academic departments, typically on an ordinary Windows workstation attached to a
document scanner.

The plausible architectures were:

1. a Python desktop application (PySide6/Qt);
2. a browser-first application (web front end plus a local or remote server);
3. an Electron desktop application with a Python or Node back end;
4. a command line tool plus spreadsheets.

Requirements that discriminate between them:

- **Local data.** Candidate identities and results must not require upload to an
  external service.
- **Large local images.** Batches of hundreds of 150-300 dpi scans must be read
  from a local folder without copying them into a server or a browser sandbox.
- **Image processing.** OpenCV and NumPy are the practical baseline for marker
  detection and bubble metrics.
- **Human-in-the-loop review.** Conflict resolution needs a fast, zoomable image
  view beside structured data - a desktop interaction pattern.
- **A small maintainer team**, including AI coding agents. One language and one
  toolchain is a real advantage.

## Decision

Build OMRFlow as a Python 3.12 desktop application using PySide6 (the official
Qt for Python binding), with OpenCV and NumPy for image processing, SQLite via
SQLAlchemy 2.x for project data, and pytest for tests.

## Rationale

- **One language end to end.** The recognition engine, the services and the GUI
  are all Python; no serialisation boundary, no second package ecosystem, no
  duplicated domain model between front and back end.
- **Direct file system access.** A desktop application can reference scans in
  place. A browser cannot read a folder without an upload step, which for a
  multi-gigabyte batch is both slow and a data-handling risk.
- **The ecosystem is already Python.** OpenCV, NumPy, pandas and openpyxl are
  where the mature implementations live. Choosing a JavaScript front end would
  mean either reimplementing recognition in JS or running a Python back end
  anyway - the complexity of both worlds for the benefit of neither.
- **Qt suits the review interface.** Fast image rendering with zoom and overlays,
  native file dialogs, keyboard-driven navigation and a mature widget set.
- **PySide6 specifically** over PyQt6: it is the official binding from the Qt
  Company and is LGPL, which leaves the project's own licensing options open.
- **Against Electron:** it adds a Chromium runtime and a second toolchain, and
  would still need Python for recognition. Installer size and memory use are
  materially worse for no functional gain.
- **Against a CLI-only tool:** conflict resolution and template design are
  inherently visual. A CLI cannot show an operator the bubble it is unsure about.

## Consequences

**Positive**

- Single toolchain; contributors and coding agents need one skill set.
- Recognition code is testable headlessly, independent of the GUI.
- Works offline by construction; no server to deploy or secure.

**Negative**

- Distribution is heavier than a web page: users need Python or a packaged
  build. Phase 11 addresses Windows packaging.
- Qt's signal/slot machinery is not fully expressible in Python's type system, so
  the GUI layer is type-checked slightly less strictly than the rest (documented
  in `docs/DEVELOPMENT_GUIDE.md`).
- No multi-user or remote access. Accepted: examination processing is a
  single-operator workflow, and centralising candidate data was never a goal.

**Mitigations**

- The architecture keeps every layer below `gui` free of Qt imports (enforced by
  `tests/unit/test_architecture.py`), so a future web or CLI front end could
  reuse the services unchanged.
- `opencv-python-headless` is used instead of `opencv-python` to avoid OpenCV's
  bundled Qt plugins shadowing PySide6's.
