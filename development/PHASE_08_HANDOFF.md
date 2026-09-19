# Phase 8 handoff — Answer-Key & Scoring Engine

**Implemented:** 2026-09-19
**Version:** 0.1.0.dev0
**Environment verified on:** Windows 11, Python 3.12.7, PySide6 6.11.2, OpenCV
5.0.0, NumPy 2.5.3, SQLAlchemy 2.x, 16 logical CPUs

Read alongside `docs/scoring.md` (the operator-facing description),
`docs/DATA_MODEL.md` (the three new tables and the migration) and
`docs/ARCHITECTURE.md`.

**Phase 8 produces marks that can be *defended*, not marks that are *right*.**
It records the rule, the key revision and the answers a mark was computed from,
so the mark can be reproduced and argued about. Whether the key is correct, and
whether recognition read the paper correctly, remain Phase 4's and Phase 6's
problems.

---

## 1. What already existed

| Capability | Where | Verdict |
|---|---|---|
| Per-question recognition with explicit blank/multiple states | `recognition/`, `AnswerView` | Read, never modified |
| The effective value of a reviewed field | `review_store` (Phase 6) | Extended by two functions (§4) |
| Append-only audit ledger with `entity_type` | `audit_event` (Phase 6-7) | Available; see §9 |
| Who a script belongs to, and whether they sat the paper | `reconciliation_store` (Phase 7) | Read as the eligibility gate |
| Multi-character set codes | Everywhere since Phase 3 | Honoured; never assumed to be one character |
| Template question numbering and answer labels | `QuestionBlockFieldDefinition` | The single source of the question count |
| Recognition engine for a single sheet | `RecognitionEngine.process` | Reused for solution sheets |
| Forward-only migration framework | `database/migrations.py` | Followed exactly |
| Background-worker pattern (`QThread` + one signal) | `gui/scan`, `gui/review`, `gui/attendance` | Reused |
| Named-reviewer requirement | `review_store`, `reconciliation_store` | Pattern followed for key verification |

**Nothing was rewritten.** No second OMR engine for solution sheets, no second
audit mechanism, no second definition of what a question is.

## 2. What was missing

1. No way to say what the correct answers *are*.
2. No marking rules, and nowhere to keep them.
3. No `CandidateResult`, so nothing to be reproducible.
4. No exact arithmetic anywhere - every number in the application before this
   phase was a measurement, where a float is the right type.

## 3. What was built

### `domain/scoring.py` — the arithmetic

Pure. `BLANK`/`MULTIPLE`, `AnswerKey`, `AnswerKeyStatus`, `AnswerKeySource`,
`ScoringPolicy`, `NegativeMarking`, `QuestionOutcome`, `QuestionScore`,
`ScoreBreakdown`, `ResultStatus`, `BlockReason`, `ScoringBlock`, `StaleReason`,
`ResultCounts`, plus `score_answers`, `canonical_answer_string`, `format_mark`
and `parse_mark`.

`score_answers` is a function of its arguments and nothing else - no clock, no
configuration lookup, no database, no locale. That is what makes "recompute,
never patch" testable rather than a slogan.

### `services/answer_key.py` — reading a key

`plan_for` (what the template says), `read_key` (typed or pasted),
`key_from_scan` (a solution sheet, through the existing engine),
`parse_wrong_questions`, `normalise_key_text`. Pure.

### `services/scoring.py` — eligibility

`build_candidate_answers`, `working_script`, `score_candidate`. Knows about
candidates, scripts and Phases 5-7; produces either a mark or a list of blocks.
Pure.

### `services/scoring_store.py` — persistence

Key revisions and verification, policy revisions, `score_batch`,
`stale_reasons_for`, `breakdown_for`, and the read side.

### Migration 5

`answer_key_revision`, `scoring_policy_revision`, `candidate_result`. Purely
additive.

### `gui/answer_key/` and `gui/results/`

The two stages, plus `ScoringPolicyDialog` and `ScoringWorker`.

### Elsewhere

- `review_store.effective_set_codes` and `review_store.effective_answers` —
  §4.
- `batch_store.results_by_scan` — a scan id to its stored result, because
  pairing two lists by position is a silent mismatch waiting for the first
  batch with an undecodable row.
- `main_window`: page wiring, `broadcast_template`, reviewer broadcast,
  `shutdown` in `closeEvent`.
- `catalog.py`: both stages are no longer placeholders.

## 4. Why two more Phase 6 bridges exist

