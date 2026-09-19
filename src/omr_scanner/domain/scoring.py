"""Vocabulary for answer keys, scoring policy and a candidate's marks.

Purpose:
    The words Phase 8 reasons in - what a canonical answer string is, what an
    answer key is and when it may be used, how a mark is calculated, and what a
    result records about how it was produced. Pure data: no Qt, no SQLAlchemy,
    no file access.

Why exact arithmetic:
    A mark may end up on an official transcript. ``0.1 + 0.2`` is not ``0.3`` in
    binary floating point, and a hundred questions at ``-1/3`` accumulate a
    representation error that depends on the order the questions were added.
    Every mark in this module is a :class:`fractions.Fraction`, which is exact
    for every value an examination policy can express, and **rounding happens
    once, at the end, for display** (:func:`format_mark`). Never round per
    question.

Why the scorer is pure:
    :func:`score_answers` is a function of its arguments and nothing else - no
    clock, no configuration lookup, no database, no locale. That is what makes
    "recompute, never patch" meaningful: a result is reproducible by feeding
    the stored inputs back in, and a test can assert a hand-calculated mark
    without building an examination.

What does NOT belong here:
    * Reading a key from a file or a scan, storing a result, or deciding
      whether a candidate is eligible. Those are services.
    * Candidate data. This module describes shapes, never people.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from fractions import Fraction
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

# ----------------------------------------------------------------------
# The canonical answer string
# ----------------------------------------------------------------------
BLANK = "_"
"""One question, unanswered.

Kept as a *position* in the answer string rather than omitted: question N is
character N, always, so a string can be read against a key without either side
having to say which questions it covers.
"""

MULTIPLE = "?"
"""One question, answered more than once - a **confirmed** multiple mark.

Not the same as "recognition was unsure". An unresolved reading is a Phase 6
conflict and blocks scoring; see
:class:`omr_scanner.domain.scoring.ScoringBlock`. By the time a ``?`` reaches
this module it means "this candidate marked two bubbles", which is a fact worth
scoring, not a doubt worth hiding.
"""

RESERVED_SYMBOLS = frozenset({BLANK, MULTIPLE})
"""Symbols a template may not use as an answer label."""

MARK_DISPLAY_PLACES = 2
"""Decimal places used when a mark is shown or exported.

Display only. The stored and compared value is the exact
:class:`~fractions.Fraction`; formatting it at two places is a presentation
choice and must never feed back into arithmetic - a ``-1/3`` policy would
otherwise score differently because a label was narrow.
"""


def format_mark(value: Fraction) -> str:
    """Render an exact mark for display or export.

    Args:
        value: The exact mark.

    Returns:
        A fixed-point string at :data:`MARK_DISPLAY_PLACES`, half-up, with
        ``-0.00`` normalised to ``0.00``.

    Half-up rather than Python's default banker's rounding: an examination
    office reading ``0.125 -> 0.12`` beside ``0.135 -> 0.14`` will report it as
    a bug, and "round half away from zero" is what every published marking
    scheme means.
    """
    quantum = Decimal(1).scaleb(-MARK_DISPLAY_PLACES)
    rendered = (
        Decimal(value.numerator) / Decimal(value.denominator)
    ).quantize(quantum, rounding=ROUND_HALF_UP)
    if rendered == 0:
        rendered = abs(rendered)
    return f"{rendered:.{MARK_DISPLAY_PLACES}f}"


def parse_mark(text: str) -> Fraction:
    """Read a mark written as a decimal, exactly.

    ``Fraction("0.25")`` is exactly one quarter; ``Fraction(0.25)`` from a
    ``float`` happens to be too, but ``Fraction(0.1)`` is not one tenth. Marks
    therefore always arrive here as text and are never routed through
    ``float``.
    """
    return Fraction(Decimal(text.strip()))


# ----------------------------------------------------------------------
# Answer keys
# ----------------------------------------------------------------------
class AnswerKeyStatus(StrEnum):
    """Whether an answer key may be used to produce official marks."""

    DRAFT = "draft"
    """Entered or recognised, not yet checked by a person."""

    VERIFIED = "verified"
    """Checked and locked. The only status scoring will accept."""

    SUPERSEDED = "superseded"
    """Replaced by a later revision. Kept, because results reference it."""

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            AnswerKeyStatus.DRAFT: "Draft - not yet verified",
            AnswerKeyStatus.VERIFIED: "Verified",
            AnswerKeyStatus.SUPERSEDED: "Superseded by a later revision",
        }[self]

    @property
    def may_score(self) -> bool:
        """Whether marks may be produced from a key in this state."""
        return self is AnswerKeyStatus.VERIFIED


class AnswerKeySource(StrEnum):
    """Where an answer key's answers came from."""

    MANUAL = "manual"
    SCANNED = "scanned"

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            AnswerKeySource.MANUAL: "Entered by hand",
            AnswerKeySource.SCANNED: "Read from a solution sheet",
        }[self]


