# Changelog

All notable changes to OMRFlow are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Versions below 1.0 make no compatibility promises.

## [Unreleased]

### Added — Phase 10

Integration, Recovery & Production Hardening: the pipeline survives abrupt
termination, detects its own damage, avoids silent duplication, and
demonstrates that its architecture scales to a 100,000-sheet examination.
No recognition, scoring or reporting rule changed. Full detail:
`development/PHASE_10_HANDOFF.md`.

- **`omr_scanner.services.scan_provenance`**: streaming SHA-256 content
  hashing (bounded memory regardless of file size), exact-duplicate-scan
  detection, source-scan availability classification (present/missing/
  changed), and hash-verified relinking of a moved scan. Wired into the
  Scan page's background worker thread, never the GUI thread.
- **`omr_scanner.services.project_lock`**: a second OMRFlow process cannot
  open a project another already has open for writing. A lock left behind
  by a crash is never removed automatically; the operator is shown who (or
  what) held it and chooses cancel, read-only, or an explicit override.
- **A genuine SQLite read-only connection mode**
  (`database.engine.open_project_database(read_only=True)`), refusing every
  write at the database level, not by application convention alone.
- **`omr_scanner.services.project_backup`**: snapshots via SQLite's own
  online backup API (safe against a live database, never a raw file copy),
  with a manifest written only after the backup finishes and is hashed, so
  an interrupted backup can never be mistaken for a complete one.
- **`omr_scanner.services.project_health`**: an on-demand comprehensive
  check (structural integrity, foreign keys, schema version, stale jobs,
  source-scan availability, unresolved Phase 6/7 exceptions, sets missing a
  verified Phase 8 key, backup presence, free disk space) with deliberately
  no repair action.
- **Reprocessing** (`services.batch_store.mark_for_reprocessing` and its
  `reprocess_failed_scans`/`reprocess_batch` wrappers): a sheet's superseded
  machine reading is archived, append-only, before it is reset to `pending`
  — reprocessed twice, it keeps both readings on record.
- **Worker recycling and configurable OpenCV threads**
  (`config.processing.ProcessingSettings`), exposed under *File → Settings →
  Advanced*.
- **`omr_scanner.evaluation.stress_dataset`**: a deterministic,
  index-addressable synthetic-sheet generator for the mandatory
  100,000-sheet stress test — sheet *N* reproduces independently from
  `(seed, N)` alone, across 15 case kinds.
- **`omr_scanner.evaluation.stress_runner`** and
  **`omr_scanner.tools.benchmark_stress`**: bounded-disk-usage orchestration
  reusing the unmodified batch pipeline, and a headless CLI to run or resume
  a stress batch of any size, with telemetry and a JSON report.
- **`gui.health_dialog.ProjectHealthDialog`**: one dialog, *Tools → Project
  Health / Recovery…*, for both the health check and backup/restore.

### Fixed — Phase 10 (found during its own testing, before release)

- A modal-dialog-in-a-testable-method hang was designed around, not
  reintroduced, for the new project-lock-conflict dialog — the third time
  this defect class has been specifically guarded against (Phases 8 and 9
  each found and fixed a real instance).
- `ProcessPoolExecutor`'s own `max_tasks_per_child` parameter was found, by
  a minimal reproduction with no OMRFlow code involved, to hang permanently
  on this platform after one worker-recycle generation. Worker recycling is
  implemented instead as a sequence of short-lived pools.
- The stress-test virtual source-path scheme (`stress://seed/index`) did
  not survive a `pathlib.Path` round trip on Windows, silently losing its
  own prefix. Fixed by choosing a path-separator-free identity string.
- The benchmark CLI's first version closed only a `ProjectDatabase`, never
  the `ProjectSession` that holds the project lock — every run after the
  first refused to open, even after a clean exit.
- Killing the batch coordinator process leaves its worker processes
  running, orphaned (observed directly during forced kill/resume testing).
  Does not affect data integrity (workers never had database write access);
  automatic cleanup is not yet implemented — see
  `development/PHASE_10_HANDOFF.md` §8/§12.

### Added — Phase 9

Result Management & Reporting: Phase 8's stored marks become the workbooks an
examination office files — per set, independently, from a copy of the
office's own result/absentee template. Full detail:
`development/PHASE_09_HANDOFF.md`; operator description: `docs/reporting.md`.

- **A set's own result template is authoritative for its roster.** Order,
  Roll No., name and existing absentee markers are the template's; Phase 7/8
  supply attendance and marks. This is why an absentee can appear on a
  report at all — nothing in Phases 1-8 records which set an absent
  candidate (who has no script) was assigned to.
