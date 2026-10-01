# Phase A (0.1.1-A) handoff — Set identity

Branch `feat/0.1.1-phase1-set-identity`, from `main` at `0ed96ed`. **Not merged,
not tagged, nothing released.** Prompt: the revised Phase 1 brief (equivalent to
[prompts/01-set-identity.md](prompts/01-set-identity.md)); where the two name
the branch differently, the revised brief's name was used.

## Baseline (before any change)

| | |
|---|---|
| Starting commit | `0ed96ed` (*feat: Results Dashboard tab*), branch `main`, clean apart from untracked `docs/debug/` |
| Starting version | `0.1.0-alpha.2` |
| Starting schema | 12 (`MIGRATIONS[-1].version`; migrations 1–12 taken) |
| pytest | **6,066 passed, 15 skipped, 0 failed**, 4 `stress` deselected (52 min 20 s) |
| ruff (`src tests tools scripts`) | All checks passed |
| mypy | Success: no issues found in 201 source files |

No baseline failures. The 15 skips are environment skips (no LibreOffice, no
symlink privilege, local real-world fixtures absent, a window-manager size
limit, two by-design skips).

## Implementation

### Version

`_version.py` `0.1.0-alpha.2` → `0.1.1-alpha.0`, alone, in `d76cebf`
(*chore: begin the 0.1.1-alpha.0 development line*), with a CHANGELOG
`[Unreleased]` note. No other version constant exists (enforced by
`test_the_version_is_not_hard_coded_anywhere_else_in_the_package`). The
editable install caches its metadata, so `pip install -e .` was re-run;
without it `test_the_installed_metadata_matches_the_module` fails (expected,
environment-only).

### Inventory of set-code assumptions before this phase

The architecture guard, run against the schema-12 code (`0ed96ed`), flags
**30** direct comparisons in `services/` and `domain/`, plus the private
`.upper()` rules. In summary:

| Where | Old behaviour |
|---|---|
| `domain/exam_sets.find_conflicting_set` | exact (documented decision: `a` ≠ `A`) |
| `services/project_sets.set_by_code` | exact SQL `code ==` |
| `services/review_store.sync_undefined_set_codes` | `value in {item.code ...}` exact |
| `services/reconciliation_store._placements` / `_lifecycle_placement` / `script_scope` / `batch_scripts` | `code == set_code`, `code in defined` exact |
| `services/scan_lifecycle` declared set, `outstanding_for_set`, `deferred_for_set`, replacement `set_code_agrees` | exact |
| `services/scoring.usable_set_code` | `.strip().upper()` |
| `services/scoring_store` | `save_key` revision numbering and `verify_key` supersession **exact**; `verified_key` / `list_keys` **upper-cased one side**; `verified_keys` dict keyed exactly; stale check exact — together defect 5 |
| `services/report_store` | template association / layout row lookups exact SQL; `set_overview` grouping exact |
| `services/report_readiness` | `result.set_code != set_code` exact |
| `services/result_analytics.analyse` | exact grouping against defined codes |
| `services/project_health._missing_verified_key_issue` | raw scan codes vs key codes, exact |
| `services/answer_key` | `key_from_scan`, `can_print_set_code`, `check_sheet_set` each upper-cased privately |
| GUI | Answer Key page `_normalise_code` / `offer_set_codes` upper-cased; Attendance disposition filter exact; Results preflight set difference exact; Reject dialog exact preselection |
| Synthetic generator | marks the set code itself; no mapping concept |

### Canonicalisation

`domain/set_identity.canonical_code(code) = unicodedata.normalize("NFKC", code).strip().upper()`.
A string rule only: `05` ≠ `5`, `010` ≠ `10`. The operator's spelling is stored
and displayed; comparison is canonical. Helpers: `same_set`, `SetCodeMap`
(canonical-keyed read-only mapping that records rather than overwrites a
canonical duplicate), `group_by_set`, `distinct_codes`, `find_collisions`,
`SetIdentity`.

**Why two modules.** The rule lives in `domain/set_identity.py` because the
migration (database layer) needs it and `database` may import `domain` but not
`services`. `services/set_identity.py` is the authority callers use: it
re-exports the rule and adds `load(database)`, `collisions`,
`require_no_collision` (raises `SetCollisionError`) and display helpers.