@dataclass(frozen=True, slots=True)
class AnswerKey:
    """One question-paper set's answers, at one revision.

    Attributes:
        set_code: Which paper this answers. **Not assumed to be one
            character** - ``"A"``, ``"10"`` and ``"X1"`` are all set codes the
            rest of the application already supports.
        answers: One character per question, in question order. A key has no
            blanks and no multiples: every position is a real option label.
        wrong_questions: Printed question numbers flagged invalid **for this
            set**. Every scored candidate receives full credit for these
            whatever they marked; see :func:`score_answers`.
        revision: 1 for the first key of a set, incrementing thereafter. A
            result records the revision it used, so "which key produced this
            mark" always has an answer.
        first_question: The printed number of ``answers[0]``.
        status / source: See the enums above.
    """

    set_code: str
    answers: str
    wrong_questions: frozenset[int] = frozenset()
    revision: int = 1
    first_question: int = 1
    status: AnswerKeyStatus = AnswerKeyStatus.DRAFT
    source: AnswerKeySource = AnswerKeySource.MANUAL

    @property
    def question_count(self) -> int:
        """How many questions this key covers."""
        return len(self.answers)

    @property
    def question_numbers(self) -> range:
        """The printed question numbers this key covers."""
        return range(self.first_question, self.first_question + len(self.answers))

    def answer_for(self, number: int) -> str:
        """The key's answer for one printed question number, or ``""``."""
        offset = number - self.first_question
        if 0 <= offset < len(self.answers):
            return self.answers[offset]
        return ""

    def is_wrong_question(self, number: int) -> bool:
        """Whether this printed question number is flagged invalid."""
        return number in self.wrong_questions

    def describe(self) -> str:
        """A one-line summary for a list or a status label."""
        flagged = (
            f", {len(self.wrong_questions)} wrong question(s)"
            if self.wrong_questions
            else ""
        )
        return (
            f"Set {self.set_code} revision {self.revision} - "
            f"{self.question_count} question(s){flagged} - {self.status.label}"
        )


# ----------------------------------------------------------------------
# Scoring policy
# ----------------------------------------------------------------------
class NegativeMarking(StrEnum):
    """How an incorrect or multiple response is penalised."""

    NONE = "none"
    FIXED = "fixed"
    ONE_PER_THREE = "one_per_three"
    ONE_PER_FOUR = "one_per_four"

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            NegativeMarking.NONE: "No negative marking",
            NegativeMarking.FIXED: "Fixed deduction per wrong answer",
            NegativeMarking.ONE_PER_THREE: "1 mark deducted per 3 wrong answers",
            NegativeMarking.ONE_PER_FOUR: "1 mark deducted per 4 wrong answers",
        }[self]


ONE_THIRD = Fraction(1, 3)
ONE_QUARTER = Fraction(1, 4)


