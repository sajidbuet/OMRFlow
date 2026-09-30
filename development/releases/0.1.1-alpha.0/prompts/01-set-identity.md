# Prompt 01 — `0.1.1-A`: Set identity (and the version bump)

You are working in the OMRFlow repository. This is **phase 0.1.1-A**, the
first phase of the `0.1.1-alpha.0` line. Work on a new branch
`feat/0.1.1-a-set-identity` from the current `main`.

The plan is in `development/releases/0.1.1-alpha.0/`: `ROADMAP.md` (§5 A, §9),
`ARCHITECTURE_NOTES.md` (§2.5, §3 defect 5, §7, §15),
`ACCEPTANCE_CRITERIA.md` (§1, §2 A). They were reconciled against `main` at
`128512d` (schema 12). **The code may have moved since**: the code is the
fact, the plan is the intent — record every discrepancy in your handoff.

---

## 1. Inspect first — before writing any code

- `README.md` (Development status, Testing status),
  `docs/wiki/Development-Roadmap.md`, `docs/wiki/Examination-Sets.md`,
  `docs/DATA_MODEL.md`, `docs/decisions/ADR-0003-schema-migrations.md`
- `src/omr_scanner/_version.py`, `tests/unit/test_version.py`,
  `tests/unit/test_release_automation.py`
- `database/models.py` (`ProjectSet`, every `set_code` column),
  `database/migrations.py` (confirm `SCHEMA_VERSION` and the last migration)
- `domain/exam_sets.py` (`validate_set_code`, `find_conflicting_set` and its
  docstring explaining today's exact-match decision), `services/project_sets.py`
- every place that compares or looks up a set code: search `src/` for
  `set_code`, `.upper()`, `.casefold()`; at minimum `services/review_store.py`
  (`effective_set_codes`, `sync_undefined_set_codes`),
  `services/reconciliation_store.py`, `services/scoring.py`,
  `services/scoring_store.py`, `services/report_store.py`,
  `services/report_readiness.py`, `services/scan_lifecycle.py`,
  `services/answer_key.py` (`can_print_set_code`, `check_sheet_set`)
- `gui/project_config_dialog.py`, the Resolve set-code editor under `gui/review/`
- `evaluation/answer_keys.py`, `evaluation/attendance_dataset.py`
- `tests/unit/test_architecture.py` (how architecture rules are enforced)

Run `git status`, `git log --oneline -10` and the full suite; record the
baseline.

## 2. Preserve

- Raw readings: `batch_scan.set_code_value` and the review ledger keep what was
  read or corrected **on the paper**. Stored `answer_key_revision.set_code`,
  `candidate_result.set_code` and `generated_report.set_code` are **not
  rewritten**; they are compared through the canonical function.
- Reject & Rescan's `declared_set_code` stays logical.
- The Attendance workstation's per-set rosters, Answer Key provenance
  (migration 12), scoring and report behaviour — identical for projects whose
  set codes are already upper-case and unmapped.
- Everything in `prompts/README.md` "Rules every prompt repeats".

## 3. Scope — exactly this

1. **Version bump, first commit, alone.** `__version__` in
   `src/omr_scanner/_version.py` from `0.1.0-alpha.2` to `0.1.1-alpha.0`.
   Nowhere else defines the version. Update version tests only if they pin the
   literal; add a `CHANGELOG.md` `[Unreleased]` note that the 0.1.1 line has
   begun. Do not tag, build or release. Commit:
   `chore: begin the 0.1.1-alpha.0 development line`.
2. **`services/set_identity.py`** (Qt-free): `canonical_code()` =
   `unicodedata.normalize("NFKC", code).strip().upper()`,
   `logical_for_physical()`, `physical_for_logical()`, and the helpers the
   callers need.
3. **Migration** (next free number — 13 unless something else merged first):
   `project_set.canonical_code` (unique where not NULL),
   `project_set.physical_mark` (nullable). Collision handling per
   ARCHITECTURE_NOTES.md §7.3: the first set by `display_order` gets the
   canonical code, the other stays NULL; nothing is merged. Migrations change
   structure; if filling `canonical_code` is data, justify doing it in the
   migration (it is derived, deterministic structure metadata) or do it in the
   service on open — decide and record.
4. **Route every set-code comparison through `set_identity`**:
   `find_conflicting_set` (defining `a` when `A` exists is refused — update its
   docstring, which documents the opposite decision), undefined-set conflicts,
   reconciliation placement, declared-set validation, scoring's usable set and
   key lookup, verified keys, report associations, readiness, the Answer Key
   stage. One translation point for physical → logical: the effective-set
   derivation in `review_store`.
5. **Collision reporting**: Project Health and Project Configuration name
   colliding sets; set-dependent stages refuse to score until renamed or merged
   by the operator.
6. **Physical mark UI**: Project Configuration → Sets *Printed on sheet as*
   (validated with `can_print_set_code`, unique); Resolve shows
   *Set 10 (A on sheet)* and offers physical marks; `check_sheet_set` compares
   the raw read with the chosen set's mark; exports show *Set (as read)* and
   *Set*; the synthetic generator marks the physical symbol.
7. **Architecture test** forbidding direct comparisons of `set_code` values
   outside `set_identity`.

## 4. Out of scope

- Scan sessions, batch sealing, downstream aggregation (phases B, C).
- Automatic merging of colliding sets.
- Any change to recognition, conflict policy meaning, scoring rules or report
  layout.

## 5. Persistence expectations

- Additive migration guarded by `PRAGMA table_info`; upgrade test from a
  committed **schema-12 project fixture** created by current code, including a
  project with sets `A` and `a`.
- An `0.1.0-alpha.2` build refuses the upgraded project with the existing
  message; say so in `docs/wiki/Upgrade-Compatibility.md`.

## 6. Tests required

- Unit: canonical form (case, surrounding whitespace, NFKC, digits); physical ↔
  logical round trip; `can_print` validation; collision detection.
- Integration: the schema-12 → new-schema migration with and without
  collisions; a lower-case logical set and a mapped `10 → A` set driven from
  definition → recognition → Resolve → Attendance → Answer Key → Results →
  Reports, with the key found (defect 5).
- GUI: set editor, Resolve set display.
- Architecture test (X11). Whole existing suite unchanged.

## 7. Verification

```powershell
.venv\Scripts\python.exe -m ruff check src tests tools scripts
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m pytest -q
```

## 8. Documentation updates

`docs/DATA_MODEL.md` (new columns, schema version); `docs/wiki/Examination-Sets.md`
(case-insensitive codes, physical marks); `docs/wiki/Upgrade-Compatibility.md`;
`ROADMAP.md` §7 status; new `PHASE_A_HANDOFF.md`; `development/CURRENT_STATE.md`;
README Development/Testing status (a `0.1.1-A` row); the `0.1.1` entry of
`docs/wiki/Development-Roadmap.md`; `CHANGELOG.md` `[Unreleased]`.

## 9. Status reporting

Report each track separately (ROADMAP.md §7): implemented; tested; synthetic
validation n/a; network share n/a; real scanners — not performed (no real
sheet with a mapped set has been read); production — not performed. Phase A may
be reported as "Implemented; tested", never as "Complete" on tests alone.

Commit in small commits, push the branch, do not merge unless asked.
