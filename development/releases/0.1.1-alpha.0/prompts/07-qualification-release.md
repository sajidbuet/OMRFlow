# Prompt 07 — `0.1.1-G`: Qualification and `0.1.1-alpha.0` release

You are working in the OMRFlow repository. This is **phase 0.1.1-G**, the last
phase of `0.1.1-alpha.0`. Phases A–F must be merged (handoffs exist);
otherwise stop and say so. Branch: `feat/0.1.1-g-qualification`.

Plan: `ROADMAP.md` §§4, 7, 9; `ACCEPTANCE_CRITERIA.md` §§1, 3–8;
`ARCHITECTURE_NOTES.md` §16 Q9, Q10 and finding F8. The code is the fact;
record discrepancies.

**This phase builds and runs qualification and prepares a release. It publishes
nothing without the user's explicit instruction.**

---

## 1. Inspect first

- All `PHASE_*_HANDOFF.md` of this line; `docs/intake.md`, the rescan docs
- `docs/phase10_qualification.md`, `evaluation/qualification.py`,
  `evaluation/stress_runner.py`, `src/omr_scanner/tools/phase10_qualification.py`,
  `src/omr_scanner/tools/benchmark_stress.py` — the supervisor, kill-point,
  evidence-outside-the-killed-process and anti-vacuity techniques to reuse
- `evaluation/synthetic_dataset.py`, `evaluation/fold_plans.py`,
  `evaluation/attendance_dataset.py`, `evaluation/answer_keys.py`,
  `docs/testing/SYNTHETIC_DATA.md`
- `docs/release/RELEASE_CHECKLIST.md`, `CLEAN_MACHINE_TEST.md`,
  `docs/release/validation/`, `scripts/release/`, `packaging/`
- `docs/wiki/Release-Process.md`, `Development-Roadmap.md` (qualification
  matrix), `Known-Limitations.md`, `Upgrade-Compatibility.md`
- `src/omr_scanner/_version.py` `numeric_version` (finding F8)
- tests: `tests/unit/test_qualification.py`, `tests/integration/test_stress_*`,
  `tests/unit/test_release_automation.py`

Run the full suite first; record the baseline.

## 2. Preserve

- **The Phase 10 100,000-sheet campaign is not redesigned.** Its harness,
  assertions and report wording stay; its self-test passes. The intake campaign
  is **additional**, in its own module and CLI.
- The release process: nothing tagged or published by automation; GitHub
  releases stay drafts until a person publishes; Alpha is a pre-release.
- Everything in `prompts/README.md` "Rules every prompt repeats".

## 3. Scope — exactly this

1. **Intake qualification harness** (e.g. `evaluation/intake_qualification.py`
   plus a headless CLI) implementing the workload of ACCEPTANCE_CRITERIA.md §5.1:
   simulated sources as separate writer processes; ≥ 3 sources, ≥ 10,000
   multi-set images with exact ground truth; same filenames across sources;
   stepped/partial writes; random inter-arrival; byte duplicates; source
   loss/reconnection; forced kill and restart of the coordinator while writers
   continue; a scripted operator resolving conflicts, confirming suggested
   rejections and replacements (different source; chains); session closure;
   session-level Results and Reports compared with ground truth.
2. **Assertions**: exactly the list in ACCEPTANCE_CRITERIA.md §5.2, as a
   constant the evaluator is tested to emit in full. Writer and submission logs
   give *measured* evidence; each kill lands with real committed and real
   in-flight work.
3. **Verdicts**: QUALIFIED / ALL RUNS PASSED — NOT THE RELEASE QUALIFICATION /
   FAILED, as Phase 10 reports.
4. **Run it at full scale**; commit the report (not the images) under
   `docs/release/validation/`.
5. **SMB qualification** (ACCEPTANCE_CRITERIA.md §6): write
   `docs/release/SMB_QUALIFICATION.md` and a helper script. It needs two real
   machines and a person — if you cannot perform it, prepare it completely, say
   so, and leave the gate open. Never simulate a share and call it SMB.