@dataclass(frozen=True, slots=True)
class ScoringPolicy:
    """The rules a batch is marked under, at one revision.

    Every mark is a :class:`~fractions.Fraction`. The penalties are stored as
    **positive magnitudes** and subtracted, so a policy can never be made
    accidentally generous by an operator typing ``-0.25`` into a field already
    labelled "deduction".

    Attributes:
        correct_mark: Added for a correct answer, and for a question flagged
            invalid.
        blank_mark: Added for an unanswered question. Zero by default, and
            **never** the incorrect penalty: in this phase a blank is not a
            wrong answer.
        incorrect_penalty: Subtracted for a wrong answer under
            :attr:`NegativeMarking.FIXED`. Ignored by the other modes, which
            define their own deduction.
        multiple_penalty: Subtracted for a confirmed multiple answer. Defaults
            to the incorrect penalty when ``None``, which is what an
            examination usually means - but it is its own field so a paper that
            treats them differently needs no code change.
        mode: Which deduction applies.
        clamp_minimum: Whether a total below :attr:`minimum_score` is raised to
            it. Recorded rather than hard-coded, so a result can say whether it
            was clamped.
        minimum_score: The floor, when clamping.
        revision: 1 for the first policy, incrementing thereafter.
    """

    correct_mark: Fraction = Fraction(1)
    blank_mark: Fraction = Fraction(0)
    incorrect_penalty: Fraction = Fraction(0)
    multiple_penalty: Fraction | None = None
    mode: NegativeMarking = NegativeMarking.NONE
    clamp_minimum: bool = True
    minimum_score: Fraction = Fraction(0)
    revision: int = 1

    @property
    def effective_incorrect_penalty(self) -> Fraction:
        """The magnitude deducted for one wrong answer.

        The 1-per-3 and 1-per-4 modes deduct exactly one third and one quarter
        of a **mark**, not of the correct-answer value: "one mark deducted per
        three wrong answers" is what the published rule says, and scaling it by
        the correct mark would silently double the penalty on a paper marked
        out of two per question.
        """
        return {
            NegativeMarking.NONE: Fraction(0),
            NegativeMarking.FIXED: self.incorrect_penalty,
            NegativeMarking.ONE_PER_THREE: ONE_THIRD,
            NegativeMarking.ONE_PER_FOUR: ONE_QUARTER,
        }[self.mode]

    @property
    def effective_multiple_penalty(self) -> Fraction:
        """The magnitude deducted for one confirmed multiple answer."""
        if self.mode is NegativeMarking.NONE:
            return Fraction(0)
        if self.mode is NegativeMarking.FIXED and self.multiple_penalty is not None:
            return self.multiple_penalty
        return self.effective_incorrect_penalty

    def describe(self) -> tuple[str, ...]:
        """A human-readable summary of every rule, for the preview.

        Returned as lines rather than one string so a caller can lay them out;
        the wording is the wording an operator is asked to confirm before a
        batch is marked, so it is written here once rather than in the page.
        """
        lines = [
            f"Correct answer:   +{format_mark(self.correct_mark)}",
            f"Blank answer:      {format_mark(self.blank_mark)}",
        ]
        if self.mode is NegativeMarking.NONE:
            lines.append("Incorrect answer:  0.00  (no negative marking)")
            lines.append("Multiple answer:   0.00  (no negative marking)")
        else:
            lines.append(
                f"Incorrect answer: -{format_mark(self.effective_incorrect_penalty)}"
            )
            lines.append(
                f"Multiple answer:  -{format_mark(self.effective_multiple_penalty)}"
            )
            lines.append(f"Negative marking:  {self.mode.label}")
        lines.append(
            f"Minimum total:     {format_mark(self.minimum_score)}"
            if self.clamp_minimum
            else "Minimum total:     none (negative totals allowed)"
        )
        return tuple(lines)


