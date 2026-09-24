# Testing

> The authoritative document is
> **[`docs/TESTING.md`](https://github.com/sajidbuet/OMRFlow/blob/main/docs/TESTING.md)**.

## Running

```powershell
pytest                 # everything, ~30 min on an idle machine
pytest tests/unit      # fast, no Qt
pytest -m gui          # GUI only
pytest --cov=omr_scanner
```

GUI tests need a Qt platform plugin; the offscreen one is selected
automatically on a headless runner, or force it with
`QT_QPA_PLATFORM=offscreen`.

## Layout

```text
tests/
├── unit/          pure logic, imaging geometry, the synthetic generator, layering
├── integration/   services + database + filesystem together
├── gui/           pytest-qt tests of the PySide6 shell
└── fixtures/      test data
```

## The principles that matter

- **A test asserts behaviour, not implementation.** A test that restates the
  code is worse than no test.
- **A measurement, not "something happened".** `assert result is not None` is
  not a test of an alignment engine. Compare a recovered position against a
  known one and report the distance; justify every tolerance where it is
  defined.
- **Failures are tested as carefully as successes.** For examination
  software, "refuses a damaged project cleanly" matters as much as "opens a
  good one".
- **Architecture is tested too.** `tests/unit/test_architecture.py` parses
  every source file and enforces the layering.
- **Tests never touch real user data.** An autouse fixture redirects the
  per-user configuration and log directories into the test's temporary
  folder.

## Before writing a GUI test

Read the GUI testing policy in `docs/TESTING.md`, and in particular **the
three ways an offscreen Qt test hangs or lies**: a nested modal loop that
never returns, a signal wired to a modal prompt, and `resize()` on a widget
that was never shown. Each has cost a real debugging session here.

## Release testing

Packaging and installer verification are separate and live in
`scripts/release/` — see [Release Process](Release-Process).