### Logical ↔ physical mapping

`ExamSet.physical_mark` (`""` = the sheet prints the code). `SetIdentity`:
`logical(code)` (logical lookup; a mark is not accepted), `for_reading(raw)`
(a mark **or** a logical code names its set), `logical_for_physical`,
`physical_for_logical`, `describe` (*Set 10 (A on sheet)*). Ambiguity rule:
within a project each canonical code and each canonical mark names at most one
set (`token_owner`), so Set 10 printed `A` and a Set `A`, or two sets printed
`A`, are refused. A mark equal to the set's own code is stored as `""`. When
the project template is known and has a set-code field, a mark must pass
`can_print_set_code`.

### Migration

**Migration 13** (next free number, confirmed at implementation time),
`_migration_013_set_identity`: `project_set.canonical_code VARCHAR(32) NULL`,
`project_set.physical_mark VARCHAR(32) NOT NULL DEFAULT ''`, both guarded by
`PRAGMA table_info`; fill `canonical_code`; `CREATE UNIQUE INDEX IF NOT EXISTS
ux_project_set_canonical_code ... WHERE canonical_code IS NOT NULL`. The model
declares the same index, so a fresh database gets it from migration 8's
`create_all`. **Schema version 13.** Filling the column in the migration is a
recorded exception to "structure not data" (deterministic, same-row, tiny
table, required by the index).

### Collision behaviour

As ARCHITECTURE_NOTES §7.3: rows in `display_order, code, set_id` order; the
first of a colliding group keeps the canonical code, later ones NULL. Nothing
deleted, renamed or merged; no other table touched. While a collision exists:

* a reading of the shared code resolves to **no** set (so Resolve raises
  *Set code not a defined set*, naming both);
* reconciliation of a colliding set's roster, `verify_key` for it, and report
  generation for it (`resolve_set_sources`) are refused with the sets named;
* **`score_batch` is refused project-wide** while any collision exists
  (deviation, see below);
* Project Health reports `SET_CODE_COLLISION` (warning) per pair;
  Project Configuration shows the rows in red with a banner.

Renaming or deleting one set recomputes `canonical_code`
(`project_sets._refresh_canonical_codes`); editing only a colliding set's
description is allowed; reordering never moves the canonical code.

### Translation boundary

`services/review_store.effective_set_codes`, as the roadmap proposed. It
assembles the paper's value after every Resolve decision (unchanged code, now
`_paper_set_codes`) and translates once: `EffectiveIdentifier.value` = logical
set, new `EffectiveIdentifier.as_read` = paper value; `was_corrected` compares
the paper value. Reconciliation, scoring, Results, reports, rejection
snapshots and the Answer Key offer read `value` and never interpret marks.
The review ledger, `batch_scan.set_code_value`, answer-key revisions, results,
generated reports and rejection records are not rewritten.

### Call sites migrated

Registry (`exam_sets`, `project_sets`), Resolve (`sync_undefined_set_codes`),
reconciliation placement (resolved **set ids** compared, not strings), Reject &
Rescan declared set (validated through `logical`, stored in the defined
spelling), outstanding/deferred lists, replacement agreement, scoring
(`usable_set_code` → `canonical_code`), key revision numbering, verification
supersession across spellings, `verified_key(s)` (`SetCodeMap`), `list_keys`,
`known_set_codes`, `key_overview`, stale-set check, `summarise`, report
associations and layouts, `set_overview`, `known_sets`, readiness, analytics,
Project Health, `key_from_scan`, `can_print_set_code`, `check_sheet_set`,
Results preflight, Attendance disposition filter.

### GUI

* **Project Configuration → Sets**: third column *Printed on sheet as*
  (appended so existing column indices hold); *Printed on sheet as* field in
  the set editor; refusals shown beside the table; collision banner and red
  rows; marks checked against the project template when it can be read.
* **Resolve**: evidence line *Reads as: Set 10 (A on sheet)* for set-code
  records; the full set-code editor translates a typed logical code of a
  mapped set to its mark (typing `10` records `A`) and its preview reads
  `D → A = Set 10 (A on sheet)`; the Reject dialog lists mapped sets by label
  and preselects canonically.