Scoring needs the candidate's *set* and their *answers* as they stand after
review. Reading `BatchScan.set_code_value` or `ScanResult.answers` would use
what the **machine** read and silently ignore every correction a reviewer made -
the Phase 6 defect in a third costume, and here it would mark a paper against
the wrong set's key.

So `review_store` answers both questions, as it already answered the identifier
one for Phase 7:

- `effective_set_codes(database, batch_id)` — the set after review, with an
  `unresolved` flag;
- `effective_answers(database, batch_id, template)` — the decided answers and
  **the question numbers still awaiting review**.

That second list is why `BlockReason.UNRESOLVED_ANSWERS` exists. An unread
answer is not a blank, and marking it as one would award the blank mark for a
question the candidate may well have answered.

## 5. Why `Fraction` rather than `Decimal`

Both are exact where they are exact, but `Decimal` is exact only for values
with a terminating decimal expansion. `1/3` is not one of them, and the
1-per-3 negative-marking mode is a published examination rule.

`Decimal("1")/3` is `0.333...` truncated at the context precision, so a hundred
questions accumulate an error that depends on the order they were added.
`Fraction(1, 3)` is one third, and three hundred of them sum to exactly 100 - a
test asserts precisely that.

Marks enter as text and are converted with `Fraction(Decimal(text))`, which is
exact. They never pass through `float`: `Fraction(0.1)` is not one tenth.
Formatting to two places is presentation, done once at the end, and never feeds
back.

## 6. Why the per-question breakdown is not stored

A hundred questions across ten thousand candidates is a million rows.

The brief allows "per-question breakdown **or enough deterministic data to
recreate it**", and the second is strictly better here: a stored breakdown is a
second copy of the truth that can contradict the total it belongs to, and
keeping the two in step is work with no upside. `breakdown_for` reruns the same
pure function over the same stored inputs, so a detail view and a mark cannot
disagree - and a test asserts they do not.

It also means a **stale** result explains the mark it actually has, not the one
the current key would give, because the breakdown is regenerated from *its*
recorded revisions.

## 7. Why a policy revision is not created per save

Saving an unchanged policy is a no-op that returns the current revision.

Creating one per click would make every result in the project stale every time
somebody opened the dialog and pressed Save, and "stale" has to mean something
for an operator to act on it. Only a change to a score-affecting field creates
a revision, and there is a test per field asserting which fields those are.

## 8. Why a cancelled run writes nothing

`score_batch` accumulates results in memory and writes them in one transaction
at the end. Cancelling discards them.

The alternative - committing what was computed - leaves a batch half marked
under one policy and half under another, with no marker saying which is which.
The brief asks for a coherent state; discarding is the only state that is
coherent *and* honest.

## 9. What was **not** built: a scoring audit trail

`audit_event` takes any `entity_type`, and Phase 7's handoff suggested a score
override would belong there.

**No score override exists, so nothing writes to it from this phase.** A mark
is never edited - it is recomputed - so there is no human decision about a
*mark* to audit. The decisions that *do* affect a mark are already audited
where they are made: an answer correction by Phase 6, a script assignment or
attendance override by Phase 7. Key verification records `verified_by` and
`verified_at` on the revision itself, and the policy records `created_by`.

Adding a ledger entry that said "a batch was scored" would be a log line, not
provenance - the result row already carries everything needed to reproduce it.
If a later phase introduces a genuine manual mark override, `audit_event` is
where it belongs, and `entity_type='result'` is free.

## 10. Testing

| Suite | Count | What it holds |
|---|---|---|
| `tests/unit/test_scoring.py` | 71 | The arithmetic as a table: the brief's hand-calculated cases, every negative-marking mode, wrong-question precedence, clamping, boundaries, the canonical string |
| `tests/unit/test_answer_key.py` | 43 | Reading a key, every refusal and its message, and reading one off a solution sheet |
| `tests/unit/test_scoring_store.py` | 55 | Revisions, verification, scoring, staleness, **recomputation never patching**, persistence, migration |
| `tests/integration/test_scoring_workflow.py` | 22 | The acceptance scenario with **real recognition over real sheets**, Phase 6 integration, reproducibility |
| `tests/gui/test_scoring_pages.py` | 56 | Both pages and the policy dialog, with a real project |
| **Total new** | **247** | |

Raised to **314** by the audit in §11a; the counts above are as first written.

Plus 5 new `qtguitesting` smoke checks (**48/48** passing).

**Full suite: 2,937 passed, 1 skipped** (baseline before this phase: 2,690).
`ruff check .` clean. `mypy` clean (130 source files).