# ----------------------------------------------------------------------
# Scoring one candidate
# ----------------------------------------------------------------------
class QuestionOutcome(StrEnum):
    """What happened on one question."""

    CORRECT = "correct"
    INCORRECT = "incorrect"
    BLANK = "blank"
    MULTIPLE = "multiple"
    WRONG_QUESTION = "wrong_question"

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            QuestionOutcome.CORRECT: "Correct",
            QuestionOutcome.INCORRECT: "Incorrect",
            QuestionOutcome.BLANK: "Blank",
            QuestionOutcome.MULTIPLE: "Multiple",
            QuestionOutcome.WRONG_QUESTION: "Wrong question - full credit",
        }[self]

    @property
    def counts_as_attempted(self) -> bool:
        """Whether the candidate put a mark on the paper for this question."""
        return self in (
            QuestionOutcome.CORRECT,
            QuestionOutcome.INCORRECT,
            QuestionOutcome.MULTIPLE,
        )


@dataclass(frozen=True, slots=True)
class QuestionScore:
    """One question's contribution to a mark, and why.

    Attributes:
        number: Printed question number.
        candidate: What the candidate effectively answered - an option label,
            :data:`BLANK` or :data:`MULTIPLE`.
        key: The key's answer.
        outcome: The classification.
        mark: This question's exact contribution, **signed**.
        wrong_question: Whether the question was flagged invalid for this set.
    """

    number: int
    candidate: str
    key: str
    outcome: QuestionOutcome
    mark: Fraction
    wrong_question: bool = False

    @property
    def display_mark(self) -> str:
        """The contribution, formatted."""
        rendered = format_mark(self.mark)
        return f"+{rendered}" if self.mark > 0 else rendered


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """A complete, self-explaining mark.

    Every number here is derived from :attr:`questions`, so the total and the
    detail cannot disagree - which is the reason a stored result keeps its
    inputs and regenerates this rather than storing a thousand rows that could.
    """

    questions: tuple[QuestionScore, ...]
    raw_score: Fraction
    final_score: Fraction
    clamped: bool = False

    @property
    def correct_count(self) -> int:
        """Questions answered correctly, excluding invalid ones."""
        return self._count(QuestionOutcome.CORRECT)

    @property
    def incorrect_count(self) -> int:
        """Questions answered wrongly, excluding invalid ones."""
        return self._count(QuestionOutcome.INCORRECT)

    @property
    def blank_count(self) -> int:
        """Questions left unanswered, excluding invalid ones."""
        return self._count(QuestionOutcome.BLANK)

    @property
    def multiple_count(self) -> int:
        """Questions with a confirmed multiple mark, excluding invalid ones."""
        return self._count(QuestionOutcome.MULTIPLE)

    @property
    def wrong_question_count(self) -> int:
        """Questions flagged invalid, all of which received full credit."""
        return self._count(QuestionOutcome.WRONG_QUESTION)

    @property
    def question_count(self) -> int:
        """How many questions were marked."""
        return len(self.questions)

    @property
    def penalised_count(self) -> int:
        """How many responses attracted a deduction."""
        return sum(1 for item in self.questions if item.mark < 0)

    def _count(self, outcome: QuestionOutcome) -> int:
        return sum(1 for item in self.questions if item.outcome is outcome)