* **Answer Key**: offered codes de-duplicated canonically; the solution-sheet
  check receives the project's set identity; the review dialog files a sheet
  "as marked" under the **logical** set the mark names.
* Rendered and inspected: Project Configuration (mapped sets + collision) and
  Resolve with the set-code editor open, offscreen at the machine's 175 %
  scaling; one clipping issue found (preview text cut off) and fixed.

### Exports

The scan-results CSV keeps `set_code` = *Set (as read)* and, **only for a
project with at least one physical mark**, adds `set` (the logical set) after
`unresolved_conflicts`; every other project's CSV is column-for-column
unchanged. Report workbooks are one per logical set and carry no per-row set,
so nothing there conflated the two.

### Synthetic generator

`generate_dataset(..., physical_marks={"10": "A"})`, the dev dialog's set field
and `make_dataset --sets` accept `10=A, 11=B` (`parse_set_spec`). Candidate and
solution sheets are marked with the physical value; ground truth keeps it in
`set_code` / `set_marks` and records `metadata["logical_set"]`; keys stay keyed
by logical set with `physical_mark` in the manifest; `generator.physical_marks`
recorded. Without a mapping the output is unchanged.

### Architecture guard

`tests/unit/test_set_identity_architecture.py` (X11). AST scan of `services/`,
`domain/`, `database/`: forbids `==`/`!=`/`in`/`not in` where an operand is a
set-code name (`set_code`, `set_codes`, `set_code_value`, `declared_set_code`,
`recognised_set_code`, `declared_set`, `wanted_set`, `machine_set_code`,
`printed_as`, `physical_mark`) or an attribute `.code`, and `.upper()` /
`.lower()` / `.casefold()` on one. Permits: the two `set_identity` modules,
comparisons with `""`, lines marked `# set-identity: exact` (three
exact-spelling uses: "code unchanged" in `update_set`, "prefer the exact row"
in two report lookups), and every assignment / serialisation / display. GUI,
evaluation and tools are not scanned. A self-test proves the detector fires.

## Tests

