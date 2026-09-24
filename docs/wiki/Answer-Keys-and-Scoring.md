# Answer Keys and Scoring

> The detailed reference is
> **[`docs/scoring.md`](https://github.com/sajidbuet/OMRFlow/blob/main/docs/scoring.md)**.

## Answer keys

One key per question-paper set, on the **Answer Key** stage.

1. Select the set under **Question-paper set**.
2. Enter the answers, or use **Read From Solution Sheet…** to recognise them
   from a filled-in sheet — the same recognition path as a candidate's sheet.
3. **Save As New Revision.** Keys are versioned; saving does not destroy the
   previous one.
4. **Verify Answer Key.**

### Verification is required

**Results cannot be calculated from an unverified key.** A wrong key
mis-marks an entire cohort silently and plausibly, so OMRFlow insists on a
deliberate confirmation step. The **Validation** panel reports what is wrong
— a missing answer, an answer outside the available choices, the wrong number
of questions.

### Defective questions

Under **Wrong questions (full credit for everyone)**, mark a question that
should not count against anyone. Every candidate receives credit for it. This
is recorded with the key rather than applied by editing marks afterwards, so
the reason survives a recalculation.

## Scoring

On the **Results** stage.

1. **Scoring Configuration…** — marks for a correct answer, for an incorrect
   one and for a blank, and whether negative marking applies.
2. **Check Before Scoring** reports what is not ready: an unverified key, an
   unreconciled roster, unresolved conflicts, candidates without scripts.
   Read it before continuing; it is the cheapest check available.
3. **Calculate Results.**

### How marks are decided

A blank and a wrong answer are different things and are scored separately —
which is what makes negative marking meaningful. A
[multiple mark](Recognition-Symbols) is not a correct answer.

### Ranking

**Standard competition ranking**: equal scores share a rank and the next rank
skips. `90, 88, 88, 85` gives `1, 2, 2, 4`.

### Reviewing and recalculating

- The **Results summary** lists candidates with marks and ranks.
- Select a candidate for their per-question detail.
- **Review Answers…** shows their answers against the key.
- **Recalculate This Candidate** re-scores one person after a correction,
  without re-scoring the cohort.

Changing a key or the scoring configuration and recalculating is a supported
path, and the stages downstream are told that stored results are no longer
current.

## Verify before you publish

Whatever OMRFlow computes, **check a sample by hand** before acting on it.
Real-data qualification is incomplete — see
[Known Limitations](Known-Limitations).

## Related

- [Results & Reports](Results-and-Reports)
- [Answer Key Format](Answer-Key-Format)
- [Recognition Symbols](Recognition-Symbols)
