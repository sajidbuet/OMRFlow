"""Decide who can be scored, assemble their answers, and mark them.

Purpose:
    The deterministic core of Phase 8. Given a candidate's reconciliation, the
    recognition result for their script, whatever Phase 6 decided about it, and
    a verified key, either produce a mark or say precisely why one cannot be
    produced.

Scope:
    Pure functions over value objects. No database, no Qt, no image handling.
    The arithmetic lives one level down in
    :mod:`omr_scanner.domain.scoring`; this module is the part that knows about
    candidates, scripts and the four phases underneath.

The rule the whole module is arranged around:

    **A mark is only ever produced from inputs somebody has settled.** Every
    uncertainty - an unreconciled candidate, an unresolved set, a duplicate
    script, an answer still in the review queue, a key nobody has verified -
    is a :class:`~omr_scanner.domain.scoring.ScoringBlock`, never a guess and
    never a zero.

Determinism:
    :func:`score_candidate` is a function of its arguments. Feed it the stored
    inputs again and it produces the same breakdown, which is what makes
    "recompute, never patch" a thing that can be tested rather than a slogan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from omr_scanner.domain.reconciliation import (
    AttendanceState,
    ReconciliationEntry,
    ReconciliationStatus,
)
from omr_scanner.domain.scoring import (
    RESERVED_SYMBOLS,
    AnswerKey,
    BlockReason,
    QuestionOutcome,
    ResultStatus,
    ScoreBreakdown,
    ScoringBlock,
    ScoringPolicy,
    canonical_answer_string,
    score_answers,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from omr_scanner.services.answer_key import QuestionPlan
    from omr_scanner.services.recognition_models import ScanResult

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "CandidateAnswers",
    "CandidateScore",
    "ScoringInputs",
    "build_candidate_answers",
    "score_candidate",
    "unnameable_responses",
    "usable_set_code",
    "working_script",
]


# ----------------------------------------------------------------------
# What a candidate answered
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CandidateAnswers:
    """One candidate's effective answers, and their provenance.

    Attributes:
        scan_id: The script these came from.
        answers: The canonical answer string - see
            :mod:`omr_scanner.domain.scoring`.
        machine_answers: The same string as **recognition alone** produced it,
            before any Phase 6 correction. Kept so a result can show both and
            so nothing here can be mistaken for a licence to overwrite the
            machine's reading.
        corrected: Printed question numbers a named reviewer decided.
        unresolved: Printed question numbers whose conflict is still open.
            Non-empty blocks scoring: an unread answer is not a blank.
        unnameable: Printed question numbers whose stored value is not one of
            the template's current answer choices. Non-empty blocks scoring;
            see :func:`unnameable_responses`.
        set_code: The candidate's question-paper set, after Phase 6 review.
        machine_set_code: The set code recognition read.
    """

    scan_id: int
    answers: str
    machine_answers: str
    corrected: tuple[int, ...] = ()
    unresolved: tuple[int, ...] = ()
    unnameable: tuple[int, ...] = ()
    set_code: str = ""
    machine_set_code: str = ""

    @property
    def was_corrected(self) -> bool:
        """Whether a person changed any answer on this script."""
        return bool(self.corrected)

    def machine_answer_for(self, number: int, first_question: int) -> str:
        """What recognition alone read for one printed question number."""
        offset = number - first_question
        if 0 <= offset < len(self.machine_answers):
            return self.machine_answers[offset]
        return ""


def build_candidate_answers(
    result: ScanResult,
    plan: QuestionPlan,
    *,
    scan_id: int = 0,
    decided: Mapping[int, str] | None = None,
    unresolved: Sequence[int] = (),
    set_code: str = "",
) -> CandidateAnswers:
    """Assemble the canonical answer string for one script.

    Args:
        result: What recognition produced for the sheet.
        plan: The template's questions and option labels.
        scan_id: The script's durable id, carried through for provenance.
        decided: Answers a named reviewer decided, keyed by printed question
            number (Phase 6). These override recognition.
        unresolved: Printed question numbers whose conflict is still open.
        set_code: The effective set code, after review. Falls back to the
            machine's.

    Returns:
        Both strings - effective and machine-only - of exactly
        ``plan.question_count`` characters.

    **The machine's reading is never modified.** A correction changes which
    character this function *emits*; ``result`` is read and put down again
    untouched, and :attr:`CandidateAnswers.machine_answers` preserves what it
    said.
    """
    machine = {item.number: item.value for item in result.answers}
    effective = dict(machine)
    if decided:
        effective.update(decided)

    return CandidateAnswers(
        scan_id=scan_id,
        answers=canonical_answer_string(effective, plan.numbers, plan.labels),
        machine_answers=canonical_answer_string(machine, plan.numbers, plan.labels),
        corrected=tuple(sorted(decided or {})),
        unresolved=tuple(sorted(unresolved)),
        unnameable=unnameable_responses(effective, plan),
        set_code=usable_set_code(set_code or result.set_code_value),
        machine_set_code=usable_set_code(result.set_code_value),
    )


def unnameable_responses(
    responses: Mapping[int, str], plan: QuestionPlan
) -> tuple[int, ...]:
    """Printed question numbers whose value is not a choice this paper offers.

    A sheet read when the template offered ``A/B/C/D/E`` and marked against a
    template since edited down to ``A/B/C/D`` has stored values the plan cannot
    name. :func:`~omr_scanner.domain.scoring.canonical_answer_string` turns
    those into :data:`~omr_scanner.domain.scoring.MULTIPLE`, because calling
    them blank would credit a candidate for a question they answered - but a
    multiple attracts the multiple deduction, so a candidate would be penalised
    for a template edit.

    Neither reading is right. What is right is to stop, which is what a
    non-empty result here makes :func:`score_candidate` do: the sheet needs
    reading again against the template it is being marked against.
    """
    known = set(plan.labels)
    found: list[int] = []
    for number in plan.numbers:
        raw = (responses.get(number) or "").strip().upper()
        # Blank, a known choice, and the engine's "B-D" are all nameable. The
        # test mirrors canonical_answer_string exactly, so the two cannot
        # disagree about what a value means.
        if not raw or raw in known or "-" in raw or len(raw) > 1:
            continue
        found.append(number)
    return tuple(found)


def usable_set_code(value: str) -> str:
    """Return a set code, or ``""`` when it is not one.

    Recognition assembles a field value with :data:`~omr_scanner.domain.scoring.BLANK`
    for a position nobody marked and :data:`~omr_scanner.domain.scoring.MULTIPLE`
    for one marked twice, so an unread set code arrives as ``"_"`` rather than
    as an empty string. Passing that through would look up a key for a set
    called ``"_"`` and report "no verified answer key for Set _", which sends an
    operator looking for a key rather than at the sheet.

    A set code is usable only when every printed position resolved to a real
    symbol.
    """
    cleaned = value.strip().upper()
    if not cleaned or any(character in RESERVED_SYMBOLS for character in cleaned):
        return ""
    return cleaned


# ----------------------------------------------------------------------
# Which script to mark
# ----------------------------------------------------------------------
def working_script(entry: ReconciliationEntry) -> tuple[int | None, ScoringBlock | None]:
    """Choose the one script to mark for a candidate.

    Returns:
        ``(scan_id, None)`` when exactly one script applies, or
        ``(None, block)`` when one cannot be chosen.

    Scripts an operator has set aside do not count - that is what setting one
    aside means. Among several that remain, the one nominated as the working
    script wins; **without a nomination this refuses**, because picking the
    lowest scan id would be choosing a candidate's paper for them.
    """
    counted = [item for item in entry.scripts if item.counts_as_a_script]
    if not counted:
        return None, ScoringBlock(BlockReason.NO_SCRIPT)
    if len(counted) == 1:
        return counted[0].script.scan_id, None

    nominated = [item for item in counted if item.primary]
    if len(nominated) == 1:
        return nominated[0].script.scan_id, None
    return None, ScoringBlock(
        BlockReason.DUPLICATE_SCRIPT,
        detail=(
            f"{len(counted)} scripts are attributed to this candidate. Set one "
            "aside as an accidental re-scan, or nominate the working script, "
            "on the Attendance stage."
        ),
    )


# ----------------------------------------------------------------------
# Scoring one candidate
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ScoringInputs:
    """Everything needed to mark one candidate, gathered in one place.

    A small class rather than six parameters, because the store, the GUI and
    the tests all assemble the same things and a positional mistake between two
    strings would be silent.
    """

    entry: ReconciliationEntry
    answers: CandidateAnswers | None
    key: AnswerKey | None
    policy: ScoringPolicy
    plan: QuestionPlan


@dataclass(frozen=True, slots=True)
class CandidateScore:
    """The outcome of putting one candidate through scoring.

    Exactly one of :attr:`breakdown` and :attr:`blocks` is meaningful:
    :attr:`status` says which.
    """

    candidate_id: str
    status: ResultStatus
    set_code: str = ""
    answers: CandidateAnswers | None = None
    key: AnswerKey | None = None
    breakdown: ScoreBreakdown | None = None
    blocks: tuple[ScoringBlock, ...] = ()

    @property
    def is_scored(self) -> bool:
        """Whether this candidate has a mark."""
        return self.status is ResultStatus.SCORED and self.breakdown is not None

    def describe_blocks(self) -> str:
        """Every reason this candidate could not be scored, as one line."""
        return "; ".join(item.describe() for item in self.blocks)


def score_candidate(inputs: ScoringInputs) -> CandidateScore:
    """Mark one candidate, or explain why they cannot be marked.

    The precedence is explicit and is the order of the checks below:

    1. **Not eligible** - the candidate is absent, or reconciliation left an
       exception on them. An absent candidate gets
       :attr:`~omr_scanner.domain.scoring.ResultStatus.ABSENT` and **no mark**,
       not a zero.
    2. **No usable script** - none, or several with none nominated.
    3. **Set unresolved or missing.**
    4. **No verified key** for that set.
    5. **Answers this template cannot name**, answers still in the review
       queue, or answers of the wrong length or numbering.
    6. Otherwise mark, question by question, under
       :func:`~omr_scanner.domain.scoring.score_answers` - where a question
       flagged invalid takes precedence over everything else.

    Every check that fails contributes a block rather than returning at once,
    so an operator sees all of a candidate's problems together.
    """
    entry = inputs.entry
    blocks: list[ScoringBlock] = []

    # 1. Eligibility. Absence is an outcome, not a failure - but only once
    # reconciliation has *settled* that the candidate was absent.
    #
    # A candidate marked absent whose script turned up is ABSENT_WITH_SCRIPT:
    # either the attendance record or the recognised ID is wrong, and Phase 7
    # exists to decide which. Recording them as a plain "Absent" would present
    # an open question as a finished answer, and the physical script in the
    # room would never be marked. The same is true of any other exception a
    # candidate carries while marked absent - a duplicate script, an unresolved
    # ID. Only ABSENT_CONFIRMED is an outcome.
    if entry.effective_attendance is AttendanceState.ABSENT:
        if entry.status.is_exception:
            blocks.append(
                ScoringBlock(
                    BlockReason.NOT_RECONCILED,
                    detail=(
                        f"{entry.status.label}. Settle this on the Attendance "
                        "stage: a candidate recorded absent whose script exists "
                        "is neither absent nor markable until somebody decides "
                        "which record is right."
                    ),
                )
            )
            return CandidateScore(
                candidate_id=entry.candidate_id,
                status=ResultStatus.BLOCKED,
                set_code=inputs.answers.set_code if inputs.answers else "",
                answers=inputs.answers,
                blocks=tuple(blocks),
            )
        return CandidateScore(
            candidate_id=entry.candidate_id,
            status=ResultStatus.ABSENT,
            set_code=inputs.answers.set_code if inputs.answers else "",
        )
    if not entry.is_registered:
        blocks.append(
            ScoringBlock(
                BlockReason.UNKNOWN_CANDIDATE,
                detail="Assign this script to a registered candidate first.",
            )
        )
    elif entry.status is not ReconciliationStatus.MATCHED:
        blocks.append(
            ScoringBlock(
                BlockReason.NOT_RECONCILED,
                detail=entry.status.label,
            )
        )

    # 2. The script.
    answers = inputs.answers
    if answers is None:
        _, block = working_script(entry)
        blocks.append(block or ScoringBlock(BlockReason.NO_RESULT_STORED))
    else:
        # 3. The set. Without one there is no key to mark against, and
        # falling back to another set's key would produce a plausible mark
        # against the wrong paper - the failure this check exists to prevent.
        if not answers.set_code:
            blocks.append(
                ScoringBlock(
                    BlockReason.SET_MISSING,
                    detail=(
                        "No question-paper set was read from this sheet. "
                        "Resolve it on the Resolve stage."
                    ),
                )
            )
        # 5a-. A value this template cannot name. Checked before the review
        # queue because it is not a disagreement about what the sheet says -
        # it is a sheet read against a different paper.
        if answers.unnameable:
            shown = ", ".join(str(n) for n in answers.unnameable[:6])
            more = (
                "" if len(answers.unnameable) <= 6
                else f" (+{len(answers.unnameable) - 6} more)"
            )
            blocks.append(
                ScoringBlock(
                    BlockReason.UNNAMEABLE_RESPONSE,
                    detail=(
                        f"Question {shown}{more} hold answers this template no "
                        "longer offers. Read the batch again with the template "
                        "it is being marked against."
                    ),
                )
            )
        # 5a. Answers still under review.
        if answers.unresolved:
            shown = ", ".join(str(n) for n in answers.unresolved[:6])
            more = (
                "" if len(answers.unresolved) <= 6
                else f" (+{len(answers.unresolved) - 6} more)"
            )
            blocks.append(
                ScoringBlock(
                    BlockReason.UNRESOLVED_ANSWERS,
                    detail=f"Question {shown}{more} - resolve on the Resolve stage.",
                )
            )

    # 4. The key. Only worth reporting when a set is known: "no verified key
    # for Set _" would send an operator looking for a key when the real
    # problem is the sheet, and the set block above already says so.
    key = inputs.key
    has_set = answers is not None and bool(answers.set_code)
    if key is None and (answers is None or has_set):
        blocks.append(
            ScoringBlock(
                BlockReason.NO_VERIFIED_KEY,
                detail=f"Set {answers.set_code}" if has_set and answers else "",
            )
        )
    elif key is not None and answers is not None:
        if len(answers.answers) != key.question_count:
            blocks.append(
                ScoringBlock(
                    BlockReason.ANSWER_LENGTH_MISMATCH,
                    detail=(
                        f"{len(answers.answers)} answer(s) read, "
                        f"{key.question_count} expected"
                    ),
                )
            )
        elif inputs.plan.first_question != key.first_question:
            # The key's wrong-question numbers are *printed* question numbers.
            # A key written against a differently numbered paper lines its
            # answers up perfectly and withdraws the wrong questions, so this
            # has to block rather than be corrected by guessing which numbering
            # the examiners meant.
            blocks.append(
                ScoringBlock(
                    BlockReason.KEY_NUMBERING_MISMATCH,
                    detail=(
                        f"the key numbers its questions from "
                        f"{key.first_question}, this paper from "
                        f"{inputs.plan.first_question}. Save a new revision of "
                        f"the key for set {key.set_code}."
                    ),
                )
            )

    if blocks or answers is None or key is None:
        return CandidateScore(
            candidate_id=entry.candidate_id,
            status=ResultStatus.BLOCKED,
            set_code=answers.set_code if answers else "",
            answers=answers,
            key=key,
            blocks=tuple(blocks),
        )

    breakdown = score_answers(
        answers.answers, key, inputs.policy, first_question=inputs.plan.first_question
    )
    return CandidateScore(
        candidate_id=entry.candidate_id,
        status=ResultStatus.SCORED,
        set_code=answers.set_code,
        answers=answers,
        key=key,
        breakdown=breakdown,
    )


def outcome_counts(breakdown: ScoreBreakdown) -> dict[QuestionOutcome, int]:
    """Count each outcome once, for a summary panel."""
    counts = dict.fromkeys(QuestionOutcome, 0)
    for item in breakdown.questions:
        counts[item.outcome] += 1
    return counts