def score_answers(
    answers: str,
    key: AnswerKey,
    policy: ScoringPolicy,
    *,
    first_question: int = 1,
) -> ScoreBreakdown:
    """Mark one candidate's answers. Pure, deterministic, exact.

    Args:
        answers: The canonical answer string - one character per question, in
            question order, using option labels, :data:`BLANK` and
            :data:`MULTIPLE`.
        key: The verified key for this candidate's set.
        policy: The rules to mark under.
        first_question: The printed number of ``answers[0]``.

    Returns:
        Every question's outcome and contribution, and the total.

    Raises:
        ValueError: ``answers`` and ``key`` cover different questions. A
            length mismatch is a bug or a mis-entered key, never something to
            paper over by marking the overlap.

    **Precedence**, in this order, and tested as such:

    1. the question is flagged invalid for this set -> full credit, whatever
       the candidate did and with no deduction;
    2. the response is blank -> the blank mark;
    3. the response is a confirmed multiple -> the multiple deduction;
    4. the response equals the key -> the correct mark;
    5. otherwise -> the incorrect deduction.

    Rule 1 comes first deliberately. A question the examiners have withdrawn is
    not a question a candidate can get wrong, so nothing below it may apply -
    including the multiple and blank rules, which is the case an implementation
    that checked "blank first" would get wrong.

    Every contribution is summed exactly and the total is clamped **once**, at
    the end. Rounding per question would make a ``-1/3`` policy depend on the
    order questions were added.
    """
    if len(answers) != key.question_count:
        raise ValueError(
            f"answer string covers {len(answers)} question(s) but the key for "
            f"set {key.set_code!r} covers {key.question_count}"
        )

    correct = policy.correct_mark
    blank = policy.blank_mark
    incorrect_penalty = policy.effective_incorrect_penalty
    multiple_penalty = policy.effective_multiple_penalty

    scored: list[QuestionScore] = []
    for offset, response in enumerate(answers):
        number = first_question + offset
        expected = key.answers[offset]

        if key.is_wrong_question(number):
            scored.append(
                QuestionScore(
                    number=number,
                    candidate=response,
                    key=expected,
                    outcome=QuestionOutcome.WRONG_QUESTION,
                    mark=correct,
                    wrong_question=True,
                )
            )
            continue

        if response == BLANK:
            outcome, mark = QuestionOutcome.BLANK, blank
        elif response == MULTIPLE:
            outcome, mark = QuestionOutcome.MULTIPLE, -multiple_penalty
        elif response == expected:
            outcome, mark = QuestionOutcome.CORRECT, correct
        else:
            outcome, mark = QuestionOutcome.INCORRECT, -incorrect_penalty

        scored.append(
            QuestionScore(
                number=number,
                candidate=response,
                key=expected,
                outcome=outcome,
                mark=mark,
            )
        )

    raw = sum((item.mark for item in scored), Fraction(0))
    final = raw
    clamped = False
    if policy.clamp_minimum and raw < policy.minimum_score:
        final = policy.minimum_score
        clamped = True
    return ScoreBreakdown(
        questions=tuple(scored), raw_score=raw, final_score=final, clamped=clamped
    )


# ----------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------
class ResultStatus(StrEnum):
    """What happened when a candidate was put through scoring."""

    SCORED = "scored"
    ABSENT = "absent"
    BLOCKED = "blocked"

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            ResultStatus.SCORED: "Scored",
            ResultStatus.ABSENT: "Absent",
            ResultStatus.BLOCKED: "Cannot be scored",
        }[self]

    @property
    def has_mark(self) -> bool:
        """Whether this status carries a numeric mark.

        An absent candidate has **no mark**, not a mark of zero. Writing zero
        would make them indistinguishable from somebody who sat the paper and
        answered nothing, and would drag every average down with a candidate
        who was never there.
        """
        return self is ResultStatus.SCORED


class BlockReason(StrEnum):
    """Why a candidate could not be scored.

    Every member describes something a person must decide. None of them is
    ever resolved by guessing - that is the whole point of having the list.
    """

    NOT_RECONCILED = "not_reconciled"
    NO_SCRIPT = "no_script"
    DUPLICATE_SCRIPT = "duplicate_script"
    UNKNOWN_CANDIDATE = "unknown_candidate"
    SET_UNRESOLVED = "set_unresolved"
    SET_MISSING = "set_missing"
    NO_VERIFIED_KEY = "no_verified_key"
    UNRESOLVED_ANSWERS = "unresolved_answers"
    ANSWER_LENGTH_MISMATCH = "answer_length_mismatch"
    NO_RESULT_STORED = "no_result_stored"

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            BlockReason.NOT_RECONCILED: "Reconciliation is not complete",
            BlockReason.NO_SCRIPT: "No script was received",
            BlockReason.DUPLICATE_SCRIPT: "More than one script, none nominated",
            BlockReason.UNKNOWN_CANDIDATE: "Script belongs to no registered candidate",
            BlockReason.SET_UNRESOLVED: "Question-paper set not yet resolved",
            BlockReason.SET_MISSING: "No question-paper set was read",
            BlockReason.NO_VERIFIED_KEY: "No verified answer key for this set",
            BlockReason.UNRESOLVED_ANSWERS: "Answers still awaiting review",
            BlockReason.ANSWER_LENGTH_MISMATCH: "Answers do not match the key length",
            BlockReason.NO_RESULT_STORED: "This sheet has no stored recognition result",
        }[self]


