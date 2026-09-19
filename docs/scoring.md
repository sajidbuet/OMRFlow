# Answer keys and scoring (Phase 8)

Turning recognised answers into marks that can be defended.

For the recognition that produced the answers, see
[`recognition_engine.md`](recognition_engine.md); for the review that settles a
disputed one, [`conflict_review.md`](conflict_review.md); for the
reconciliation that says whose paper it is, [`reconciliation.md`](reconciliation.md).

---

## 1. The claim this phase makes

> **Every mark is reproducible from stored inputs, the exact answer key used
> for each candidate is recorded, and changing a rule recomputes rather than
> patches.**

Everything below follows from that. A stored mark on its own is not
persistence; it is a number somebody would have to take on trust.

---

## 2. The canonical answer string

One character per question, in question order, **always the same length as the
paper**:

```text
ABCD?_BACCA_D?
```

| Character | Means |
|---|---|
| An option label (`A`, `B`, … as the template defines them) | One clear mark |
| `_` | Blank - the candidate answered nothing |
| `?` | **Confirmed** multiple - the candidate marked more than one bubble |

Question *N* is character *N*. The string is never compressed and a blank never
removed, so a candidate's answers can be read against a key without either side
having to say which questions it covers.

### `?` is not "the recogniser is unsure"

This distinction is load-bearing. A double mark that a reviewer has looked at
is a **fact about the paper** and is scored under the multiple-answer rule. A
reading still sitting in the Phase 6 queue is a **doubt**, and scoring is
**blocked** until somebody settles it:

```text
Cannot be scored: Answers still awaiting review:
Question 37 - resolve on the Resolve stage.
```

Treating an unread answer as a blank would quietly award the blank mark for a
question the candidate may well have answered.

---

## 3. Four values, kept apart

The same principle as Phase 7, one layer up:

| | Where it comes from | Can it change? |
|---|---|---|
| **Machine answer** | What recognition read | **Never.** |
| **Effective answer** | The machine's, unless Phase 6 changed it | Follows the review |
| **Answer key** | Typed, pasted, or read off a solution sheet | Only by a new **revision** |
| **Mark** | The scorer, over the three above | Recomputed, never edited |

A result stores both strings, so the detail view can show what the machine read
beside what was scored:

```text
Q37   Machine: (multiple)   Effective: B   Key: B   Correct   +1.00
```

---

## 4. Answer keys

### One key per question-paper set

Set codes are **not assumed to be one character** - `A`, `10` and `X1` are all
valid, as they are everywhere else in the application. A candidate is marked
against the key for **their own** set; there is no fallback to another set's
key, ever. Scoring a Set B paper against Set A's key produces a mark that looks
perfectly ordinary and is against the wrong paper, which is the worst outcome
available.

### Writing one

Type or paste one character per question. Spaces, tabs, line breaks, commas,
semicolons and pipes are ignored, so a key pastes cleanly out of a spreadsheet.
**Anything else is reported, never dropped** - a key silently shortened by one
stray character marks every candidate against the wrong questions from that
point on.

Lowercase is normalised (`abcd` → `ABCD`).

Validation refuses, naming the question:

> The answer key contains 98 answer(s), but this template contains 100
> questions. Please add answers for Questions 99-100.

> Question 43 has 'X', which is not one of this template's answer choices
> (A/B/C/D).

> Question 12 has no answer in the key. An answer key must give exactly one
> correct choice for every question; if the question itself is invalid, flag it
> as a wrong question instead.

Every problem is reported at once, not one per attempt.

### Reading one off a solution sheet

*Read From Solution Sheet…* runs the **existing recognition engine** - there is
no second OMR pipeline here - and loads the answers as a draft.

**A recognised sheet is never automatically authoritative.** Blanks and double
marks are listed and must be dealt with before the key can be verified:

> 3 question(s) were not a single clear mark: 12, 40, 77

Recognition completing says the sheet was read. It does not say the examiner
meant what was read.

### Verification

```text
DRAFT  ──verify──▶  VERIFIED  ──a later revision is verified──▶  SUPERSEDED
```

**Only a verified key produces marks.** Verifying records who checked it, and
warns that results computed under an earlier revision will go stale.

### Revisions

A revision is **never edited**. Correcting a verified key creates revision
*n+1* and supersedes the old one - which is kept, because results point at it.

```text
Set A revision 1   ABCCDA...   superseded
Set A revision 2   ABCADA...   verified
```

A `CandidateResult` records **the revision it used**, so "which key produced
this mark" always has an answer, and a later key never retroactively changes
what an earlier mark was computed from.

---

## 5. Wrong questions

A question the examiners have withdrawn is flagged **per set** - Set A's
wrong questions need not be Set B's.

Every scored candidate receives the **full correct mark**, whatever they did:

```text
marked the nominal answer   →  full credit
marked something else       →  full credit
marked two bubbles          →  full credit
left it blank               →  full credit
```

and **no deduction is ever applied**. The rule takes precedence over every
other, which is why it is checked first in the scorer - an implementation that
checked "blank first" would get the blank case wrong.

