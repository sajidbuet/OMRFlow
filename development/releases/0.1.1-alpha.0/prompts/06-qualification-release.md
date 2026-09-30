# Prompt 06 — `0.1.1-F`: Operational qualification & `0.1.1-alpha.0` release

You are working in the OMRFlow repository. This is **phase 0.1.1-F**, the
last phase of `0.1.1-alpha.0`. Phases A–E must be merged (handoffs exist);
otherwise stop and say so. Branch: `feat/0.1.1-f-qualification`.

Plan: `development/releases/0.1.1-alpha.0/ACCEPTANCE_CRITERIA.md` §§1, 3–7.
The code is the fact; record discrepancies.

**This phase builds and runs qualification, and prepares a release. It does
not publish anything without the user's explicit instruction.**

---

## 1. Inspect first

- All `PHASE_*_HANDOFF.md` of this line; `docs/intake.md`, `docs/rescan.md`
- `docs/phase10_qualification.md`, `evaluation/qualification.py`,
  `evaluation/stress_runner.py`, `src/omr_scanner/tools/phase10_qualification.py`,
  `src/omr_scanner/tools/benchmark_stress.py` — the supervisor, kill-point, evidence-outside-
  the-killed-process and vacuous-pass techniques you must reuse
- `evaluation/synthetic_dataset.py`, `evaluation/fold_plans.py`,
  `evaluation/attendance_dataset.py`, `docs/testing/SYNTHETIC_DATA.md`
- `docs/release/RELEASE_CHECKLIST.md`, `CLEAN_MACHINE_TEST.md`,
  `docs/release/validation/`, `scripts/release/`, `packaging/`
- `docs/wiki/Release-Process.md`, `Development-Roadmap.md` (qualification
  matrix), `Known-Limitations.md`, `Upgrade-Compatibility.md`
- tests: `tests/unit/test_qualification.py`, `tests/integration/test_stress_*`,
  `tests/unit/test_release_automation.py`

Run the full suite first; record the baseline.

## 2. Preserve

- **The Phase 10 100,000-sheet campaign is not redesigned.** Its harness, its
  15 assertions and its report wording stay as they are and its self-test
  passes. The intake campaign is **additional**, in its own module and CLI.
- Everything in prompt 01 §2.
- The release process: nothing is tagged or published by automation; GitHub
  releases stay drafts until a person publishes; Alpha is a pre-release.

## 3. Scope

1. **Intake qualification harness** (`evaluation/intake_qualification.py` or
   similar, plus a headless CLI): simulated sources as separate writer
   processes; ≥ 3 sources, ≥ 10,000 images from the template-driven generator
   with exact ground truth; same filenames across sources; stepped/partial
   writes; random inter-arrival; byte duplicates; source loss/reconnection;
   forced kill and restart of OMRFlow's coordinator while writers continue; a
   scripted operator resolving conflicts as new ones arrive; fold-generated
   RESCAN_REQUIRED sheets and replacements (from a different source; chains).
2. **Assertions**: exactly the release-blocking list in ACCEPTANCE_CRITERIA.md
   §4.2, as a constant the evaluator is tested to emit in full (as Phase 10
   does). Writer logs and submission logs provide *measured* evidence for
   "no incomplete file processed" and "no completed scan re-recognised".
   Anti-vacuity: each kill must land with real committed and real in-flight
   work.
3. **Verdicts**: QUALIFIED / ALL RUNS PASSED — NOT THE RELEASE QUALIFICATION /
   FAILED, as Phase 10 reports.
4. **Run it at full scale** on this machine; commit the report (not the
   images) under `docs/release/validation/`.
5. **SMB qualification** (ACCEPTANCE_CRITERIA.md §5): write the procedure
   (`docs/release/SMB_QUALIFICATION.md`) and a helper script; **it requires
   two real machines and a person** — if you cannot perform it, prepare it
   completely, say so, and leave the gate open. Never simulate a share and
   call it SMB.
6. **Finite-mode regression**: golden finite project, Phase 10 self-test,
   whole suite.
7. **Packaged build**: existing packaging and installer checks, plus a scripted
   live-intake smoke run on the installed build (two local sources, a few
   hundred files, one restart); upgrade of a `0.1.0-alpha.2` project on the
   installed build.
8. **Release preparation** per `docs/release/RELEASE_CHECKLIST.md`: release
   notes stating that real scanning-room qualification is not performed and
   quality-decision defaults are unvalidated; CHANGELOG `[0.1.1-alpha.0]`
   section prepared; version already `0.1.1-alpha.0`. **Stop before tagging
   and ask the user.**

## 4. Out of scope

- Real scanning-room qualification (ACCEPTANCE_CRITERIA.md §6) — later Alpha.
- Beta criteria, code signing, the full 100,000-sheet Phase 10 run (optional
  at Alpha).
- New features. Defects found are fixed in small, separately committed
  changes with regression tests, and listed.

## 5. Persistence expectations

The campaign verifies, rather than changes, persistence: SQLite integrity
checks, the health check, and the snapshot partition recomputed from raw rows
at every checkpoint.

## 6. Tests required

- Harness self-tests at small scale (minutes), including one real kill,
  mirroring Phase 10's harness tests; evaluator emits every assertion.
- Tests for any defect fixed.
- Whole existing suite; GUI suite; release-automation tests.

## 7. Verification

```powershell
.venv\Scripts\python.exe -m ruff check src tests tools scripts
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest -q -m stress
# plus the intake campaign CLI at full scale, and the packaging scripts
```

## 8. Documentation updates

- New `docs/intake_qualification.md` (how to run, what it proves, what it
  does not — power loss, other hardware, recognition accuracy, real SMB unless
  §5 was performed).
- `docs/release/validation/` reports.
- `docs/wiki/Known-Limitations.md`, `Upgrade-Compatibility.md`,
  `Release-History.md` (only once actually released), `Development-Roadmap.md`
  (the `0.1.1` entry, the qualification-matrix row for this release, and
  Phase 11B's target version now that Beta follows the 0.1.1 line).
- `PHASE_F_HANDOFF.md`, ROADMAP.md §5, `CURRENT_STATE.md`, `CHANGELOG.md`.
- **README**: Current release status (only after release), Development status
  and Testing status — phases completed / under testing, automated, synthetic,
  network-share, real-data status and pending work, with the campaign result.

## 9. Status reporting

Report every track separately and exactly: implementation; automated tests;
**synthetic validation** (campaign verdict and scale); **network-share
validation** (performed on real SMB, or not — never inferred); real scanner
validation (not performed; not required for `alpha.0`); production
qualification (not performed); released (only if the user has published it).
If any gate item in ACCEPTANCE_CRITERIA.md §7 is unmet, say which and do not
describe the release as ready.

Commit in small commits, push the branch, do not merge or tag unless asked.
