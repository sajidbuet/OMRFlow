# Answer Key Format

Answer keys are entered and stored **inside the project**, not as a separate
file to be managed. There is no key file format to prepare.

## Entering a key

On the **Answer Key** stage:

- Select the **question-paper set**. Each set has its own independent key.
- Type the answers into the **Answers** field, or
- **Read From Solution Sheet…** to recognise the key from a filled-in sheet,
  using the same recognition path as a candidate's sheet.
- **Save As New Revision.**

The **Answers** field takes one option label per question, in question order —
`BDBDBADACB…` — and ignores spaces, tabs, line breaks, `,`, `;` and `|`, so a
key copied from a spreadsheet or an email pastes as it is. Any other character
is reported with its question number, never dropped.

Synthetic datasets (**Tools → Developer / Testing → Generate Synthetic Test
Dataset…**) write every set's key in exactly this form, as
`solution/Set_<code>_Answer_Key.txt`, beside a filled solution sheet
`solution/Set_<code>_Solution.png` that **Read From Solution Sheet…** reads
back as the same key. See `docs/testing/SYNTHETIC_DATA.md`.

## Revisions

Keys are versioned. Saving creates a new revision rather than overwriting the
previous one, so a key change is traceable — and recalculating results after
a key correction is a supported, recorded operation.

## Verification

**Verify Answer Key** is required before results can be calculated. The
**Validation** panel reports:

- missing answers,
- answers outside the choices the template defines,
- the wrong number of questions for the template.

A wrong key mis-marks a whole cohort silently and plausibly, which is why
OMRFlow will not score from an unverified one.

## Defective questions

**Wrong questions (full credit for everyone)** marks a question that should
not count against anyone; every candidate receives credit for it. It is
stored with the key, so the reason survives a recalculation instead of being
an untraceable manual adjustment to marks.

## Confidentiality

An answer key before an examination is among the most sensitive material a
project holds. It lives in the project database and the project's
`answer_keys\` folder. **Never attach a key to a public issue**, even after
the examination.

## Related

- [Answer Keys & Scoring](Answer-Keys-and-Scoring)
- [Recognition Symbols](Recognition-Symbols)
- `docs/scoring.md`
