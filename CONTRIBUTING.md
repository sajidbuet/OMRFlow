# Contributing to OMRFlow

Thank you for considering it. OMRFlow processes real examinations, so the
bar for a change is "would I trust this with a cohort's results?" — which
mostly means: tested, honest about what it does not cover, and no wider than
it needs to be.

This file is the practical summary. [`docs/DEVELOPMENT_GUIDE.md`](docs/DEVELOPMENT_GUIDE.md)
has the full detail, and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
explains why the code is laid out the way it is — read the second one before
adding a module.

## Before you start

- For anything beyond a small fix, **open an issue first**. It is quicker to
  agree an approach than to redo a pull request.
- Check the [Development Roadmap](docs/wiki/Development-Roadmap.md). OMRFlow
  is phased on purpose, and a change that belongs to a later phase is
  usually deferred rather than rejected.
- Read [Known Limitations](docs/wiki/Known-Limitations.md). Some behaviour is
  a documented gap, not a bug.

## Development environment

Requires **Python 3.12 or newer** and Windows for the GUI and packaging work
(the engine and its tests are platform-neutral; the packaging is not).

```powershell
git clone https://github.com/sajidbuet/OMRFlow.git
cd OMRflow
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Run the application:

```powershell
python -m omr_scanner.main
```

## Tests, lint and types

All three must pass before a pull request is ready. There is no "the linter
is wrong" exemption; if a rule is genuinely wrong, change the rule in
`pyproject.toml` in its own commit and say why.

```powershell
pytest                                  # everything (~30 min)
pytest tests/unit                       # fast, no Qt
pytest -m gui                           # GUI only
ruff check src tests tools
mypy src/omr_scanner
```

The suite is large and slow because it is mostly real: it processes images,
opens databases and drives Qt. Run the fast subset while working and the
whole thing before pushing.

### Writing tests

- **Every change needs a test.** A bug fix needs one that fails without it.
- Test behaviour, not implementation. `assert result is not None` is not a
  test of an alignment engine; compare a recovered position against a known
  one and assert the distance.
- Justify every tolerance where it is defined.
- GUI tests are a smoke layer, with one deliberate exception for geometry.
  Read the GUI testing policy in [`docs/TESTING.md`](docs/TESTING.md) before
  writing one — in particular the three ways an offscreen Qt test hangs or
  silently lies, each of which has cost a debugging session here.

## Coding conventions

- Ruff and mypy in strict mode are the formatting and typing authority.
- Module docstrings state *purpose*, *responsibilities* and *what does not
  belong here*. The last one is the most useful and the most often skipped.
- Comments explain **why**, never what. If removing a comment would not
  confuse a future reader, delete it.
- Respect the layering in `docs/ARCHITECTURE.md`. It is enforced by
  `tests/unit/test_architecture.py`, which parses every source file: the GUI
  imports no OpenCV or SQLAlchemy, `imaging` imports no Qt, `domain` imports
  nothing above it.
- Keep `omr_scanner/__init__.py` cheap to import — no Qt, no database.

## Changes that need extra care

### Database migrations

Never edit an existing migration; append a new one. `SCHEMA_VERSION` is
derived from the migration list and is never edited by hand. A migration
must be tested against a database created by the *previous* version, and
must fail with a clear message rather than half-apply. See "Adding a
database migration" in the development guide.

### Anything a project file's format depends on

Projects created by a released build must keep opening. If a change cannot
preserve that, it needs a migration and a note in the changelog's
compatibility section.

### Recognition, scoring and reporting

These decide people's marks. Changes here need a test that pins the
*numerical* outcome, not merely that a function ran, and the reasoning
belongs in the pull request.

### The version

`src/omr_scanner/_version.py` is the only place the version is written down —
`pyproject.toml` reads it from there. Do not add a second copy;
`tests/unit/test_version.py` fails if one appears. Releases are made by the
maintainer following `docs/release/RELEASE_CHECKLIST.md`.

## Branches and pull requests

- Branch from `main`. Name it for what it does: `fix/absentee-rank-gap`,
  `docs/quick-start`.
- Keep a pull request to one subject. Two unrelated fixes are two pull
  requests.
- Fill in the pull request template honestly — particularly *tests
  performed*. "Ran the suite" when you ran part of it wastes a review.
- Update the documentation in the same pull request as the behaviour.
  Documentation that lags is documentation that misleads.
- Add a `CHANGELOG.md` entry under `## [Unreleased]` for anything a user
  would notice.

## Documentation

- User-facing documentation lives in [`docs/wiki/`](docs/wiki/), which is the
  canonical source; the GitHub Wiki is published from it.
- Developer documentation lives in `docs/`.
- Use the interface's actual current wording. Screenshots must come from the
  current build and use synthetic data.
- **Do not document behaviour you have not verified.** If something cannot be
  checked, mark it clearly as unverified rather than writing a plausible
  guess. Accuracy about what is *not* known is part of the project's
  documentation standard — see the status legend in the README.

## Never commit

- Real candidate names, roll numbers, rosters or attendance workbooks.
- Real answer keys or examination content.
- Scans of real answer sheets.
- A real project folder or its `database.sqlite`.
- Signing certificates, private keys, tokens or passwords.
- Virtual environments, build output, caches or IDE settings.

`.gitignore` covers the usual cases, including `local_test_data/`, which
exists precisely so real material can sit in the working tree without ever
being committed. It is not a substitute for looking at `git status` before
you commit.

If you believe confidential data has been committed — even in a branch, even
if deleted in a later commit — say so immediately via
[SECURITY.md](SECURITY.md) rather than opening a public issue. Git history
keeps it.

## Reporting bugs and proposing features

See [SUPPORT.md](SUPPORT.md) for what makes a report actionable and how to
sanitise a reproduction before uploading it. Security and privacy problems
go through [SECURITY.md](SECURITY.md), not the issue tracker.

## Licence

By contributing you agree that your contribution is licensed under the
project's [MIT Licence](LICENSE).
