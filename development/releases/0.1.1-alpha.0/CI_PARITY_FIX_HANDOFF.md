# CI portability correction — handoff

Branch `fix/ci-platform-parity`, from `main` `9f5d7d4`, 2026-10-08. Merged into
`main` as `eb6d6dd` (2026-10-08, on the owner's instruction). Not
tagged, not released. The brief named `main` `acad683`; the local and
remote `main` were both `9f5d7d4` (docs-only commits on top of `acad683`), and
the branch starts there.

**Status: CI PARITY FIX COMPLETE** at `8821093`. Every GitHub job is green and
the native Windows canonical gate passes (§7, §8). Real SMB is **not
performed**.

## 1. Root causes

The brief listed 4 Linux mypy errors and 6 GUI failures. Reproducing the
GitHub jobs found more: the Ubuntu test job had never reached those six. On
every `main` run inspected from 2026-10-03 on, it stopped at about 7 % with a
segmentation fault. The six failures were the **Windows** job's.

| # | Where | Root cause | Kind |
|---|---|---|---|
| 1 | Ubuntu mypy | mypy constant-folds `sys.platform` for the platform it analyses; the Windows-only branches of `smb_qualification/topology.py` and `services/intake_fs.py` are unreachable under Linux | Configuration |
| 2 | Windows offscreen, `test_ui_zoom` ribbon | **Real state bug.** `WorkflowRibbon._apply` gave the new geometry only to the steps a plan places; in the narrow (current-only) layout the 8 hidden steps kept the previous zoom/density. Reproduced **natively**: 200 % → 190 % at 1366×768. Nothing hidden is painted and a step is re-measured when shown, so nothing visible was wrong | Product (A) |
| 3 | Windows offscreen, 5 geometry tests | Offscreen Qt on Windows finds no fonts ("Qt no longer ships fonts") and lays out with a fallback about twice as wide as Segoe UI. Measured: one provenance line 895 → 1,968 px; Resolve reason combo minimum 278 → 534 px; the getting-started card's minimum width 264 → 418 px against its 400 px column, so Qt computes the card's height-for-width at 418 px and its rows come out short. Not stale layout: re-running layouts changes nothing. Natively, 0 of 30 width×zoom cases are short | Font metrics (C/D) |
| 4 | Ubuntu tests, first 8 F's | `tests/crash` asserted that workers die with an abruptly killed coordinator. That is a Windows Job Object guarantee; `process_containment` documents POSIX as not implemented, and `test_stress_kill_resume` already asserted it on Windows only | Test assumption |
| 5 | Ubuntu tests, segfault at ~7 % | `tests/gui/conftest.py::_discard_windows_since` deleted every new top-level widget, including **parented popups** (combo lists, menus, toolbar overflow menus) whose owners keep plain pointers. Offscreen's wide font makes toolbars overflow; destroying a page's actions then reached a deleted overflow menu. gdb: `~QAction → QWidget::removeAction → QToolBar::actionEvent → QWidget::removeAction → SIGSEGV` | Test infrastructure |
| 6 | Ubuntu tests, 3 failures CI never reached | (a) on POSIX, `spawn` also starts multiprocessing's `resource_tracker`, which `test_engine_process_pool` counted as a worker (and its kill test killed it instead of a worker); (b) the intake campaign test's orphan check (as #4); (c) `drive_type` of a UNC path is Windows-only — on POSIX `Path(r"\\host\share")` is a relative file name | Test assumptions |
| 7 | Ubuntu tests, second segfault at ~14 % (CI run 37771727673) | **Real defect in `gui/ui_scale.py`, latent on Windows.** The zoom pass reached nested layouts as `itemAt(i).layout()`. A layout returned as a `QLayoutItem*` gets a PySide wrapper that is never invalidated when Qt deletes it; `QStatusBar` deletes and rebuilds its nested layouts when it reformats, so a later pass was handed a stale `QVBoxLayout` wrapper for a reused address (glibc reuses freed blocks promptly), and the `QLayout`/`QLayoutItem` pointer adjustment produced garbage | Product (A) |

A WSL failure that is **not** a CI failure: under Ubuntu's system Python
3.12.3, `test_the_generator_shuts_its_pool_down_even_if_abandoned` fails
because `close()` leaves the outer generator holding the inner one. It passes
on 3.12.15, the version CI uses, and on the Windows venv's 3.12.7. No change
was made for it.

## 2. Files changed

```
pyproject.toml                                   mypy override; native_qt_layout marker
src/omr_scanner/gui/widgets/workflow_ribbon.py   #2
src/omr_scanner/gui/ui_scale.py                  #7
tests/gui/conftest.py                            native_qt_layout skip; #5
tests/gui/test_workflow_ribbon.py                #2 regression test
tests/gui/test_ui_zoom.py                        #3 ActionRow; #7 regression tests
tests/gui/test_resolve_page.py                   #3 Resolve
tests/gui/test_resolve_provenance_gui.py         #3 provenance
tests/crash/harness.py                           #4 Killed.uncontained
tests/crash/test_crash_matrix.py                 #4
tests/crash/test_engine_kills.py                 #4
tests/crash/test_phase7_kills.py                 #4
tests/gui/test_session_recovery_gui.py           #4
tests/integration/test_engine_process_pool.py    #6a
tests/integration/test_intake_qualification_campaign.py  #6b
tests/unit/test_smb_qualification.py             #6c
.github/workflows/ci.yml                         comment only
scripts/test-ci-parity.ps1                       new helper
pytest-ruff-mypy.ps1                             header comment only
docs/TESTING.md, CONTRIBUTING.md, README.md      documentation
development/releases/0.1.1-alpha.0/CI_PARITY_FIX_HANDOFF.md   this file
```

## 3. Mypy fix

The existing `[[tool.mypy.overrides]]` that set `warn_unreachable = false` for
`omr_scanner.config.paths` now also lists
`omr_scanner.evaluation.smb_qualification.topology` and
`omr_scanner.services.intake_fs`. It is per module and covers that one
diagnostic only. `strict = true` and every other check still apply to all
three modules, and `warn_unreachable` stays on everywhere else. Results:
`mypy --platform linux`, `--platform win32`, `--platform darwin` and a default
run are all clean (269 files). Before the fix, `--platform linux` reproduced
exactly the 4 CI errors.

The README records an earlier precedent (`process_containment.py`) where the
code was restructured into `if/else` instead. The brief asked for the
override, and the Windows code is correct as written, so it was not
restructured.

## 4. GUI test classifications (the original six)

| Test | Class | Fix | Native coverage | Offscreen coverage |
|---|---|---|---|---|
| `test_ui_zoom::TestBWhatGrows::test_the_ribbon_measures_steps_at_the_new_zoom` | **A** product state | `_apply` re-measures hidden steps as the chevron they become; `test_workflow_ribbon::TestDNarrowLayout::test_the_hidden_stages_follow_a_zoom_and_density_change` (fails without the fix) | unchanged test runs | unchanged test runs, passes |
| `test_resolve_page::TestSpaceDistribution::test_nothing_is_clipped_at_a_supported_size[1366-768]` | C (font metrics) | that parameter is `native_qt_layout`; choice-button / queue checks moved to a portable test at both sizes | runs | `[1920-1080]` + `test_every_choice_is_reachable_and_the_queue_never_scrolls_sideways` |
| `test_resolve_provenance_gui::…::test_at_1366_at_100_percent_the_file_name_stays_recognisable` | C | `native_qt_layout` | runs | `test_in_the_window_it_shows_what_its_room_allows[long,short]` (in-window text == `fitted_text(contents width)`), `TestShorteningOrder` |
| `…::test_a_short_line_is_shown_whole` | C | `native_qt_layout` | runs | same, plus `TestShorteningOrder::test_a_short_line_with_room_is_shown_whole` |
| `test_ui_zoom::TestEWrappedRows::…[100]`, `[150]` | C (whole test, `[200]` included: its offscreen pass was incidental) | `native_qt_layout` | runs | `test_a_row_given_its_height_for_width_shows_every_line[100,150,200]` (ActionRow's hfw contract at explicit widths) |

Mutation check (offscreen): making `ActionRow.heightForWidth` return the
fixed row height, or making `ProvenanceLabel` refit to 10,000 px, fails the
new portable tests.

`native_qt_layout(reason=…)` is a registered marker. The autouse fixture in
`tests/gui/conftest.py` skips it at run time when
`QGuiApplication.platformName() == "offscreen"`, and the skip message names
the portable test that covers the contract. 6 test items carry it.

## 5. Production changes

1. `gui/widgets/workflow_ribbon.py` `_apply`: steps hidden by the layout get
   `self._chevron(first_in_row=index == 0)`. Presentation state only.
2. `gui/ui_scale.py` `_rescale_layout_tree`: nested layouts are walked through
   `layout.children()` instead of `itemAt(i).layout()`. The same layouts are
   reached, now with tracked wrappers. Presentation only: it changes how the
   zoom pass finds layouts, not what it applies to them.

Nothing else in `src/` changed apart from the mypy configuration, which is
not code. There were no changes to intake, registration, deduplication,
sessions, recognition, persistence, scoring, reports or SMB.

## 6. Qualification impact

* **Phase 9** (automated intake qualification, QUALIFIED at `542b2af`): it
  exercises the headless intake/engine and none of the changed code; the two
  production changes are GUI presentation. **Remains valid.** No rerun.
* **Phase 10** (installed-build evidence on candidate `73e364b`): the evidence
  is about intake, packaging, installation and upgrade; it measured neither
  ribbon layout nor zoom. **Remains valid for what it established.** The
  release bundle will be rebuilt from whatever commit is eventually tagged,
  so these fixes will be in it. CI's packaging job (below) builds and checks
  a bundle from this branch. No targeted installed-build recheck was done.
  None is needed for the SMB run, which exercises intake.
* The test-only changes (#3–#6) alter what the suite asserts **on Linux or
  offscreen only**. Every Windows-native assertion is unchanged, except that
  window cleanup now leaves popups to their owners.

## 7. Local validation

| Check | Where | Result |
|---|---|---|
| mypy (default, win32) | Windows, `8821093` | clean, 269 files |
| mypy `--platform linux` | Windows, `8821093` | clean, 269 files (before the fix: the 4 CI errors) |
| mypy `--platform win32` / `darwin` | Windows, `a81523a` | clean |
| ruff `src tests tools scripts` | Windows | clean |
| Targeted GUI, native (`test_resolve_page`, `test_resolve_provenance_gui`, `test_ui_zoom`, `test_workflow_ribbon`) | `287a6a5` | 378 passed, 1 skipped (pre-existing skip) |
| Same, offscreen Windows | `287a6a5` | 372 passed, 7 skipped (6 `native_qt_layout` + the pre-existing one) |
| Before any fix, the 3 brief modules offscreen Windows | `9f5d7d4` | 6 failed, 276 passed, 1 skipped — the six CI failures reproduced |
| Ribbon regression test without / with the fix | native and offscreen | fails / passes |
| `TestFNestedLayouts` (zoom-pass regression) without / with the fix | WSL Linux 3.12.15 | segfault / 17 passed, 2 skipped (with the provenance module), twice |
| Previously failing Linux tests + `tests/crash` | WSL Linux 3.12.15, `d62e0e4` | 113 passed, 6 skipped (Windows-only) |
| **Canonical gate** `pytest-ruff-mypy.ps1` | native Windows, clean worktree `C:\Research\OMRflow-ci`, branch `gate/ci-parity-8821093` at `8821093`, log `Scratch\Log\2026-10-08_184926` | **PASS**: pytest **7,274 passed, 29 skipped, 0 failed**, 10 `stress` deselected (2 h 30 min); ruff clean; mypy clean (269 files); git state unchanged |

The 10 `stress` tests were not rerun. Neither they nor the code they exercise
changed, except that `test_intake_qualification_campaign.py`'s non-stress
test now checks orphans on Windows only.

A full offscreen run on Windows was not done locally. The GitHub Windows job
below is that run.

## 8. GitHub Actions

[Run 37779681011](https://github.com/sajidbuet/OMRFlow/actions/runs/37779681011),
`workflow_dispatch` on `fix/ci-platform-parity` at `8821093` (the workflow
does not trigger on branch pushes; no pull request was opened):

| Job | Result |
|---|---|
| Lint and type check (Ubuntu) | **success** — ruff clean; mypy clean, 269 files |
| Tests (windows-latest, offscreen) | **success** — 7,253 passed, 50 skipped, 10 deselected (2 h 21 min) |
| Tests (ubuntu-latest, offscreen) | **success** — 7,238 passed, 65 skipped, 10 deselected (49 min) |
| Packaging smoke test | **success** — "Bundle verified: OMRFlow 0.1.1-alpha.0" |

Both test jobs skip the 6 `native_qt_layout` items with the explicit reason.

Earlier runs on this branch, for the record: 37743070378 at `7149a31`
(lint ✅, Windows ✅, Ubuntu ❌ — the first segfault, not yet fixed) and
37771727673 at `3addff7` (lint ✅, Ubuntu ❌ — the second segfault, found
there; Windows was cancelled by the next dispatch). The last failing `main`
run before the branch was 37656566479 (`9f5d7d4`).

## 9. SMB readiness

**Real SMB qualification remains NOT PERFORMED.** None of this work touches
SMB tooling, the workload, network policy, the 15-second quiet period or the
30-second poll interval. CI is green on `fix/ci-platform-parity`, which was
merged into `main` as `eb6d6dd`. That merge commit's tree is the branch tip's
(`main` had not moved). Once the push-triggered run on `main` is green, run
Saturday's two-machine SMB qualification from that commit.

## How CI was mirrored locally (for next time)

* Linux mypy: `mypy --platform linux src/omr_scanner`.
* Offscreen GUI on Windows: `scripts\test-ci-parity.ps1` (`-AllGui` for all
  of `tests/gui`).
* The Ubuntu test job: WSL Ubuntu 24.04, `uv` in `~/.local/bin`, a clone
  (`~/omrflow-ci`) with a venv on `uv`-managed CPython 3.12.15 (CI's
  version), `QT_QPA_PLATFORM=offscreen pytest -q`. Native backtraces came
  from `gdb` extracted from `apt-get download` debs into `~/gdbroot` (no
  root).