An absent candidate stays absent. A withdrawn question does not hand marks to
somebody who was never there.

---

## 6. Scoring rules

| Setting | Default | Notes |
|---|---|---|
| Correct answer | `+1.00` | Also what a wrong question pays |
| Blank answer | `0.00` | **A blank is not a wrong answer** and never attracts the deduction |
| Incorrect answer | `0.00` | See the modes below |
| Multiple answer | same as incorrect | Separately configurable |
| Minimum total | `0.00`, clamped | Recorded in the policy, not hard-coded |

### Negative marking

| Mode | Deduction per penalised response |
|---|---|
| **None** | 0 |
| **Fixed** | Whatever you set - `0.25` by convention, any value allowed |
| **1 mark per 3 wrong** | exactly `1/3` |
| **1 mark per 4 wrong** | exactly `1/4` |

The last two deduct one third and one quarter of **a mark**, not of the
correct-answer value: "one mark deducted per three wrong answers" is what the
published rule says, and scaling it would silently double the penalty on a
paper marked out of two per question.

**Fractional penalties are never truncated.** One wrong answer under the
1-per-3 rule costs `1/3`, not nothing:

```text
1 wrong  →  -1/3          3 wrong  →  -1
2 wrong  →  -2/3          6 wrong  →  -2
```

### Precedence

Tested as an explicit table, in this order:

1. the question is flagged **invalid for this set** → full credit, no deduction;
2. the response is **blank** → the blank mark;
3. the response is a **confirmed multiple** → the multiple deduction;
4. the response **equals the key** → the correct mark;
5. otherwise → the incorrect deduction.

---

## 7. Exact arithmetic

Every mark is a `fractions.Fraction`. Nothing goes through `float`.

`0.1 + 0.2` is not `0.3` in binary floating point, and a hundred questions at
`-1/3` accumulate an error that depends on the order the questions were added.
A decimal typed into the configuration is read with `Fraction(Decimal(text))`,
which is exact; `Fraction(0.1)` from a float is not one tenth.

**Rounding happens once, at the end, for display** - two places, half-up, with
`-0.00` normalised to `0.00`. It never feeds back: a `-1/3` policy scores the
same whether or not a label is narrow. Stored marks are exact rational strings
(`"65/4"`), not rounded decimals.

---

## 8. Who can be scored

Scoring respects Phase 7. A candidate is marked only when everything about them
has been settled:

| State | What happens |
|---|---|
| Reconciled, present, one script | Scored |
| **Absent** | `ABSENT`, **no mark** - not a zero |
| Present but no script | Cannot be scored |
| Duplicate script, none nominated | Cannot be scored |
| Unknown candidate ID | Cannot be scored |
| Set unresolved or unread | Cannot be scored |
| No verified key for the set | Cannot be scored |
| Answers still under review | Cannot be scored |

An absent candidate gets **no mark at all**. Writing zero would make them
indistinguishable from somebody who sat the paper and answered nothing, and
would drag every average down with a candidate who was never there.

Nothing blocked is hidden: it is a row with a reason, and *Check Before
Scoring* lists every one of them together rather than one dialog at a time.

---

## 9. Staleness, and recomputation

A result goes **stale** when any input changes:

- the answer key was revised;
- the scoring configuration was changed;
- an answer was corrected on the Resolve stage;
- the candidate's set changed;
- their reconciliation changed.

A stale result **keeps its mark** - it is a true record of what the earlier
inputs produced - but says so, and the summary counts it.

### Recompute, never patch

*Calculate Results* runs the whole scorer again over the stored inputs:

```text
stored effective answers
        + recorded set
        + verified answer-key revision
        + current scoring configuration
        + wrong-question configuration
                ↓
        FULL RECOMPUTATION
                ↓
          CandidateResult
```

Nothing anywhere adds a delta to an existing mark. A test asserts this directly
by **corrupting a stored score** and checking that recomputation produces the
correct value rather than the corrupted one adjusted.

Repeated recomputation with identical inputs is idempotent.

Cancelling a run **writes nothing**. A half-marked batch under two different
policies, presented as current, is worse than one that was never marked.

---

## 10. Storage

| Table | Holds |
|---|---|
| `answer_key_revision` | One set's answers at one revision, with its wrong questions, status and source |
| `scoring_policy_revision` | The marking rules at one revision, as exact rational strings |
| `candidate_result` | One candidate's mark, and the inputs that produced it |

Created by **migration 5**; field-by-field in [`DATA_MODEL.md`](DATA_MODEL.md).

### Why there is no per-question table

A hundred questions across ten thousand candidates is a million rows that would
have to be kept in step with a total they could contradict. Instead the result
keeps its **inputs**, and the breakdown is regenerated by the same pure
function that produced the mark - so a detail view and a total can never
disagree, because there is only one calculation.

---

## 11. What Phase 8 does not do

- It does not check that a key is *right*. It records who verified it.
- It does not produce reports or transcripts. That is Phase 9.
- It does not rank candidates.
- It has been exercised against **synthetic sheets and the repository's single
  real sample**. No examination has been marked with it; see
  `development/PHASE_08_HANDOFF.md`.