### The assertions that matter most

```python
assert breakdown.final_score == Fraction(14) - Fraction(1, 3)   # exact, not 13.67
assert result.answer_key_revision == 2                          # which key
assert after.final_score == expected_from_inputs                # after corrupting the stored one
assert second.outcome is QuestionOutcome.WRONG_QUESTION         # whatever was marked
```

The third is the one the phase turns on. A test deliberately writes `999` into
a stored score, leaves every authoritative input untouched, rescores, and
asserts the mark is re-derived rather than adjusted.

## 11. What is not done

- **No examination has been marked with it.** Everything is synthetic or uses
  the repository's one real sheet. No real cohort, no real answer key, no
  operator checking a mark against a paper in front of them.
- **No examination-scale run.** Scoring is arithmetic and fast; the largest
  *real* batch in this project remains 48 scans.
- **A key is not checked for correctness.** Verification records who looked at
  it - a much weaker claim, and deliberately so, because nothing in software
  can do better.
- **Scoring is per batch and per roster.** A cohort split across two batches is
  marked twice, with no combined view.
- **No result export.** Reports are Phase 9.
- **One policy per paper.** Section-wise or per-question weights are not
  supported; the model would take a policy attached to a question range
  without changing the result shape.
- **No manual mark override**, by design (§9).
- Windows only, as for every phase since 3.

## 11a. Independent audit, and what it changed

After the above was written, Phase 8 was put through an adversarial audit
against the brief: every requirement mapped to code, behaviour verified rather
than inferred from a class or a test name. The baseline suite was green
(2,937 passed, 1 skipped) and stayed green, so nothing below was found by a
failing test - which is the point.

Nine defects were found and repaired. Five mattered.

### Silent, and the worst of them: a key numbered for another paper

`score_answers` took the printed number of the first question from its
*caller* and never checked it against the key's own `first_question`. A key's
`wrong_questions` are printed question numbers, so a key written when the paper
started at question 1, used against a plan numbered from 101, lines its answers
up perfectly and **withdraws nothing at all**. Every candidate loses credit the
examiners granted them, every total is internally consistent, and nothing looks
wrong anywhere.

It now refuses, and `score_candidate` turns the refusal into
`BlockReason.KEY_NUMBERING_MISMATCH` so an operator gets a sentence rather than
a traceback.

### A contradiction filed as a settled outcome

`score_candidate` returned `ABSENT` for anyone whose effective attendance was
absent - including `ABSENT_WITH_SCRIPT`, where the attendance record says one
thing and a script carrying that candidate's ID says another. Phase 7 exists to
decide which is wrong. Recording it as "Absent" answered the question by
ignoring it, and left a physical script unmarked with no row anywhere
complaining. Only `ABSENT_CONFIRMED` is an outcome now; any exception on an
absent candidate blocks.

### A key that never reached the stage that uses it

`AnswerKeyPage.key_saved` and `.key_verified` were emitted into the void - no
`connect` anywhere - as was `ResultsPage.policy_changed`, and
`offer_set_codes` had no caller at all despite a docstring saying the main
window called it. So verifying a key left the Results stage still reporting
*"Verified answer keys: none"*, with results that had just gone stale still
presented as current, until something unrelated happened to rebuild the table.
The Results stage also learned its batch only when a project was *opened*, so a
batch scanned during the session was invisible to it. All four are wired
through `MainWindow` now.

### A template edit that penalised the candidates

The same family as the numbering one, and reachable the same way. A sheet read
when the paper offered `A/B/C/D/E`, marked against a template since cut to
`A/B/C/D`, holds an `E` that `canonical_answer_string` cannot name. It becomes
`?` - deliberately, because calling it blank would credit a candidate for a
question they answered - and a `?` attracts the **multiple deduction**. So a
candidate lost marks for an edit somebody else made to the template, with a
`?` in the detail view that the sheet does not show.

`unnameable_responses` now finds those values and the sheet blocks, asking to
be read again against the template it is being marked against. The docstring in
`canonical_answer_string` claiming "the caller is expected to have blocked such
a sheet already" was, until this, describing a caller that did not exist.

### A rule that was stored, previewed, and ignored

`effective_multiple_penalty` honoured `multiple_penalty` only under the fixed
mode. The dialog could nevertheless save a policy with a separate multiple
deduction *and* a 1-per-3 mode - switch mode with the checkbox already
cleared - and the deduction was then silently never applied. It is honoured in
every penalising mode now, and the dialog offers the field in all of them,
because a rule that is displayed and not applied is worse than one that does
not exist.