6. **100,000-sheet run** (decided, Q9): a fresh 100k run is **optional** for
   `0.1.1-alpha.0`; if not run, the release notes say so. Required instead:
   the **targeted endurance tests** of ACCEPTANCE_CRITERIA.md §5.3 and **every
   crash-safety test** of §5.4, including interruption and resume at ≈ 1, 25,
   50, 75 and 99 % of a large run, with real process termination. Prepare the
   harness so the **mandatory pre-Beta** fresh 100k qualification on the new
   architecture (§9) can be run without redesign.
7. **Finite-mode regression**: golden single-batch project; Phase 10 self-test;
   whole suite.
8. **Packaged build**: existing packaging and installer checks; a scripted
   live-intake smoke run on the installed build (two local sources, a few
   hundred files, one restart); upgrade of a schema-12 project and of a
   `0.1.0-alpha.2` project on the installed build.
9. **Version ordering** (F8): report whether `numeric_version` still sorts a
   Beta below the Alphas it follows. It is a **release gate before any Beta
   tag** (ACCEPTANCE_CRITERIA.md §9): fix and test it in this phase if the user
   agrees, otherwise record it as an open gate. The Beta that follows this line
   is `v0.1.1-beta.x`; never `v0.1.0-beta.x`.
9a. **Packaged SQLite durability**: record the packaged build's SQLite version
   and its default `synchronous` / `journal_mode`, since crash safety relies on
   them (ARCHITECTURE_NOTES.md §13.3).
10. **Release preparation** per `docs/release/RELEASE_CHECKLIST.md`: release
    notes stating that real scanning-room qualification is **not performed** and
    quality-decision defaults are **unvalidated**; `CHANGELOG.md`
    `[0.1.1-alpha.0]` section prepared. **Stop before tagging and ask the
    user.**

## 4. Out of scope

- Real scanning-room qualification (ACCEPTANCE_CRITERIA.md §7) — a later
  `0.1.1` Alpha, and part of Phase 11B's evidence.
- Phase 11B's Beta criteria; code signing; new features. Defects found are
  fixed in small, separately committed changes with regression tests, and
  listed.

## 5. Persistence expectations

The campaign verifies rather than changes persistence: SQLite integrity checks,
the health check, and the snapshot partition recomputed from raw rows at every
checkpoint.

## 6. Tests required

- Harness self-tests at small scale (minutes), including one real kill;
  evaluator emits every assertion.
- Tests for any defect fixed.
- Whole suite; GUI suite; release-automation tests.

## 7. Verification

```powershell
.venv\Scripts\python.exe -m ruff check src tests tools scripts
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest -q -m stress
# plus the intake campaign CLI at full scale, and the packaging scripts
```

## 8. Documentation updates

New `docs/intake_qualification.md` (how to run, what it proves, what it does not
— power loss, other hardware, recognition accuracy, real SMB unless §3.5 was
performed); `docs/release/validation/` reports; `docs/wiki/Known-Limitations.md`,
`Upgrade-Compatibility.md`, `Release-History.md` (only once released),
`Development-Roadmap.md` (the `0.1.1` entry and the qualification-matrix status
for this release; Phase 11B's target is **not** changed by this phase);
`ROADMAP.md` §7; new `PHASE_G_HANDOFF.md`; `CURRENT_STATE.md`; `CHANGELOG.md`;
README Current release status (only after release), Development status and
Testing status with the campaign result.

## 9. Status reporting

Every track separately and exactly: implemented; tested; **synthetic
validation** (campaign verdict and scale); **network-share validation**
(performed on real SMB, or not — never inferred); real-scanner validation (not
performed; not required for `alpha.0`); production qualification (not
performed); released (only if the user has published it). If any gate item in
ACCEPTANCE_CRITERIA.md §8 is unmet, say which and do not describe the release as
ready.

Commit in small commits, push the branch, do not merge or tag unless asked.