- **`omr_scanner.domain.reporting`**: standard competition ranking
  (`compute_ranks`, checked against the brief's own `90, 88, 88, 85 → 1, 2,
  2, 4` example), the dynamic `RANK.EQ` formula generator (never a hard-coded
  column or row range), spreadsheet-injection mitigation for user-controlled
  text, and the readiness-issue vocabulary.
- **`omr_scanner.services.report_template`**: reads a real institution's
  workbook — header-row detection tolerant of decorative title rows, column
  mapping that reports ambiguity rather than guessing (reusing Phase 7's own
  identifier normalisation and header-matching algorithm, promoted to public
  names for exactly this reuse).
- **`omr_scanner.services.report_readiness`**: cross-checks a template's
  roster against Phase 7's reconciliation and Phase 8's scoring, producing
  every disagreement as a named, addressable issue — a missing candidate, a
  duplicate Roll No., an absentee-status mismatch, a present candidate with
  no score — never silently concealed.
- **`omr_scanner.reporting.excel`**: builds Rollwise (populated in place
  inside a *copy* of the template — the original is proven, by SHA-256,
  never opened for writing), Meritwise (sorted by mark descending, Roll No.
  ascending as a deterministic tie-break), Summary, Answer Key and
  Processing Log sheets, plus header/logo/font/page-setup layout that
  changes nothing when left at its defaults.
- **`omr_scanner.reporting.pdf`**: a dependency-injected PDF exporter
  abstraction over LibreOffice's headless conversion (which is what actually
  recalculates the `RANK.EQ` formulas before rendering); reports plainly,
  never pretends to succeed, when no PDF engine is available.
- **`omr_scanner.services.report_store`**: per-set template and layout
  persistence, the append-only `GeneratedReport` audit trail, and
  **regenerate-never-patch** orchestration — every generation rebuilds the
  whole workbook from Phase 7/8's current stored state.
- **Never silently overwrites an existing report.** A second generation
  writes `<name>_1.xlsx`; the first file is untouched.
- **The Reports stage**: a per-set overview table, template
  association/validation, a report-layout dialog, background generation with
  progress, and "Generate All Sets" — one set's failure never hides
  another's success.
- **Phases 1-8 are untouched.**

### Fixed — Phase 9 (found during its own testing, before release)

- **An automatic worker-completion callback could hang the application
  indefinitely.** `ReportsPage._on_generated` opened a modal `QMessageBox`
  summary whenever a generation run included a blocked or failed set — a
  routine outcome, not an exceptional one — and nothing in an automated or
  headless context could ever dismiss it. The same defect class Phase 8's
  `ResultsPage._on_scored` was fixed for during that phase's own audit,
  rediscovered independently here. Replaced with an inline status label; a
  genuine worker exception is still shown, as text, never as a blocking
  dialog.

### Fixed — Step 4 Scan GUI readability/layout refinement

The left-hand control column on the Scan stage (Template, Scans, Processing,
Output) had no scroll area, so its combined natural height - about 900
logical pixels, nearly half of it the Processing group's seven action
buttons, status line and progress readout - became the whole page's minimum
height. On any window shorter than that (a smaller laptop panel, a restored
rather than maximised window, or higher Windows display scaling), Qt had no
choice but to compress every widget in the column below its own size hint,
which is what made Processing's button labels and icons overlap and clip.

The column now lives in its own scroll area, so it always lays out at full,
uncompressed size; a short window scrolls it instead of squeezing it. The
splitter separating it from the preview and results panels no longer lets a
dragged divider collapse it either. Presentation only - no processing,
multiprocessing, cancellation, resume, retry, reprocessing or conflict-review
behaviour changed. `src/omr_scanner/gui/scan/page.py`.

### Added — Phase 8

Answer-Key & Scoring Engine: recognised answers become marks that can be
defended — **reproducible from stored inputs, traceable to the exact key
revision that produced them, and recomputed rather than patched when a rule
changes**. Full detail: `development/PHASE_08_HANDOFF.md`; operator
description: `docs/scoring.md`.

- **A canonical answer string**, one character per question in question order:
  the template's own option labels, `_` for a blank, `?` for a **confirmed**
  multiple. Question *N* is character *N*, always — never compressed, never
  shortened by a blank.
- **An unresolved reading is not a `?`.** A sheet still in the Phase 6 queue
  **blocks** scoring, naming the question, rather than being marked as a blank
  or as a multiple nobody has actually looked at.
- **`omr_scanner.domain.scoring`**: the arithmetic, as pure functions over
  value objects. `score_answers` reads no clock, no configuration and no
  database, which is what makes "recompute, never patch" testable.
- **Exact arithmetic.** Every mark is a `fractions.Fraction`; a decimal typed
  into the configuration is read with `Fraction(Decimal(text))` and never
  through `float`. `Decimal` was rejected because `1/3` has no terminating
  decimal expansion and the 1-per-3 rule is a published marking scheme.
  Rounding happens **once**, at the end, for display, and never feeds back.
- **`omr_scanner.services.answer_key`**: reading a key from text or from a
  scanned solution sheet, through the *existing* recognition engine. Spaces,
  line breaks and commas are ignored so a key pastes out of a spreadsheet;
  **anything else is reported**, because a key silently shortened by one stray
  character marks every candidate against the wrong questions from there on.
- **Validation that names the question** and reports every problem at once:
  *"The answer key contains 98 answer(s), but this template contains 100
  questions. Please add answers for Questions 99-100."*
- **One independent key per question-paper set**, with set codes never assumed
  to be one character. A candidate is marked against **their own** set's key;
  there is no fallback, ever.
- **Verification before scoring.** A key is a draft until a named person checks
  it — including one read off a solution sheet, because recognition completing
  does not make a key right. Blanks and double marks on a solution sheet are
  listed and must be dealt with first.
- **Revisions.** A verified key is never edited: correcting it creates the next
  revision and supersedes the old one, which is **kept**, because results point
  at it. Revision numbering is owned by the store, so two operators cannot both
  create "revision 2".
- **Wrong questions**, flagged independently per set. Every scored candidate
  receives full credit whatever they marked — right, wrong, multiple or blank —
  and **no deduction is ever applied**. The rule is checked *first*, above the
  blank and multiple rules, which is the ordering an implementation that
  checked "blank first" gets wrong.
- **Four negative-marking modes**: none, a fixed deduction, 1 mark per 3 wrong,
  1 mark per 4 wrong. The last two deduct exactly `1/3` and `1/4` of a *mark*,
  and **fractional penalties are never truncated** — one wrong answer costs
  `1/3`, not nothing. Multiple answers default to the incorrect penalty and are
  separately configurable.
- **A configurable minimum total**, recorded in the policy rather than
  hard-coded, and applied **once** at the end so a mid-paper dip is
  recoverable. A result can therefore say whether it was clamped.
- **`CandidateResult` stores its inputs**, not just its mark: the answer
  string, the machine's own string, the set, the key revision and the policy
  revision. **Every result records the exact revisions used**, so a later key
  never retroactively changes what an earlier mark was computed from.
- **The per-question breakdown is regenerated, not stored.** A million rows for
  a ten-thousand-candidate batch would be a second copy of the truth that could
  contradict the total; rerunning the same pure function cannot.
- **An absent candidate has no mark**, not a zero — zero would be
  indistinguishable from somebody who sat the paper and answered nothing.
- **Staleness.** Changing a key, a rule, an answer, a set or a reconciliation
  makes affected results stale. They **keep their mark** — a true record of
  what the earlier inputs produced — and say so, and recalculating runs the
  whole scorer again. **Nothing adds a delta to an existing mark**, asserted by
  a test that corrupts a stored score and checks it is ignored.
- **A cancelled run writes nothing**, rather than leaving a batch half marked
  under two policies while claiming to be current.
- **Three additive tables, created by migration 5**: `answer_key_revision`,
  `scoring_policy_revision` (marks stored as exact rational strings) and
  `candidate_result`.
- **The Answer Key stage**: set and revision choosers, the key as text and as a
  question-by-question table driven by one model, wrong-question flags,
  validation, save and verify.
- **The Results stage**: a scoring-configuration dialog with a worked-example
  preview, a pre-scoring check listing every blocked candidate at once, batch
  marking off the GUI thread with progress, a filterable results table, and a
  per-question detail showing what the machine read beside what was scored.
- **Phases 5, 6 and 7 are untouched.**

### Fixed

- **An unread question-paper set was reported as a missing answer key.**
  Recognition assembles a field value with `_` for an unmarked position, so a
  blank set code arrived as `"_"` and produced *"no verified answer key for
  Set _"* — sending an operator to look for a key when the problem was the
  sheet. It now reports that no set was read. (Found by inspecting a Phase 8
  screenshot.)
- **The Results page listed the verified keys only when the project was
  opened**, so a key verified on the Answer Key stage left it saying "none".

### Fixed — Phase 8 independent audit

Found by mapping the phase brief onto the code and checking behaviour rather
than names. The suite was green before and after; none of these was caught by
an existing test. Detail in `development/PHASE_08_HANDOFF.md` §11a.

- **A key written for a differently numbered paper withdrew no questions at
  all.** `score_answers` took the first printed question number from its caller
  and never compared it with the key's own. Because `wrong_questions` holds
  *printed* numbers, a key written for a paper starting at question 1 and used
  against one numbered from 101 lined its answers up perfectly and silently
  granted nobody the credit the examiners had granted. It is refused now, and
  reported as a blocked candidate with a sentence rather than a traceback.
- **A candidate recorded absent whose script had turned up was filed as
  "Absent".** That contradiction is Phase 7's to settle; recording it as an
  outcome answered the question by ignoring it and left a real script unmarked.
  Only a *confirmed* absence is an outcome now.
- **Verifying an answer key did not reach the stage that uses it.**
  `key_saved`, `key_verified` and `policy_changed` were emitted with nothing
  connected to them, and `offer_set_codes` had no caller at all, so the Results
  stage went on reporting "Verified answer keys: none" with freshly stale
  results shown as current. The Results stage also learned its batch only when
  a project was *opened*, so a batch scanned during the session was invisible
  to it and "Calculate Results" refused to run.
- **A template edit penalised the candidates.** A sheet read when the paper
  offered `A/B/C/D/E` and marked against a template since cut to `A/B/C/D` held
  an `E` the canonical string could not name. It became `?` — which attracts
  the multiple deduction — so a candidate lost marks for somebody else's edit.
  Such a sheet blocks now and asks to be read again.
- **A deduction for a multiple answer was stored, previewed and then
  ignored** outside the fixed mode, although the dialog could save exactly that
  combination. It applies in every penalising mode now, and the field is
  offered in all of them.
- **A result that could not be scored went on asserting a problem that had been
  fixed** — *"no verified answer key for Set C"*, after the key was verified.
  Its recorded reasons are compared against the reasons it would be given now.
- **Opening and saving the scoring configuration could rewrite an exact rule.**
  A blank mark of `1/3` came back as `3333/10000` through the spin box's
  `float`, creating a revision nobody asked for and making every result stale
  under a rule that was never typed. Untouched fields keep their exact value.
  An unparseable field also no longer becomes a silent zero, which was
  indistinguishable from an operator typing one.
- **Verification applied to the stored revision while the editor could show
  something else**, so editing the key and pressing Verify locked the older
  text and discarded the edit without saying so. The button is disabled while
  the editor holds unsaved changes.
- **The Results stage read the whole batch twice per refresh**, once for the
  table and again for the summary line above it.
- A **cancelled** run now says so in the summary instead of looking like a
  finished one, and a run that finishes while the stage is shutting down is
  dropped rather than redrawing a table whose widgets are going away.

### Added — Phase 7

Candidate & Attendance Reconciliation: the candidate list an examination office
already holds is imported, the scanned scripts are matched against it, and
every discrepancy becomes an explicit exception somebody has to look at.
**Nothing is dropped, merged or silently chosen between.** Full detail:
`development/PHASE_07_HANDOFF.md`; operator description:
`docs/reconciliation.md`.

- **Four values are kept independently traceable**: what the roster file said,
  what recognition read, what an operator decided, and the effective value that
  follows. Only the last is computed. `registered_candidate` is write-once and
  the machine's reading is never overwritten, so overriding an attendance or
  reassigning a script can never cost the record of what it changed *from*.
- **`omr_scanner.domain.reconciliation`**: the vocabulary — seven
  classifications, five issues that can co-occur, four attendance states, nine
  actions and nine reason codes, with the operator-facing wording on the enum
  rather than scattered through the interface.
- **`omr_scanner.services.candidate_import`**: CSV and `.xlsx` (via
  `openpyxl`; pandas deliberately not used for a single pass over a
  spreadsheet). Worksheet selection, a preview of the real file, and a column
  mapping the operator confirms — **Candidate ID** required, **Name** and
  **Marks / Attendance** optional.
- **A marks column doubles as attendance**: `ABSENT` or `ABS`, any case, any
  spacing, means absent; anything else — including a blank cell and a mark of
  zero — means not marked absent. Compared as **whole tokens**, so `ABSENTEE`,
  `ABSENCE` and `ABS123` are not absences. The column name is never
  hard-coded: `Total (90)` matches because `total` does.
- **Candidate IDs are identifiers, not quantities.** An Excel cell holding
  `15000001` imports as `"15000001"`, never `"15000001.0"`; a non-integral
  value is left alone rather than rounded, because rounding is how two
  candidates become one; text IDs and leading zeros pass through untouched.
- **It refuses to guess.** Two columns that equally name a candidate ID stop
  the import and ask; a repeated candidate ID stops it with the ID and both row
  numbers, because the file states two different facts about one person. A
  failed import leaves **nothing** behind.
- **Every malformed input fails readably** rather than crashing: corrupt
  workbook, a zip that is not one, empty worksheet, header-only file, legacy
  `.xls` (with the two-click fix), unsupported extension, malformed CSV, UTF-8
  BOM, a mapping naming one column twice.
- **`omr_scanner.services.reconciliation`**: one pure, deterministic function
  over value objects. No clock, no config, no database — which is what lets the
  rules be tested as a table and makes a re-run idempotent.
- **Seven classifications**: Matched, Absent confirmed, Unknown candidate ID,
  Duplicate script, Present but no script found, Marked absent but script
  found, and **Candidate ID not yet resolved** — kept distinct so a roll number
  still awaiting review on the Resolve stage is never reported as an unknown
  candidate. The two need different actions.
- **Conditions that co-occur are both reported.** A candidate marked absent
  with two scripts carries both issues; the headline is chosen by documented
  precedence and never replaces the set. An architecture holding one status
  would make a physical script invisible.
- **Six additive tables, created by migration 4**, plus `entity_type` and
  `entity_id` on `audit_event` so a decision about a candidate or a script is
  recorded in the **same append-only ledger**, under the same triggers, as a
  decision about a recognition conflict. Existing audit rows were
  **deliberately not backfilled** — an `UPDATE` there is aborted by those
  triggers, so the new column's default was chosen to be already correct.
- **Resolution that never destroys**: assign a script to the right candidate,
  set an accidental re-scan **aside** (its scan row, recognition result, reason
  and audit trail all kept — never deleted), nominate the working script,
  override attendance, or accept an exception as-is. Every action needs a named
  operator and a reason.
- **Cascades are surfaced, not hidden.** Every decision re-runs reconciliation,
  so assigning a script to a candidate who already has one reports the new
  duplicate at once rather than at export time. An entry whose issues a
  decision did not clear stays **open**; being touched is not being fixed.
- **Reconciliation output is a cache, decisions are the input.** Entry and
  script rows are rewritten wholesale on every run, so a stale classification
  cannot survive a roster change; operator decisions live in their own table
  and steer the next run.
- **Re-importing supersedes rather than merges**, after a warning. The old
  roster and every decision taken against it are kept — an audit trail pointing
  at a deleted roster explains nothing.
- **The Attendance stage** (`omr_scanner.gui.attendance`), no longer a
  placeholder: roster bar, live summary counts, a table filtered and counted in
  SQL, a detail panel listing every script including set-aside ones, the
  entry's full history, and the decision panel. Import and reconciliation both
  run off the GUI thread.
- **Download Sample Template…** hands the operator a packaged example workbook
  containing placeholder data only. It lives **inside** the package and is read
  through `importlib.resources`, so it works in a wheel and a frozen build —
  the top-level `resources/` directory is dev fixtures and is never shipped.
- **No candidate name, ID or mark reaches an application log**, enforced by 18
  dedicated tests, a grep that fails if a Phase 7 module formats candidate data
  into a log call, and a smoke check that captures the root logger through a
  whole import-reconcile-assign cycle. Even an unexpected exception is logged
  by type only.
- **Phases 5 and 6 are untouched.** The worker pool, cancellation, resume and
  progress are exactly as they were — asserted by reconciling the same sheets
  read on 1 worker and on 4 and comparing, with the reported worker count
  checked so the comparison cannot be between two sequential runs.

### Fixed

- **A candidate's roll number could reach the application log.** With renaming
  on, `batch_processor` logged the *destination* file name of each copied
  scan — which is the roll number. Two log lines now omit it. (Phase 3 code;
  found while making the Phase 7 privacy rule executable.)
- **A superseded background worker was never joined.** Dropping the reference
  to a still-running `QThread` leaves a thread whose parent is later destroyed,
  which aborts the process with no traceback. The Resolve page (Phase 6) and
  both Phase 7 workers now track every worker and join all of them on
  shutdown, not just the most recent.

### Added — Phase 6

Conflict Detection & Human Resolution: everything recognition was unsure about
now becomes a reviewable queue, and every decision a person makes is recorded
against their name — **without the machine's own reading ever being
overwritten**. Full detail: `development/PHASE_06_HANDOFF.md`; operator
description: `docs/conflict_review.md`.

- **`omr_scanner.domain.review`**: the pure vocabulary, with no Qt, no database
  and no OpenCV in it — `ConflictType` (22 members, each knowing its own scope,
  whether it is a processing failure rather than a value, and whether it can be
  corrected), `ConflictState`, `ReviewAction`, `ReasonCode`, `FieldRef`,
  `MachineObservation`, `Provenance`, `ReviewCounts`. Putting the taxonomy in
  the domain layer is what lets the GUI build a type filter and the exporter
  read a provenance without either importing the other.
- **`omr_scanner.services.conflict_policy`**: the single, deterministic place
  where a recognition result becomes conflicts. It reads the engine's own
  `needs_review` judgement — which the decision layer computed from **the
  template's own** `ambiguity_margin` and `min_confidence` — rather than
  introducing a second opinion with new numbers. Calibrating a template in
  Phase 4 therefore moves the conflict queue with it. Pure functions: same
  result in, same conflicts out.
- **Two additive tables, created by migration 3**: `review_conflict` (one thing
  on one sheet to look at, with the machine's observation snapshotted for
  querying) and `audit_event` (the append-only ledger). Conflict identity is
  `(batch_id, scan_id, conflict_type, zone_id, group_key)` under a unique
  constraint, so re-running, resuming or retrying a batch **updates** conflicts
  instead of creating a second set.
- **`omr_scanner.services.review_store`**: the repository. One write path for
  events (`_append_event`) and no update or delete for them at all. Every
  decision is one transaction — the event and the state change commit together
  or neither does.
- **The effective value is projected, not stored.** There is no `resolved_value`
  column. `provenance_for` folds a conflict's ordered events over the machine's
  reading, so a correction, a reopening and a second correction each *add* to
  the record. Reopening restores the machine's value while keeping the
  superseded correction, its reviewer, its reason and its timestamp. A stored
  column would have been a third copy of the truth to keep in step; a fold
  cannot drift.
- **Append-only enforced three ways** (not by developer discipline): no update
  or delete on the service surface, none anywhere in the application, and two
  SQLite triggers that `RAISE(ABORT)` on any `UPDATE` or `DELETE` of
  `audit_event`. The table deliberately carries **no foreign key**, so the
  record that a named person decided something outlives the row it was about —
  and so Phases 7-9 can audit into the same ledger without a schema change.
- **The Resolve stage** (`omr_scanner.gui.review`), no longer a placeholder: a
  queue filtered by state, type and a search over student ID and file name,
  with live counts; and a workspace showing the disputed bubbles **zoomed**, the
  whole **normalised** sheet, and **the original scan** — the last with the
  field located through the recognition engine's own inverse homography rather
  than a second projective calculation in the GUI. Overlays are Phase 4's.
- **The evidence panel** shows what the machine saw, its status, its decision
  score and each option's measured **fill score**, labelled as a coverage
  measurement and never as a "probability".
- **Accept / Correct / Defer / Reopen**, each requiring a named reviewer (*File
  > Settings > Reviewer*, remembered between sessions) and each correction a
  reason. A correction without a name is refused outright. Keyboard: ← / → to
  step, **Enter** to accept, **D** to defer — deliberately no shortcut for
  choosing a value.
- **Duplicate student IDs detected across the whole batch**, in the coordinator
  after the run, because a duplicate is not a property of one sheet and a worker
  process must not open the database.
- **Processing failures are distinguished from ambiguous values.** A corrupt
  file or a sheet that would not register offers acknowledgement and deferral
  and no value buttons, because `A`/`B`/`C`/`D` is not an answer to an
  undecodable JPEG.
- **Special machine values are preserved.** A double mark exported as `B-D` is
  still `B-D` in the ledger after a reviewer decides it meant `B`.
- **CSV export** gained `value_source` (`machine` / `human`) and
  `unresolved_conflicts`, both appended after the existing columns. Resolutions
  reach the export through one function and nothing else computes them.
  Exporting a batch with conflicts still open **warns and states the count**; it
  does not block, because an interim export is legitimate.
- **`ScanResult.source_transform`**: the inverse homography the engine already
  computed, now carried on the result. Added so the original scan can be
  highlighted from the engine's own geometry; older stored results default it
  and still load.
- Review re-reads the one selected sheet in a background thread rather than
  storing per-bubble evidence for a whole batch — five hundred bubble records
  per sheet is most of a gigabyte over ten thousand sheets. Recognition is
  deterministic, so the evidence reproduces exactly. The same sheet is not
  decoded twice while walking its own conflicts.
- **Phase 5 is untouched.** The worker pool, worker count, cancellation, resume,
  retry and progress reporting are exactly as they were; detection runs in the
  coordinator, after the batch.

### Added — Phase 5

Batch Scan Processing Pipeline: a batch is now **durable and resumable**. With
a project open, every scan's result is written to the project database as it
finishes, so a run that is cancelled, closed or killed is continued rather than
restarted. Full detail: `development/PHASE_05_HANDOFF.md`; operator
description: `docs/scan_workflow.md` §11-§13.

- **Two additive tables, created by migration 2**: `scan_batch` (one run - its
  folder, template, both template fingerprints, engine version, settings and
  lifecycle status) and `batch_scan` (one row per sheet - status, attempt count,
  recognised roll and set code, output name, failure reason and a
  machine-readable error category, timings, and the full `ScanResult` as JSON).
  The result is stored through `ScanResult.to_dict()` - already a versioned,
  round-tripping contract - rather than exploded into columns that would need a
  migration every time recognition gained a measurement.
- **`omr_scanner.services.batch_store`**: the repository layer, with no Qt and
  no recognition in it. Attached to a run through
  `process_batch`'s **existing** `on_result` hook, so a caller with no project
  - a test, the benchmark, `python -m omr_scanner.tools.recognise` - runs
  exactly the code path it always did. `batch_processor` still knows nothing
  about a database.
- **Incremental persistence, in bounded groups.** Results are committed every
  25 sheets or every 2 seconds, whichever comes first: one `fsync` per sheet
  dominates a run on a spinning disk or a synchronised folder, and this bounds
  what an abrupt termination can cost to a second or two of finished work. The
  bound is asserted by a test, not merely intended.
- **Resume Batch** processes only the scans that were never finished, in batch
  order - so duplicate-identifier suffixes stay stable across an interruption.
  **Retry Failed** re-reads the failures and only those, incrementing each
  scan's attempt count.
- **Crash recovery on project open.** Rows left `queued` or `processing` can
  only be in those states while some process owns them, and a project being
  *opened* proves none does. They return to `pending` - never to `failed`,
  because "we do not know what happened to this sheet" is not the same as "this
  sheet is bad", and marking it failed would silently exclude exactly the sheets
  a crash caught.
- **Resume refuses to mix incompatible results silently.** Resuming with an
  edited template or retuned thresholds compares the stored fingerprints
  against the current ones and states precisely what changed before asking. The
  fingerprints are the same ones Phase 4 uses for calibration staleness.
- **Closing the window mid-batch** warns, then stops the run and *waits* for it
  - the pool torn down and the last results flushed - before releasing the
  database. That order is what leaves the batch resumable.
- **A storage failure is not a recognition failure.** The batch continues (the
  remaining sheets are still worth reading, and the results stay in memory where
  they can be exported), the buffer is kept for a later retry, and the page
  reports it in a dialog at the end. A run whose results could not be written is
  never presented as a clean success. `BatchStatus.COMPLETED` and
  `COMPLETED_WITH_ERRORS` are distinct for the same reason.
- **A scan-list filter** - All / Completed / Needs review / Failed / Not
  processed. Rows are hidden, never rebuilt, because a row index *is* an index
  into the page's entries everywhere else.
- **Bounded submission in the worker pool.** `recognise_in_parallel` previously
  submitted every path up front; it now keeps at most four tasks per worker
  outstanding, so a ten-thousand-sheet batch no longer builds ten thousand
  futures before reading the first page, and cancellation responds sooner.
  Nothing else about the pool changed - same `spawn` start method, same
  per-worker initialisation, same crash isolation, same worker-count policy.
- **Original scans remain byte-for-byte unchanged**, now proved rather than
  asserted: `tests/integration/test_batch_persistence.py` hashes every input
  before a batch that includes renaming *and* a deliberately corrupt file,
  processes it, and hashes again. The same check runs in the `qtguitesting`
  smoke suite against the repository's real sample sheet.
- 72 new tests: 35 unit (`test_batch_store.py`), 17 integration
  (`test_batch_persistence.py`, covering the plan's tests A, B, C, E, F, G, H,
  I, J, K against the real engine) and 20 GUI (`test_scan_persistence.py`,
  covering D, E and L through the real page, a real project and a real
  `QThread`). Three new `qtguitesting` smoke checks. Measured throughput on 24
  real scans: 1.00x / 1.30x / 1.79x / 1.98x at 1/2/4/8 workers.

**Phase 5 makes a batch reliable and resumable. It says nothing about whether
the values it durably recorded are correct** - Phase 3 recognition remains
pending validation with a sufficiently large real-world dataset.

Phase 3 - batch scanning and recognition. A user can now read filled answer
sheets against a template: import one or many scans, have them rectified and
recognised - concurrently across CPU cores, if the machine has them - review the
result with an overlay, optionally file the images under
their detected roll numbers, and export CSV. Recognition has been validated
against one real scanned sheet and geometric variants of it, **not** against a
corpus of independently filled papers; this build must not be used for
examination processing.

### Added — Phase 4

Template Calibration & Validation. A user can now verify and tune a saved
template against representative real scans, before trusting it with a batch,
entirely by reusing Phase 3's own registration and recognition - not a second
engine. Full detail: `development/PHASE_04_HANDOFF.md`;
operator procedure: `docs/calibration_workflow.md`.

- **`omr_scanner.gui.calibration`**: a new workflow stage, **Calibrate**,
  between Template and Scan. Load a template, add or remove test scans (PNG,
  JPEG, TIFF, BMP), step through them, and run one or all through the existing
  pipeline in a diagnostic mode. A layered overlay over the rectified page:
  detected-vs-expected registration markers, bubble-sampling geometry, and
  recognised selections/scores, each independently toggled. Click any bubble
  for its field, question, centre, sampling window, raw score, active
  threshold and classification. A field filter (All / Student ID / Set Code /
  Questions / Markers) narrows what is shown.
- **`omr_scanner.services.recognition_service.CalibrationSession`**: opens a
  scan once - load, register, measure - and can then be asked to re-decide
  against a *different* `RecognitionSettings` as many times as needed without
  repeating any of that work. Built by splitting the existing `_recognise`
  pipeline at its natural measure/decide boundary; the full pre-existing
  recognition, batch and benchmark test suites pass unchanged, which is the
  evidence nothing about ordinary recognition moved.
- **Four adjustable recognition thresholds**, and no others invented for this
  phase: `fill_ratio_threshold`, `blank_ratio_threshold`, `ambiguity_margin`,
  `min_confidence` - each a slider plus an exact numeric field. Changing one
  reclassifies every open scan, updates the overlay and the quality summary,
  and starts no worker thread and repeats no registration.
- **Non-destructive calibration.** A working value distinct from the
  template's saved value; *Reset to Template*, *Reset to Defaults* (the two
  kept visibly separate) and an explicit *Save to Template* that names the
  status about to be recorded before it writes anything.
- **`omr_scanner.services.calibration_service`**: `evaluate_calibration` - a
  four-state verdict per scan (`CalibrationStatus`: **Validation Passed** /
  **Validation Passed With Warnings** / **Needs Review** / **Calibration
  Failed**), built only from measurable Phase 3 signals already on
  `ScanResult` - registration status, geometry-class alignment warnings, the
  fraction of unusable bubbles, systematic ambiguity, near-threshold marks -
  never an arbitrary or invented check. `aggregate_calibration` rolls a
  sample up worst-status-wins. `write_calibration_report` writes a JSON
  report that says, in the file itself, what it does and does not prove.
- **Registration failure is judged first and alone.** If the existing marker
  detector cannot register a scan, every other calibration check is skipped
  and the verdict is `Calibration Failed` with no fields, answers or bubbles
  at all - never a plausible-looking wrong result from an unregistered page.
  Verified against a deliberately mismatched template on the real sample
  sheet as well as synthetic ones.
- **`OmrTemplate.calibration`**: one additive field (`CalibrationRecord`)
  recording when a template was last calibrated, against how many scans, and
  two content fingerprints (geometry, recognition settings). A later edit to
  either invalidates the record automatically
  (`OmrTemplate.is_calibration_current()`); documents saved before this phase
  load unchanged. The Scan page shows a non-blocking notice - never a block -
  when the loaded template has never been calibrated or has gone stale.
- **`MarkerView`** gained `canonical_x/y` (a detected marker reprojected
  through the same fitted homography that rectified the page) and
  `expected_x/y` (the template's own declared marker centre, in the same
  coordinate space) - both additive, both computed once from existing
  geometry, never a second calculation.
- **The sampled region is drawn, not just the printed bubble.**
  `BubbleMeasurement` records `sample_half_width`/`sample_half_height` at the
  moment it builds the sampling mask, `BubbleView` carries them, and the
  overlay has separate **Sampling** (the measured ellipse) and **Centres**
  layers alongside the printed-bubble outline. The two are genuinely different
  regions - the interior is read at 62% of the printed half-axes, which on the
  repository's real sample is 22.3 px across inside a printed 36.0 px bubble -
  and an overlay that draws one while meaning the other shows an operator a
  region recognition never looked at.
- **Registered page / Original scan** views, with overlays deliberately
  restricted to the former: overlay coordinates are canonical-page pixels, and
  painting them over an uncorrected scan would be wrong everywhere while
  looking plausible.
- **Per-position field diagnostics**: every printed column of the Student ID
  and the Set Code with its own symbol, status, fill ratio, margin and
  confidence, plus the flagged questions with the same evidence. Positions are
  iterated exactly as the template declares them, so a multi-position or
  multi-character set code is reported as what it is rather than assumed to be
  a single letter.
- **A template whose zones are displaced but whose markers still register** is
  reported as needing review rather than passing. Registration succeeds, no
  sampling window leaves the page, no alignment warning fires, and every group
  reads a confident blank from bare paper - so the existing checks are blind to
  it. Caught by a rule with no tunable constant (`NO_MARKS_DETECTED`: the sheet
  registered and not one response position carried a mark) reported as *needs
  review*, never *failed*, because a genuinely blank sheet produces identical
  evidence and the message says so.
- **`ScanPreviewView`** (already used by the Scan page) gained marker-overlay
  painting and a `clicked_scene_point` signal, reused by the Calibration page
  rather than a second image-viewer component; the Scan page's own behaviour
  is unchanged.
- 93 new tests: 38 unit (`test_calibration_service.py`, plus the reported
  sampling geometry in `test_bubble_metrics.py`), 18 integration
  (`test_calibration_workflow.py`, against the real `RecognitionEngine` and
  real templates) and 37 GUI (`test_calibration_page.py`), including a
  cross-check that the on-screen question-number label always agrees with the
  number the engine itself recognised, an overlay/image alignment check across
  three zoom levels, and a shared-data-path proof that runs the engine with a
  non-default `sample_radius_ratio` and requires the displayed sampling window
  to follow it. The `qtguitesting` skill gained a `CalibrationHarness`, five
  smoke checks and ten documented screenshot scenarios - three of them the
  sampling overlay at the top, middle and bottom of the same sheet, because a
  scale error accumulates down the page and checking one region proves nothing
  about the others.

**Calibrating a template against representative scans is not the same as
validating Phase 3's real-world recognition accuracy at scale.** This phase
makes that validation safer and more systematic to carry out on whatever real
scans an operator has; it is not a substitute for validating against a broad,
independently filled corpus, which remains Phase 3's own open item.

### Added — Phase 3

- `omr_scanner.imaging.metrics`: per-bubble measurement. An elliptical interior
  sample, a local paper estimate from an annulus around each bubble, and an ink
  threshold placed halfway between that and the page's own ink level - so a
  printed option glyph inside an empty bubble is not read as a mark, and a
  uniformly faint pencil sheet is not read as blank.
- `omr_scanner.recognition`: `models.py` (the `MarkStatus`/`FieldStatus`
  vocabulary), `decide.py` (`decide_group` - one group of bubbles to one
  `Selection`), `fields.py` (`recognise_grid_zone`, `recognise_template`).
  Blank, single, multiple, uncertain and unreadable are five distinct states
  that survive to the CSV; every threshold comes from the template.
- `omr_scanner.services.recognition_service`: `recognise_scan` - one file to a
  `ScanResult` with registration status, field values, overlay geometry and an
  optional bounded-size preview.
- `omr_scanner.services.batch_processor`: `process_batch` with per-file error
  isolation, progress callbacks and cooperative cancellation. One corrupt image
  cannot end a batch.
- `omr_scanner.services.filename_manager`: `FilenameAllocator` - decides output
  names and never touches a file. Duplicates become `_a`, `_b`, ... `_z`,
  `_aa` (bijective base-26); files already in the output directory count as
  taken; an unreliable identifier gets `UNRESOLVED_001` rather than a
  fabricated name. **No scan image is ever overwritten.**
- `omr_scanner.services.scan_import`: `collect_scan_files` - PNG/JPEG/TIFF/BMP,
  folder walking, unrelated files ignored, natural sort (`scan2` before
  `scan10`).
- `omr_scanner.services.scan_export`: deterministic UTF-8 CSV with stable base
  columns and question columns ordered by the template.
- `omr_scanner.gui.scan`: the Scan workflow stage - `page.py` (controls, scan
  list, results panel), `preview.py` (zoom/pan/fit view with a non-destructive
  recognition overlay), `worker.py` (`BatchWorker`, `PreviewWorker` - `QThread`s
  that touch no widget). The package imports no `cv2`, `numpy`, `imaging` or
  `recognition`.
- `omr_scanner.imaging.synthetic`: `AnswerBubbleSpec` and marked-bubble
  rendering, Phase 3's ground-truth generator.
- `examples/templates/ece_0000_sample.omrt` and
  `scripts/build_ece0000_template.py`: a template describing the repository's
  real sample sheet, built through the real generators.
- **Configurable multicore batch recognition.** `process_batch(..., workers=N)`
  reads several sheets at once through
  `omr_scanner.services.parallel_batch.recognise_in_parallel`, a
  `ProcessPoolExecutor` using `spawn` on every platform, with one complete page
  per worker process. Recognition results come back to the main process, which
  keeps naming, copying and recording strictly in batch order behind a single
  `FilenameAllocator` - so duplicate roll numbers, the scan list and the CSV are
  identical on any number of cores, and no two workers can ever choose the same
  output file name. Progress counts completions, so the bar advances steadily
  rather than waiting on the slowest sheet.
- `omr_scanner.config.processing`: `ProcessingMode` (Automatic / Single core /
  Custom) and `ProcessingSettings`, nested into `AppConfig.processing` and
  persisted in `omrflow.config.json`. Automatic leaves one logical CPU free and
  stops at 8 workers - a cap taken from measurement (throughput peaks at the
  physical core count and falls beyond it, while memory keeps climbing by about
  95 MB per worker); no run ever starts more workers than there are scans.
- `File > Settings...` (`omr_scanner.gui.settings_dialog.SettingsDialog`): the
  Processing section, with the detected CPU-thread count and the number of
  workers the current setting will actually use. The Scan page shows the same
  thing for the list in front of you ("124 scans - 8 parallel workers") and
  reports `Completed 46 / 100 - 8 workers` while running.
- `scripts/benchmark_batch.py`: measures batch throughput at several worker
  counts on the machine it is run on, and prints what it measured. Results for
  the development machine are recorded in `docs/scan_workflow.md` §10.
- `multiprocessing.freeze_support()` in `main()`, so a frozen Windows build
  starts workers rather than recursive copies of the application.
- 979 tests, including `tests/gui/test_scan_page.py` (the ten Scan workflows),
  `tests/integration/test_sample_sheet_recognition.py` (the real scan),
  `tests/integration/test_parallel_batch.py` (real worker processes: result
  consistency at 1/2/4 workers, ordering, duplicate-roll collisions, failure
  isolation, no orphan processes) and
  `tests/gui/test_processing_settings_gui.py`.
- The `qtguitesting` skill now covers the Scan page and the Processing settings:
  a `ScanHarness`, ten smoke checks, seven screenshot scenarios and four new
  documented scenarios.

### Added — Phase 3 developer testing tools

A template-driven synthetic dataset generator and a recognition benchmark, both
inside the application and on the command line, so recognition work can be
measured rather than guessed at.

- **`omr_scanner.evaluation.test_cases`**: the test-case vocabulary.
  `TestCaseTag` (about sixty named conditions, from `BLANK_STUDENT_ID` to
  `SCANNER_STREAK`), `CaseFamily`, `MarkPlan`, `SheetCase`, and `FieldLayout`,
  which reads identifier length, symbols, set-code structure, question count
  and option labels **from the template** so that nothing downstream assumes
  six digits and four options. `SheetBuilder` derives every expected value from
  the marks it is about to draw, in one place - the rule that stops a generator
  disagreeing with itself and the benchmark blaming the engine.
- **`omr_scanner.evaluation.case_plans`**: `DatasetProfile` (Baseline,
  Recognition, Degradation, Batch, Stress, Mixed, Custom), thirteen family
  builders and `plan_dataset`. Mandatory edge cases are emitted first and
  unconditionally; a dataset too small to hold them all takes a spread across
  families rather than a prefix, so twelve sheets are twelve *kinds* of sheet.
- **`omr_scanner.evaluation.synthetic_dataset`** rewritten around the plan:
  rendering at **150 DPI derived from the template's physical page size**
  (A4 → 1240 × 1754 px), PNG and JPEG output, `manifest.json` + `manifest.csv`
  + `dataset_summary.json` beside `images/` and `ground_truth/`, streaming one
  sheet at a time, and `on_progress`/`should_cancel` hooks. `describe_template`
  and `validate_template` say what a template offers before anything is
  written.
- **`omr_scanner.imaging.synthetic`**: horizontal-stroke, vertical-stroke and
  slash mark styles; faint and physically damaged registration markers, a faint
  orientation mark; paper tint, speckle, scanner streaks and edge shadow.
- **`SheetGroundTruth`** now records the tags a sheet was generated to test, the
  marks actually drawn in each identifier and set-code column, whether either is
  deliberately borderline, the duplicate group, and the degradation parameters -
  because the marks are the fact and the expected string is an interpretation of
  them.
- **`omr_scanner.evaluation.benchmark`**: `CategoryMetrics` and
  per-test-case-category reporting, sheet-level and registration accuracy,
  duplicate-identifier metrics kept separate from recognition correctness,
  `grid_verdict` for identifier and set-code judgement including ambiguous
  columns, the `ROLL_AMBIGUITY_MISSED` / `SET_AMBIGUITY_MISSED` /
  `ORIENTATION_ERROR` categories, `BenchmarkRunConfig`, `compare_categories`,
  and the `summary.csv` / `category_metrics.csv` / `run_config.json` outputs.
- **`omr_scanner.evaluation.session`**: `BenchmarkSession` - open a dataset,
  score a set of results, write the report beside the data, and compare with
  the previous run, with no Qt anywhere in it.
- **Tools > Developer / Testing** in the main window: *Generate Synthetic Test
  Dataset* and *Run Recognition Benchmark*.
- **`omr_scanner.gui.devtools`**: the generation dialog, a cancellable
  generation thread reusing `BatchProgressTracker` for its ETA, the progress and
  summary dialogs, and the benchmark results dialog (summary, per-test-case
  table, every disagreement, failing scans - double-click to open one in the
  scan list with its overlay).
- **Benchmark mode in the existing Scan page** rather than a second processing
  window: a banner, the dataset's scans in the ordinary list, and the same
  *Process All* button, settings and worker pool. Scoring happens automatically
  when a run ends.
- CLI: `make_dataset` gained `--families`, `--format`, `--jpeg-quality`,
  `--dpi`, `--describe` and progress output; `benchmark_recognition` gained
  `--categories`, the category table, duplicate reporting and per-category
  baseline comparison.
- Tests: 48 dataset/generator tests, 26 benchmark-harness tests and 20
  pytest-qt developer-tool tests; the `qtguitesting` smoke suite grew to 27
  checks, including generating a dataset from the real template and
  benchmarking it through the Scan page.

**Synthetic results measure regression consistency and controlled edge-case
handling. They do not establish real-world recognition accuracy**, and every
report the tools write says so in the file itself. One finding recorded rather
than tuned away: with the default 0.55 fill threshold, dot-shaped marks measure
a mean fill ratio of 0.16 and read as blank, while strokes and slashes measure
0.51-0.54 and are flagged as uncertain, against 0.93 for a filled bubble.

### Added — Phase 3 large-batch progress

Batch processing is built for examination-scale runs (10,000+ scripts), and
the Scan page now reports on one without changing shape as it grows.

- **`omr_scanner.services.batch_progress`**: `BatchProgressTracker`,
  `ProgressSnapshot`, `JobStatus`, `BatchState` and the shared duration/rate/
  count formatters. Thread-safe, headless, and unit-testable with an injected
  clock - the estimator has 57 tests including a 10,000-job simulation that
  runs in under a second.
- **A progress panel on the Scan page**: a bar driven by *finished* sheets
  (a failed scan advances it, so a batch of damaged files cannot stall it),
  completed/total counts with thousands separators, percentage to one decimal,
  elapsed time on a monotonic clock, a smoothed estimate of the time
  remaining, live throughput, an estimated finishing time, and
  successful/needs-review/failed tallies in words.
- **A smoothed ETA** from an exponential moving average over half-second
  throughput samples, with a warm-up (about ten completed sheets, or a few
  seconds of steady measurement) during which it says `Calculating...` rather
  than extrapolating from one sample. A stall makes the estimate grow rather
  than freeze; cancellation withdraws it; completion sets it to zero.
- **`Preparing batch...`** as a distinct state, so the moments before the
  first sheet do not read as a stalled bar at 0 / 10,000.
- **Throttled repainting**: the tracker counts every completion, while the
  window pulls a snapshot about five times a second. A machine finishing fifty
  sheets a second no longer asks Qt to repaint fifty times a second. Scan-list
  rows are buffered the same way and located through a path index, which turns
  a long batch from quadratic into linear work in the GUI thread.
- **Cancellation that responds immediately**: the button disables itself and
  the panel says `Cancelling batch processing...` before any worker notices;
  no new sheet starts; sheets already inside a worker finish cleanly rather
  than being killed mid-write; everything already read is kept; and the final
  state reports both halves ("6,342 processed · 3,658 not processed").
- **A completion summary**: total duration and average speed, with the bar at
  exactly 100% only when every sheet has reached a terminal state.
- `BatchProgress` gained an `outcome` field, so a progress display can tally
  successes, reviews and failures *as sheets finish* - which on a multicore
  run is earlier than results are released in batch order.

### Changed

- A batch run no longer keeps the per-bubble evidence on results it retains
  for export: on the repository's 100-question sample that is 6.7 KB per sheet
  instead of 61.6 KB, or about **68 MB rather than 631 MB** across ten
  thousand sheets. Nothing the page shows used it - the overlay comes from the
  preview worker's own result - and switching diagnostics on keeps it, because
  the diagnostic images are drawn from it.
- `RecognitionOptions.keep_bubble_measurements=False` now omits the bubble
  records entirely rather than blanking their fields, which is where the
  memory actually is. No decision changes either way.

### Added — Phase 3 architectural hardening

Phase 3 turned into a *replaceable* recognition subsystem, so that Phases 4 and
5 can be built against it now and a future Recognition Engine v2 can replace it
without rewriting them. Details in `docs/recognition_engine.md`.

- **A stable recognition API.**
  `omr_scanner.services.recognition_service.RecognitionEngine` -
  `engine.process(image_path, template) -> ScanResult` - is the single entry
  point; no caller above it needs to know about thresholds, contours or
  homographies. `recognise_scan()` remains supported and delegates to the same
  pipeline.
- **`omr_scanner.services.recognition_models`**: the result vocabulary, split
  out of the engine so a consumer can depend on the *shape* of a result without
  importing the engine that fills it. Adds to `ScanResult`: the engine name and
  version, the template's id/name/format version, a UTC timestamp,
  machine-readable `status_codes`, per-stage `timings` and a `ScanQuality`
  record (marker scores, reprojection error, corrected rotation and skew,
  perspective strength, brightness, contrast, sharpness). `to_dict()` /
  `from_dict()` serialise a result to JSON, refusing a newer schema version
  rather than misreading it.
- **Raw per-bubble measurements on every result.** `BubbleView` now carries
  `mean_darkness`, `contrast`, `paper_level`, the `ink_threshold` it was
  compared against, `sample_pixels`, `usable` and its `rank` within its group -
  so a future recalibration can ask "what would a threshold of 0.6 have
  decided?" arithmetically, without re-reading a single image.
- **`StatusCode`**: `OK`, `LOW_CONFIDENCE`, `BLANK`, `MULTIPLE_MARK`,
  `AMBIGUOUS`, `ALIGNMENT_WARNING`, `ALIGNMENT_FAILED`, `ORIENTATION_FAILED`,
  `MARKER_NOT_FOUND`, `ROLL_UNREADABLE`, `SET_UNREADABLE`, `INVALID_TEMPLATE`,
  `IMAGE_LOAD_ERROR`, `PROCESSING_ERROR` - derived from the values, so they can
  never disagree with the result they describe. A consumer branches on these,
  never on an English message.
- **`omr_scanner.services.recognition_settings`**: `RecognitionOptions` and
  `DiagnosticsOptions` - engine-level options (sampling, preview, what evidence
  to keep, debug output) in one immutable, picklable object. Thresholds stay in
  the template, where they belong.
- **`omr_scanner.services.recognition_diagnostics`**: a headless annotated
  overlay (`render_overlay`) and the staged debug dump per scan - the original,
  the rectified page, the decision overlay, a measurement overlay and the
  result as JSON. Off by default; a write failure costs the diagnostics, never
  the scan; and a test asserts that producing them changes no recognised value.
  Switchable from *File > Settings > Diagnostics* or `--diagnostics`.
- **Headless tools.** `python -m omr_scanner.tools.recognise` reads one scan or
  a folder with no GUI, writing JSON results, overlays or diagnostics, on one
  or many workers. A test proves it: recognition runs in a fresh interpreter
  where no `PySide6` module is ever imported.
- **`omr_scanner.evaluation`**, a QA layer *above* services: the ground-truth
  schema shared by synthetic and real datasets
  (`SheetGroundTruth`, `DatasetManifest`), a reproducible synthetic dataset
  generator, and a benchmark harness.
- **Synthetic dataset generator** (`python -m omr_scanner.tools.make_dataset`):
  renders labelled sheets *from a real template*, with five difficulty profiles
  and controlled defects - mark styles (fill, ring, tick, cross, scribble, dot,
  each with its own coverage, darkness, offset and size), geometry, exposure,
  blur, noise, JPEG artefacts, and structural damage such as a missing marker
  or a cropped page. Ground truth is written beside every image and records the
  defects injected; a seed reproduces a dataset byte for byte.
- **Benchmark harness**
  (`python -m omr_scanner.tools.benchmark_recognition`): scores recognition
  against ground truth, classifies every disagreement (`FALSE_MARK`,
  `FALSE_BLANK`, `WRONG_OPTION`, `MISSED_MULTIPLE_MARK`,
  `FALSE_MULTIPLE_MARK`, `ROLL_ERROR`, `SET_ERROR`, `ALIGNMENT_ERROR`,
  `PROCESSING_FAILURE`), writes `summary.json` and `errors.csv`, compares a run
  against a stored baseline, and offers a fill-threshold sweep that reports
  without changing anything.
- **Stored result fixtures** in `tests/fixtures/recognition/`: eleven scenarios
  a later phase must handle, loadable with no recognition engine present, built
  by `scripts/build_recognition_fixtures.py`.
- **`local_test_data/`**: the documented, git-ignored home for a real
  validation corpus, using the same layout and schema as a synthetic dataset so
  one benchmark command serves both. Nothing is uploaded, ever.
- 227 further tests (1,935 in the repository), covering the result contract,
  the fixtures, the evaluation harness, the engine as a subsystem, the dataset
  generator and the three command line tools.

### Fixed — Phase 3

- `ScanPage` no longer starts a second `PreviewWorker` for a scan already being
  rendered. Finishing a batch re-selects the current row, so a user who also
  clicked that row got two workers, and the late one re-applied the preview -
  resetting a zoom they had just set.
- The Settings dialog no longer reverts other preferences. It returned a whole
  `AppConfig` rebuilt from the snapshot taken when it opened, so accepting it
  discarded anything that had changed meanwhile - a project opened while the
  dialog was up disappeared from the recent list. It now returns only its own
  `ProcessingSettings`, which the main window merges into the current
  configuration.

### Added — Phase 2

- `omr_scanner.gui.template_designer`: the interactive template designer.
  - `page.py` - the workflow page: file actions with dirty tracking, marker
    detection, region creation, undo/redo, keyboard shortcuts, validation.
  - `canvas.py` - zoomable/pannable `QGraphicsView` canvas: wheel zoom, pan,
    an alignment grid overlay, rubber-band region drawing, per-bubble
    fine-tune dots.
  - `items.py` - draggable/resizable region overlays with four visual states
    (normal, auto-detected, manually overridden, missing) and individually
    draggable bubble dots.
  - `state.py` - `DesignerState`: the template plus its undo/redo history plus
    session-only marker detection provenance (never persisted).
  - `history.py` - `SnapshotHistory`, a generic undo/redo stack over immutable
    snapshots.
  - `coordinates.py` - `CoordinateMapper` and `snap`, the one place
    pixel/normalised arithmetic happens.
  - `region_list.py`, `properties_panel.py`, `dialogs.py` - the region list,
    the numeric geometry editor, and one dialog per region kind plus New
    Template and the validation report.
- `omr_scanner.domain.template_authoring`: pure region-generation and
  designer-validation functions - `generate_character_grid_zone`,
  `generate_question_columns` (one zone per printed column),
  `generate_ignored_zone`, `translate_zone`, `resize_zone`,
  `build_blank_template`, `validate_template_for_designer`.
- `omr_scanner.services.marker_detection_service`: the seam that lets the
  designer decode images and run Phase 1's marker detector without the `gui`
  layer ever importing `cv2`/`numpy`. Detection is scored per corner
  independently (never Phase 1's stricter all-or-nothing four-corner
  assignment), so a sheet with three good corners and one damaged one is
  reported accurately rather than rejected outright.
- `OmrTemplate.reference_image`: one additive, optional field recording the
  reference sheet's path relative to the `.omrt` file, so a template can be
  reopened for further editing. Does not bump `format_version`; documents
  written before Phase 2 load unchanged.
- `examples/templates/100_question_4_choice_example.omrt`: a 7-digit student
  ID, A-D question set and 100 questions in 4 columns (474 bubbles total),
  built and validated through the real generator functions, with its
  reference image shipped alongside it.
- 146 new tests (729 total): region generation and validation, coordinate
  conversion, undo/redo, designer state mutations, the marker detection
  service against synthetic sheets, and GUI smoke tests covering the full
  create-detect-draw-adjust-undo-validate-save-reload workflow.
- Set Code regions now support both **enumerated values** (arbitrary
  multi-character tokens - `"10,11,12"`, `"01,02,03"` - never split into
  digits or coerced to numbers) and a **positional code** mode (each code
  position its own bubble column); both were already representable by the
  existing `set_code` field, so this is a dialog-only addition with no schema
  change.
- Question Answer columns are independently positionable: the Questions
  dialog exposes bubble width/height, choice spacing, question row spacing
  and Column Gap as explicit image-pixel fields (defaulting to reproduce the
  original auto-fit geometry exactly), with a live dashed preview on the
  canvas; `QuestionBlockFieldDefinition.group_id` (additive, optional) ties
  sibling columns of one Question Region together.
- **Create Question Column Array** and **Distribute Columns Evenly** toolbar
  actions (`omr_scanner.domain.template_authoring.generate_column_array`,
  `.distribute_columns_evenly`, `.measure_column_gap`): generate a full set of
  calibrated columns from one reference column, or re-space a group's
  intermediate columns evenly between a fixed first and last - each applying
  as one undo step (`DesignerState.apply_zones`).
- Canvas panning via middle-button drag (always) and right-button drag (past
  Qt's own standard drag-distance threshold, leaving a plain right-click free
  for a future context menu), in addition to the existing space+left-drag;
  neither ever reaches a region item, so it cannot be mistaken for selecting
  or moving one.
- `docs/testing/question_layout_manual_test.md`: manual verification steps
  for the above.
- Official OMR Flow branding: the wordmark logo (`omr_scanner.gui.branding`)
  anchored at the bottom of the left navigation sidebar, below the workflow
  step list and horizontally centred - not a dedicated header row, so no
  vertical space is reserved above the page content - a multi-resolution
  application/window icon derived from it (`gui/resources/branding/icon.ico`,
  generated by `scripts/generate_branding_assets.py`), and a small
  developer-credit footer with a clickable link to https://www.sajid.bd
  opened via `QDesktopServices` in the system browser. Purely presentational;
  no template, scan or workflow behaviour changed.

### Changed

- `omr_scanner.gui.pages.base_page.WorkflowPage` gained an `expand: bool`
  constructor flag so a page's body can fill all available vertical space
  instead of shrinking to its content with a trailing spacer - needed for the
  designer's full-size canvas. Default behaviour for every other page is
  unchanged.
- `omr_scanner.gui.pages.catalog.WorkflowPageSpec` gained an explicit
  `implemented` field, decoupling "is this a working page" from "which phase's
  number is shown to the user" - the Template stage keeps `phase=2` for
  documentation purposes while `implemented=True`.
- The Template workflow stage is a real page instead of a placeholder.
- `docs/TEMPLATE_FORMAT.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT_GUIDE.md`
  updated for the above.
- `pyproject.toml`: the Ruff `pep8-naming` Qt-override allowlist extended to
  cover the mouse/hover/wheel/key/drag event handlers the designer's canvas
  and graphics items implement.

### Fixed

Template Designer correction pass. Root causes and the reasoning behind each
correction are recorded in `docs/development/template_gui_fix_diagnosis.md`.

- **A Question Region changed size when its column count changed.** The drawn
  rectangle was never a container: `generate_question_columns`' explicit-pitch
  branch read only `bounds.x`/`bounds.y` and derived each strip's size from the
  pitch, so the block's outer extent was proportional to the column count.
  `ColumnLayoutMode` now names the two behaviours - `FIT_CONTAINER` (the
  default; the strips tile the user's rectangle exactly, for any column count,
  gap, pitch or bubble size) and `FROM_PITCH` (which `generate_column_array`
  opts into, because "make N more like this calibrated column" is meant to
  extend past it).
- **Resizing a region threw it to the scene's top-left corner.**
  `RegionHandleItem` captured its press rectangle in *item-local* coordinates,
  added a *scene-space* mouse delta to it, and passed the result to
  `set_scene_rect`, which reads scene coordinates - so the first mouse-move set
  the item's position to the drag delta alone. The gesture is now computed
  entirely in scene coordinates, and the constructor enforces the class's
  invariant (`pos()` holds the position, `rect()` holds the size, never both).
  Resize anchoring falls out of the arithmetic with no special cases.
- **Bubble size could not be adjusted, and changing it altered nothing.**
  `RegionHandleItem.paint` drew every preview bubble at a hard-coded 3 px radius
  and `RegionSpec` carried no size at all, so no value a user could type would
  change what was drawn; separately, three of the four bubble region kinds had
  no bubble-sizing control.
- **The Template page spent a sixth of the window's height above the canvas.**
  `WorkflowPage` unconditionally gave every page a word-wrapped summary row and
  24 px margins - right for a placeholder page whose content *is* explanatory
  text, wrong for one whose body is a full-size editor.

### Added

- **Orientation-mark detection inside a user-drawn region.**
  `omr_scanner.imaging.orientation_marker` crops the rectangle, thresholds it on
  its own statistics and scores each dark shape on darkness, solidity, aspect
  ratio, size and position, with dash-shaped defaults rather than the
  registration-square criteria (which reject a 2:1 dash on aspect ratio alone).
  Reached from the designer through the toolbar's **Orientation** action and
  `services.detect_orientation_marker_in_region`. Every reported coordinate is
  in full-image pixels; a mark wholly inside the rectangle is never rejected for
  being inside it. Previously there was no orientation detection in the designer
  at all - `imaging.orientation` answers a different question (*which way up* a
  scan was fed, from four detected corners and a homography) and cannot answer
  this one. Setting `OMRFLOW_ORIENTATION_DEBUG_DIR` writes an annotated overlay
  of the search region, every candidate and each rejection reason.
- **A template-wide bubble radius.** `OmrTemplate.default_bubble_radius` -
  additive and optional, normalised to the page width, no `format_version` bump
  - with `default_bubble_size` deriving the width/height a grid stores from the
  page aspect ratio, so a bubble circular in pixels stays circular. Edited from
  toolbar row 2 in reference-image pixels, with a per-region override in the
  properties panel. A region inherits while its stored size matches what the
  default produces, so the relationship survives save and reload without a new
  schema field. `set_zone_bubble_size` changes only `grid.bubble_size`: every
  bubble centre, every hand-placed override and the parent rectangle are
  preserved exactly.
- `place_grid_in_bounds` - the counterpart to `fit_grid_to_bounds`: positions a
  lattice of a *given* pitch inside a fixed rectangle, rejecting one that does
  not fit rather than silently enlarging the region.
- A "Fit bubble spacing to the region" option in the Question Block dialog (on
  by default), so changing the column count reflows the bubbles across the
  container instead of leaving them at a pitch a different count needed.
- `.claude/skills/qtguitesting/` - a repository-local Claude Code skill for
  OMRFlow's Qt GUI: the testing hierarchy and workflow, a Qt coordinate-system
  reference, ten real-image scenarios, and four scripts
  (`run_gui_smoke_tests.py`, `capture_gui_states.py`, `dump_gui_geometry.py`,
  `compare_gui_images.py`). Documented in `docs/DEVELOPMENT_GUIDE.md`.
- Regression tests for every bug above:
  `tests/unit/test_question_region_container.py`,
  `tests/gui/test_template_designer_region_geometry.py`,
  `tests/gui/test_template_designer_bubble_and_layout.py`,
  `tests/integration/test_orientation_marker_detection.py` (seven synthetic
  scenarios plus six search-rectangle shapes on `examples/ECE-0000.png`), and
  `tests/unit/test_qtguitesting_skill.py`.

### Changed (correction pass)

- The Template toolbar is two rows - file/undo/detection/validation above,
  region tools/bubble radius/zoom/grid below. One row pushed most of the region
  tools into Qt's overflow menu on any window narrower than about 1400 px.
  `TemplateDesignerPage.toolbar_actions()` returns both rows' actions.
- `WorkflowPage` gained `show_summary` and `compact`; the designer passes both,
  keeping the stage description as the title's tooltip. Every other page is
  unchanged.
- `DesignerState.apply_template` replaces the whole document in one history
  entry, so a template-level change that also touches zones (a radius change) is
  one undo step.
- `test-output/` is git-ignored - generated screenshots and geometry dumps, never
  committed baselines.

### Known limitations

- No snap-to-grid while dragging yet (the pure function exists and is tested;
  wiring it into interactive dragging is deferred).
- Distribute is per Question-Region group only (Distribute Columns Evenly);
  no general align/distribute tool for arbitrary multi-selected regions.
- Individual-bubble override reset is per-region, not per-bubble.
- The Question Block dialog exposes one bubble *radius* rather than independent
  width and height, so a deliberately elliptical bubble cannot be authored from
  that dialog. The `.omrt` model still stores the two axes independently and a
  template authored elsewhere round-trips unchanged.
- The designer has been exercised against the repository's real sample sheet
  (`examples/ECE-0000.png`) but has not been used to process a real examination.

---

Phase 1 - OMR geometry and alignment engine. An arbitrary scan of a sheet can
now be normalised into the canonical page its template describes. Bubble
recognition still does not exist; this build must not be used for examination
processing.

### Added

- `omr_scanner.imaging`: the geometric normalisation engine, entry point
  `align_sheet(image, config=...)`.
  - `preprocessing` - validation, grayscale, working-resolution downscale,
    denoising, and Otsu or adaptive thresholding.
  - `marker_detection` - contour measurement (centroid, area ratio, aspect
    ratio, rectangularity, solidity, interior ink), combined shape filtering
    with a named reason per rejection, per-corner scoring and an exhaustive
    one-to-one assignment of candidates to the four page corners.
  - `orientation` - resolves 0/90/180/270 degree page orientation by rectifying
    the orientation mark's expected window out of the scan under each of the
    four hypotheses, after pruning those whose geometry is implausible.
  - `geometry` - point ordering, quadrilateral validation, homography and its
    inverse, as pure functions.
  - `alignment` - the orchestrator, quality metrics and warnings.
  - `diagnostics` - optional detection overlay, rectified-page preview and
    textual summary; inert by construction.
  - `synthetic` - synthetic canonical sheets with interior control points, and a
    seeded distortion engine (rotation, scale, translation, perspective,
    brightness, illumination gradient, blur, noise, JPEG, cropping).
  - `config` - every tunable value, named, documented, validated and defaulted
    in one place.
  - `models` - explicit runtime types for candidates, detections, orientation,
    metrics, diagnostics and the result.
- `omr_scanner.services.alignment_service`: converts an `OmrTemplate` into an
  `AlignmentConfig`, and reads and writes image files (through
  `fromfile`/`imdecode`, so non-ASCII paths work on Windows).
- `omr_scanner.tools`: developer command line utilities `align_image` and
  `make_test_sheet`.
- Six `ImagingError` subclasses, each carrying a stable machine-readable `code`:
  `ImageValidationError`, `MarkerDetectionError`, `InsufficientMarkersError`,
  `AmbiguousMarkerError`, `OrientationDetectionError`,
  `InvalidPageGeometryError`, `AlignmentTransformError`.
- 455 new tests (583 total), including a 40-case synthetic distortion suite
  measured against nine interior control points per case.

### Changed

- `docs/IMAGE_PROCESSING.md` rewritten to describe the implemented engine, its
  measured accuracy and its measured degradation limits.
- `docs/TESTING.md` documents the synthetic generator, the distortion engine,
  the accuracy metric and the tolerances.
- `docs/ARCHITECTURE.md` records the `tools` layer, the expanded error
  hierarchy and the template/imaging seam.
- `tests/unit/test_architecture.py` enforces the layering rules for `tools`.

### Known limitations

- No real-world validation: every accuracy number is synthetic.
- Arbitrary rotation is corrected up to ±15 degrees, plus exact quarter turns.
  Beyond that the corner search regions must be widened.
- A missing registration marker fails the sheet; no corner is ever
  extrapolated.

## [0.1.0.dev0] - 2026-09-15

Phase 0 - architecture and repository foundation. The application starts and
manages projects; no OMR processing exists yet.

### Added

- Packaging (`pyproject.toml`) with pinned tool configuration for pytest, Ruff
  and mypy, and the `omrflow` console entry point.
- Layered package skeleton under `src/omr_scanner`: `domain`, `database`,
  `services`, `gui`, `config`, `utils`, plus documented-but-empty `imaging`,
  `recognition` and `reporting` packages.
- Application exception hierarchy (`omr_scanner.errors`) with separate technical
  and user-facing messages.
- Per-user application configuration (`AppConfig`) with recent-project tracking,
  stored in the platform's standard configuration directory.
- Structured logging: an application log plus a per-project log attached while a
  project is open.
- Project model and service: create, validate, open and close a project
  directory containing `project.json`, `database.sqlite` and the standard
  sub-directories.
- SQLite/SQLAlchemy 2.x foundation with a forward-only migration ledger
  (schema version 1: `schema_migration`, `project_setting`).
- Versioned `.omrt` template document model (page geometry, registration and
  orientation markers, zones, fields, bubble grids, recognition settings) with
  load/save support and an illustrative example in `resources/templates`.
- Minimal PySide6 shell: main window, workflow navigation with eight stages,
  status bar, File and Help menus, and honest "not implemented yet" placeholder
  pages naming the phase that will implement each stage.
- Test suite (128 tests) covering configuration, project lifecycle, database
  initialisation/migration, template validation, GUI startup, and an executable
  check of the architectural layering rules.
- Documentation set: architecture, development guide, data model, template
  format, image-processing plan, testing strategy, user guide and four ADRs.

[Unreleased]: https://github.com/sajidbuet/OMRflow/compare/v0.1.0.dev0...HEAD
[0.1.0.dev0]: https://github.com/sajidbuet/OMRflow/releases/tag/v0.1.0.dev0