### The other four

- **A blocked result never expired.** Its reason - *"no verified answer key for
  Set C"* - went on being asserted after the key was verified. Its stored
  reasons are now compared against the reasons it would be given now.
- **`ScoringPolicyDialog` round-tripped marks through `float`.** An exact rule
  that four decimal places cannot express (`1/3` as a blank mark) came back as
  `3333/10000` merely because somebody opened the dialog - creating a revision
  nobody asked for and making every result in the project stale under a rule
  that was never typed. Untouched fields now keep their loaded value exactly.
  `_exact` also silently returned `0` on a parse failure, which is
  indistinguishable from an operator typing zero; it now falls back to the
  spin box's own value, and locale group separators are handled rather than
  turned into an exception.
- **The Results stage read the whole batch twice per refresh** - once for the
  table, once for the summary line above it, each a full `gather_inputs`. One
  read now, filtered for the table and summarised before filtering.
- **Verification applied to the stored revision while the editor could show
  something else**, so an operator who edited the box and pressed Verify locked
  the key they had already saved and lost the edit without being told. The
  button is disabled while the editor is dirty.

Two smaller things were made honest rather than repaired: `candidate_result`
`.stale_reasons` is documented as permanently empty (staleness is derived on
read, and a stored flag would be wrong the moment a key was verified - dropping
a column in SQLite means rebuilding the table that holds every mark, which is
not a tidying-up job), and `get_result` carries a warning that it reads the
whole batch, so Phase 9 does not call it per candidate.

### Tests added

67 (314 total for the phase, from 247; the whole suite goes 2,937 -> 3,004).
The gaps were not in the arithmetic, which was already tested as a table -
they were in everything around it:

| Area | What was missing |
|---|---|
| Phase 7 states | Nothing exercised scoring against absent-with-script, present-without-script, duplicate scripts or an unknown candidate |
| Provenance across a reopen | The brief's key-revision and policy-revision reopen tests, including two changes before one recomputation |
| Cross-contamination | Deliberately opposite keys, multi-character set codes (`10`, `11`, `12`, `X1`) beside a decoy key for their first character |
| Numbering | A key and an answer string covering differently numbered questions |
| Whole-paper extremes | Every response correct/incorrect/blank/multiple, every question withdrawn, first and last question withdrawn |
| Determinism | Field-by-field identity over repeated runs, and order-independence of the sum |
| Privacy | Nothing asserted that a *mark* or an answer string stays out of the log - `test_candidate_privacy.py` was Phase 7 only |
| GUI | The cross-stage wiring, the dirty-editor rule, the dialog's exactness, one-read summaries, preflight collecting several issues, staleness from a key change and from a withdrawn question |

One test was found to be **hiding three others**: a class added during the
audit shadowed an existing `TestDeterminism`, and `ruff`'s `F811` caught it.
Worth knowing that the suite's own count is not self-checking.

A crash was also fixed in passing: the first version of the cancellation notice
was a modal dialog raised from `_on_scored`, which arrives whenever the run
happens to end - including during teardown, where it corrupted the heap. It is
a line in the summary now, and `_on_scored` drops signals that arrive after
`shutdown`.

## 12. For whoever picks this up

**Phase 9 (reports)** is next, and three things here are meant for it:

- `scoring_store.list_results` already returns everything a roll-wise report
  needs, with the provenance a defensible report should print: the key
  revision, the policy revision and whether the result is stale.
- **A report must refuse to export a stale result silently**, or at minimum
  mark it. The pattern is Phase 6's unresolved-conflict export warning.
- `reconciliation_script.is_primary` and `.excluded` are honoured by
  `services.scoring.working_script`. A report must honour them too rather than
  inventing its own rule for duplicates.

**If you add a scoring rule**, add it to `ScoringPolicy`, to `_POLICY_FIELDS`
in `scoring_store` (or a change to it will not create a revision, and results
will silently not go stale), and to `ScoringPolicy.describe` so the preview
tells the truth. There is a parametrised test asserting every field creates a
revision; extend it.

**If you touch the scorer**, keep `score_answers` pure and keep the precedence
order. The wrong-question check comes first for a reason, and moving it below
the blank check is a change that passes most tests and is wrong for exactly the
candidates least able to argue about it.

**The one thing not to do** is add a delta to an existing mark. It is the
fastest way to make a batch cheaper to rescore and the fastest way to make a
mark impossible to defend.
