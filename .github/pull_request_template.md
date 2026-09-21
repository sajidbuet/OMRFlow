<!--
Thank you for contributing. Please fill this in honestly - particularly
"Tests performed". "Ran the suite" when only part of it was run costs a
reviewer more time than saying so.

See CONTRIBUTING.md for the conventions this project holds to.
-->

## What changed

<!-- One or two sentences. What does this do that the code did not do before? -->

## Why

<!-- The problem being solved. Link the issue: "Fixes #123". -->

## Tests performed

<!--
Which of these did you actually run? Delete the ones you did not, rather
than leaving them unticked - an unticked box is ambiguous.
-->

- [ ] `pytest` (full suite)
- [ ] `pytest tests/unit`
- [ ] `pytest -m gui`
- [ ] `ruff check src tests`
- [ ] `mypy src/omr_scanner`
- [ ] Ran the application and exercised the change by hand

New or changed tests:

<!--
Name them. Every change needs a test; a bug fix needs one that fails
without the fix. If this genuinely needs no test, say why.
-->

## Documentation

- [ ] No documentation change needed
- [ ] User documentation updated (`docs/wiki/`)
- [ ] Developer documentation updated (`docs/`)
- [ ] `CHANGELOG.md` entry added under `## [Unreleased]`

## Compatibility

- [ ] No compatibility implications
- [ ] **Database schema changed** — a new migration is appended (never an
      edit to an existing one), and it is tested against a database created
      by the previous version
- [ ] **Project file format changed** — projects created by the current
      release still open
- [ ] **Configuration format changed** — an older configuration file still
      loads
- [ ] Public API or a module's responsibilities changed

If any of the above are ticked, describe the upgrade path:

<!-- What happens to an existing project when a user upgrades into this? -->

## Behaviour that decides marks

- [ ] This does not touch recognition, scoring, attendance or reporting
- [ ] It does — and the pull request explains the reasoning, with a test
      pinning the numerical outcome rather than merely that the code ran

## Screenshots

<!--
For a visible change, before and after. Synthetic data only - no real
candidate names, roll numbers or scans.
-->

- [ ] Not a visible change

## Release notes

- [ ] Not user-visible; no release note needed
- [ ] User-visible — suggested wording:

<!-- One sentence a non-developer would understand. -->

## Confirmations

- [ ] I have not committed real candidate data, examination content, answer
      keys, project folders, signing keys or credentials
- [ ] I ran `git status` and reviewed everything this pull request adds
- [ ] My contribution is offered under the project's MIT Licence
