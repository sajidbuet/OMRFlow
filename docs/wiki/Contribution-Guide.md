# Contribution Guide

The full guide is
**[CONTRIBUTING.md](https://github.com/sajidbuet/OMRFlow/blob/main/CONTRIBUTING.md)**
in the repository root, which is where GitHub looks for it.

In summary:

- **Open an issue first** for anything beyond a small fix.
- Set up with [Development Setup](Development-Setup).
- `ruff`, `mypy` and `pytest` must all pass.
- **Every change needs a test**; a bug fix needs one that fails without it.
- Respect the layering in [Architecture](Developer-Architecture) — it is
  enforced by a test.
- Update the documentation in the **same** pull request as the behaviour.
- Add a `CHANGELOG.md` entry under `## [Unreleased]` for anything a user
  would notice.
- **Never commit** real candidate data, examination content, answer keys,
  project folders, signing keys or credentials.

Areas needing extra care, each explained in CONTRIBUTING.md: database
migrations, anything a project file's format depends on, and recognition,
scoring or reporting — which decide people's marks.

## Related

- [Development Setup](Development-Setup)
- [Testing](Testing)
- [Architecture](Developer-Architecture)
- [Release Process](Release-Process)