| File | Tests | Kind |
|---|---|---|
| `tests/unit/test_set_identity.py` | 43 | unit: canonical form (case, space, NFKC, digits, leading zeros), `SetCodeMap`, mapping both ways and round trip, no mark, undefined readings, mark validation (malformed, duplicate, ambiguous, other set's code), collisions without merge |
| `tests/unit/test_set_identity_architecture.py` | 3 | architecture guard + detector self-test |
| `tests/integration/test_set_identity_migration.py` | 15 (1 env-gated) | migration from committed schema-12 projects written by the schema-12 build: unique sets, `A`/`a`, stored keys/results/rosters preserved byte-for-byte, backup taken, reopen and **identical rescoring**, health, read-only open without migration, refusals while colliding, rename resolves it, idempotent re-run, unique index, older build refusal (simulated; and **real**, against a `0ed96ed` checkout, when `OMRFLOW_SCHEMA12_CHECKOUT` is set — run locally, passed) |
| `tests/integration/test_set_identity_end_to_end.py` | 16 | Case A (`A` defined, sheets read `a`, key typed `a`) and Case B (`10` printed `A`, sheets read `A`): definition → stored readings → Resolve → Attendance → Answer Key check → scoring → Results → final report → CSV; plus an undefined reading staying undefined |
| `tests/integration/test_set_identity_synthetic.py` | 10 | mapped synthetic dataset rendered and read by the real engine; solution sheet checked as Set 10; `parse_set_spec` |
| `tests/gui/test_set_identity_gui.py` | 16 | Project Configuration mark, duplicate/ambiguous/unprintable refusal, edit/clear, collision banner and resolution; Resolve description, logical-code entry recording the mark, evidence; Reject dialog |
| **Total new** | **103** | |

Changed existing test: `tests/unit/test_exam_sets.py::test_comparison_is_case_sensitive`
→ `test_comparison_is_canonical_not_case_sensitive`. Its old expectation (`a`
free beside `A`) is exactly the documented decision this phase reverses
(ROADMAP A3 / ACCEPTANCE A3); no other existing test was edited.

Full verification on the branch (main checkout, after `pip install -e .`):

| | Result |
|---|---|
| pytest | **6,168 passed, 16 skipped, 0 failed**, 4 `stress` deselected (39 min 24 s). +102 passed / +1 skipped against the baseline: the new tests, the skip being the env-gated real-old-build test |
| ruff (`src tests tools scripts`) | All checks passed |
| mypy | Success: no issues found in 203 source files |

## Validation status

```text
Implemented:               Yes - on branch feat/0.1.1-phase1-set-identity, not merged.
Automated tests:           Passing (see above).
Synthetic validation:      n/a for this phase (the intake campaign belongs to G); a small mapped synthetic
                           dataset is read back by the real engine in the automated tests only.
Network-share validation:  n/a for this phase.
Real-scanner validation:   Not performed - no real sheet with a mapped set, and no real lower-case set
                           code, has been read.
Production qualification:  Not performed. Phase A is "implemented; tested", not complete.
```

No operator has used any of it; the GUI was driven by tests and inspected in
rendered screenshots only.

## Discrepancies between the plan and the code

1. **Scoring refusal is project-wide.** The plan says set-dependent stages
   refuse "until renamed". Reconciliation, key verification and reports are
   refused per colliding set; `score_batch` is refused for the whole project
   while any collision exists, because which key marks a script of the shared
   code cannot be decided and scoring is driven per batch. Stricter, not
   looser.
2. **`find_conflicting_set` also refuses a code that is another set's mark**
   (needed for the ambiguity rule; not stated in the plan).
3. `docs/wiki/Upgrade-Compatibility.md` stated project format 2 / schema 9;
   the code is format 3 / schema 12 (now 13). Corrected.
4. `project_sets.references_to_set` still says nothing links to
   `project_set.set_id`, although `candidate_roster.set_id` and
   `report_template_association.set_id` (migration 9, `ON DELETE RESTRICT`)
   do. Not changed here (deletion is out of scope); the database refuses such
   a delete, with a generic message.
5. Pre-existing UI defect seen while inspecting: Project Configuration's status
   line reads "No sets defined yet…" until the first action even when sets
   exist. Not fixed (unrelated to set identity).
6. A **read-only** open never migrates, so on a schema-12 project the ORM's
   new columns are absent; without a fallback every stage reading sets would
   fail. `list_sets` reads the legacy columns in that case. Not anticipated by
   the plan.
7. The plan's prompt named the branch `feat/0.1.1-a-set-identity`; the revised
   brief named `feat/0.1.1-phase1-set-identity`, which was used.

## Remaining limitations

* The architecture guard is name-based: a comparison through a generically
  named variable (`value in defined`) would not be caught; GUI, evaluation and
  tools are not scanned.
* Printability of a mark is checked only when the project's template can be
  read and has a set-code field; changing the template later does not
  re-validate stored marks.
* Results and Attendance tables show the logical set only; the raw *as read*
  value is visible on Resolve, in the CSV export (mapped projects) and in
  storage, not in those tables.
* Two verified key revisions of one logical set under different spellings can
  exist only in pre-phase data; `verified_keys` then uses the most recently
  verified and records the other as shadowed (no health check reports it).
* `project_set.code` keeps its exact unique constraint; the canonical guarantee
  is the partial index, which a direct SQL write with `canonical_code` NULL
  could bypass (the service never does).
* The generator applies `physical_marks` to roster-based datasets only.
* The real-old-build refusal test needs a local `0ed96ed` checkout and is
  skipped otherwise (one new skip in the default run).
* Answer Key tiles and the Reports page do not show printed marks.
* Nothing here has met a real scanner, real paper or an operator.

## Next phase

> Phase 2 is the ScanSession + finite ScanBatch lifecycle foundation. It has
> not been implemented by this branch.

It should start from this branch once merged (schema 13, so its migration is
**14**), reuse `services/set_identity` for any set it touches, keep
`review_store.effective_set_codes` as the only physical → logical boundary, and
note that `score_batch` currently refuses project-wide on a set collision.