@dataclass(frozen=True, slots=True)
class ScoringBlock:
    """One reason one candidate could not be scored.

    Attributes:
        reason: What is wrong.
        detail: Specifics an operator needs - a set code, a list of question
            numbers. **May name a candidate**, and must therefore never be
            logged; see ``docs/reconciliation.md`` §9.
    """

    reason: BlockReason
    detail: str = ""

    def describe(self) -> str:
        """The message shown beside the candidate."""
        return f"{self.reason.label}{f': {self.detail}' if self.detail else ''}"


class StaleReason(StrEnum):
    """Why a stored result no longer reflects its inputs."""

    ANSWER_KEY = "answer_key"
    SCORING_POLICY = "scoring_policy"
    ANSWERS = "answers"
    SET_CODE = "set_code"
    RECONCILIATION = "reconciliation"

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            StaleReason.ANSWER_KEY: "the answer key has changed",
            StaleReason.SCORING_POLICY: "the scoring configuration has changed",
            StaleReason.ANSWERS: "this candidate's answers have changed",
            StaleReason.SET_CODE: "this candidate's question-paper set has changed",
            StaleReason.RECONCILIATION: "this candidate's reconciliation has changed",
        }[self]


@dataclass(frozen=True, slots=True)
class ResultCounts:
    """The batch summary an operator reads after scoring."""

    registered: int = 0
    scored: int = 0
    absent: int = 0
    blocked: int = 0
    stale: int = 0
    by_set: dict[str, int] = field(default_factory=dict)

    @property
    def is_clear(self) -> bool:
        """Whether every candidate is either scored, absent, or explained."""
        return self.stale == 0 and self.blocked == 0


def answer_labels_for(labels: Iterable[str]) -> tuple[str, ...]:
    """Normalise a template's answer labels for comparison.

    Uppercased and de-duplicated in order. A template whose labels collide with
    :data:`BLANK` or :data:`MULTIPLE` is refused by
    :func:`omr_scanner.services.answer_key.validate_labels`, not silently
    accepted here.
    """
    seen: list[str] = []
    for label in labels:
        upper = label.strip().upper()
        if upper and upper not in seen:
            seen.append(upper)
    return tuple(seen)


def canonical_answer_string(
    responses: Mapping[int, str],
    numbers: Sequence[int],
    labels: Iterable[str],
) -> str:
    """Assemble a canonical answer string from per-question values.

    Args:
        responses: What the candidate effectively answered, keyed by printed
            question number. A value of ``""`` is blank; a value containing
            ``"-"`` (the engine's ``"B-D"``) is a multiple; anything else is
            taken as an option label.
        numbers: Every printed question number, in order. Supplied rather than
            derived, so a question missing from ``responses`` becomes a blank
            in the right position instead of shortening the string.
        labels: The template's option labels.

    Returns:
        One character per entry in ``numbers``.

    A value that is neither blank, a multiple, nor a known label becomes
    :data:`MULTIPLE` - it is *some* mark this function cannot name, and
    reporting it as blank would credit a candidate for a question they
    answered. The caller is expected to have blocked such a sheet already; this
    is the safe fallback, not the mechanism.
    """
    known = set(answer_labels_for(labels))
    out: list[str] = []
    for number in numbers:
        raw = (responses.get(number) or "").strip().upper()
        if not raw:
            out.append(BLANK)
        elif "-" in raw or len(raw) > 1:
            out.append(MULTIPLE)
        elif raw in known:
            out.append(raw)
        else:
            out.append(MULTIPLE)
    return "".join(out)
