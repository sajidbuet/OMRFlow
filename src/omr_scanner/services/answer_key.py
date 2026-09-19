"""Read, validate and assemble an answer key.

Purpose:
    Turn what an operator types, pastes, or scans into an
    :class:`~omr_scanner.domain.scoring.AnswerKey` - or tell them exactly what
    is wrong with it. Pure: no database, no Qt.

Why validation is strict and talkative:
    A key is the standard every candidate is measured against. A key that is
    one question short, or that quietly drops a stray character, produces marks
    that look ordinary and are wrong for everybody. So every refusal here names
    the problem *and the position*, and nothing is repaired silently.

What this module will not do:
    * Guess a missing answer.
    * Accept a key of the wrong length by marking the overlap.
    * Treat a blank or a double mark on a solution sheet as an answer.
    * Decide that a recognised solution sheet is trustworthy. Recognition
      completing is not verification; a person does that.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from omr_scanner.domain.scoring import (
    BLANK,
    MULTIPLE,
    RESERVED_SYMBOLS,
    AnswerKey,
    AnswerKeySource,
    AnswerKeyStatus,
    answer_labels_for,
)
from omr_scanner.domain.template import QuestionBlockFieldDefinition
from omr_scanner.errors import OMRScannerError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services.recognition_models import ScanResult

_LOGGER = logging.getLogger(__name__)

_IGNORED_IN_PASTE = frozenset({" ", "\t", "\r", "\n", ",", ";", "|"})
"""Characters discarded when a key is pasted.

Separators a spreadsheet or an email adds, and nothing else. A character that
is not a separator and not an option label is **reported**, never dropped - a
key silently shortened by one stray letter is the defect this whole module
exists to prevent.
"""


class AnswerKeyError(OMRScannerError):
    """An answer key could not be read or accepted.

    Always carries a ``user_message`` naming the problem and, where it has one,
    the question number.
    """


# ----------------------------------------------------------------------
# What the template says
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class QuestionPlan:
    """The questions a template defines, and the labels they may be answered with.

    Assembled once and passed around, so no two parts of Phase 8 can disagree
    about how many questions there are.

    Attributes:
        numbers: Every printed question number, in order.
        labels: The option labels, uppercased, in printed order.
    """

    numbers: tuple[int, ...]
    labels: tuple[str, ...]

    @property
    def question_count(self) -> int:
        """How many questions the paper has."""
        return len(self.numbers)

    @property
    def first_question(self) -> int:
        """The lowest printed question number."""
        return self.numbers[0] if self.numbers else 1

    @property
    def is_contiguous(self) -> bool:
        """Whether the numbers run 1, 2, 3 ... without a gap.

        A key is a plain string, so a gap would make position and question
        number disagree. Templates built by the designer are always contiguous;
        this is what notices if one ever is not.
        """
        return all(
            later == earlier + 1
            for earlier, later in zip(self.numbers, self.numbers[1:], strict=False)
        )


def plan_for(template: OmrTemplate) -> QuestionPlan:
    """Read a template's question numbering and option labels.

    Raises:
        AnswerKeyError: The template has no questions, its question numbers are
            not contiguous, its blocks disagree about the option labels, or a
            label collides with the reserved blank/multiple symbols.

    Blocks are allowed to be several - a 100-question paper is four columns -
    but they must share one label set, because a key is one string over one
    alphabet.
    """
    numbers: list[int] = []
    label_sets: list[tuple[str, ...]] = []
    for zone in template.zones:
        field_def = zone.field
        if not isinstance(field_def, QuestionBlockFieldDefinition):
            continue
        numbers.extend(
            range(
                field_def.first_question,
                field_def.first_question + field_def.question_count,
            )
        )
        label_sets.append(answer_labels_for(field_def.answer_labels))

    if not numbers:
        raise AnswerKeyError(
            "Template defines no questions",
            user_message=(
                "This template contains no question regions, so there is "
                "nothing to write an answer key for. Add a Questions region in "
                "the Template stage."
            ),
        )

    ordered = tuple(sorted(numbers))
    duplicates = sorted({n for n in ordered if ordered.count(n) > 1})
    if duplicates:
        raise AnswerKeyError(
            f"Duplicate question numbers: {duplicates}",
            user_message=(
                "This template numbers the same question more than once "
                f"({', '.join(str(n) for n in duplicates[:5])}). Fix the "
                "question regions before writing an answer key."
            ),
        )

    distinct = set(label_sets)
    if len(distinct) > 1:
        raise AnswerKeyError(
            "Question blocks disagree about answer labels",
            user_message=(
                "The question regions in this template offer different answer "
                "choices from one another, so a single answer key cannot cover "
                "them all: "
                + "; ".join("/".join(item) for item in sorted(distinct))
            ),
        )

    labels = label_sets[0]
    reserved = sorted(set(labels) & RESERVED_SYMBOLS)
    if reserved:
        raise AnswerKeyError(
            f"Answer labels collide with reserved symbols: {reserved}",
            user_message=(
                f"This template uses {', '.join(reserved)} as an answer choice, "
                f"but OMRFlow reserves '{BLANK}' for a blank answer and "
                f"'{MULTIPLE}' for a multiple answer. Rename the choice in the "
                "Template stage."
            ),
        )

    plan = QuestionPlan(numbers=ordered, labels=labels)
    if not plan.is_contiguous:
        raise AnswerKeyError(
            "Question numbers are not contiguous",
            user_message=(
                "This template's question numbers have a gap in them, so an "
                "answer key written as one string could not be lined up "
                "against them. Renumber the question regions so they run "
                "consecutively."
            ),
        )
    return plan


# ----------------------------------------------------------------------
# Reading a typed or pasted key
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class KeyIssue:
    """One problem with a proposed key.

    Attributes:
        message: Operator-facing text, naming the question where it has one.
        question: The printed question number, or ``0`` for a whole-key issue.
    """

    message: str
    question: int = 0


@dataclass(frozen=True, slots=True)
class KeyDraft:
    """A proposed answer key and everything wrong with it.

    Reading never raises for *content* problems - the interface wants to show
    them all at once, beside the text the operator is still editing. Only a
    structurally impossible request (a template with no questions) raises.
    """

    answers: str
    plan: QuestionPlan
    set_code: str
    issues: tuple[KeyIssue, ...] = ()
    wrong_questions: frozenset[int] = frozenset()
    source: AnswerKeySource = AnswerKeySource.MANUAL
    ignored_characters: str = ""

    @property
    def is_valid(self) -> bool:
        """Whether this draft may be stored."""
        return not self.issues

    def to_key(self, *, revision: int = 1) -> AnswerKey:
        """Build the key. Only call when :attr:`is_valid`."""
        if not self.is_valid:
            raise AnswerKeyError(
                "Refusing to build an invalid answer key",
                user_message=self.issues[0].message,
            )
        return AnswerKey(
            set_code=self.set_code,
            answers=self.answers,
            wrong_questions=frozenset(self.wrong_questions),
            revision=revision,
            first_question=self.plan.first_question,
            status=AnswerKeyStatus.DRAFT,
            source=self.source,
        )

    @property
    def summary(self) -> str:
        """A one-line description for the review panel."""
        return (
            f"Set {self.set_code} - {len(self.answers)} of "
            f"{self.plan.question_count} answer(s)"
            + (f", {len(self.issues)} problem(s)" if self.issues else ", valid")
        )


def normalise_key_text(text: str) -> tuple[str, str]:
    """Strip separators from a pasted key.

    Returns:
        ``(cleaned, ignored)`` - the key with separators removed and uppercased,
        and the separator characters that were removed.

    Only the characters in :data:`_IGNORED_IN_PASTE` are removed. Anything else
    survives into validation so it can be *reported*: a key quietly shortened
    by a stray character marks every candidate against the wrong questions from
    that point on.
    """
    kept: list[str] = []
    ignored: list[str] = []
    for character in text:
        if character in _IGNORED_IN_PASTE:
            ignored.append(character)
        else:
            kept.append(character.upper())
    return "".join(kept), "".join(ignored)


def read_key(
    text: str,
    plan: QuestionPlan,
    set_code: str,
    *,
    wrong_questions: Sequence[int] = (),
    source: AnswerKeySource = AnswerKeySource.MANUAL,
) -> KeyDraft:
    """Read a typed or pasted answer key and report everything wrong with it.

    Args:
        text: What the operator entered.
        plan: The template's questions and labels.
        set_code: Which paper this answers.
        wrong_questions: Printed question numbers flagged invalid for this set.
        source: How the answers were obtained.

    Returns:
        The draft, with an issue for every problem found - all of them, not the
        first, because an operator fixing a key one message at a time gives up.
    """
    cleaned, ignored = normalise_key_text(text)
    issues: list[KeyIssue] = []
    labels = set(plan.labels)
    expected = plan.question_count

    if not set_code.strip():
        issues.append(
            KeyIssue(
                "This answer key has no question-paper set. Choose the set it "
                "answers before saving it."
            )
        )

    if len(cleaned) != expected:
        issues.append(KeyIssue(_length_message(cleaned, plan)))

    for offset, character in enumerate(cleaned[:expected]):
        number = plan.first_question + offset
        if character in labels:
            continue
        if character == BLANK:
            issues.append(
                KeyIssue(
                    f"Question {number} has no answer in the key. An answer key "
                    "must give exactly one correct choice for every question; "
                    "if the question itself is invalid, flag it as a wrong "
                    "question instead.",
                    question=number,
                )
            )
        elif character == MULTIPLE:
            issues.append(
                KeyIssue(
                    f"Question {number} is marked as a multiple answer. An "
                    "answer key must give exactly one correct choice.",
                    question=number,
                )
            )
        else:
            issues.append(
                KeyIssue(
                    f"Question {number} has '{character}', which is not one of "
                    f"this template's answer choices ({'/'.join(plan.labels)}).",
                    question=number,
                )
            )

    issues.extend(_wrong_question_issues(wrong_questions, plan))

    return KeyDraft(
        answers=cleaned,
        plan=plan,
        set_code=set_code.strip(),
        issues=tuple(issues),
        wrong_questions=frozenset(
            number for number in wrong_questions if number in plan.numbers
        ),
        source=source,
        ignored_characters=ignored,
    )


def _length_message(cleaned: str, plan: QuestionPlan) -> str:
    """Say precisely which questions are missing or extra."""
    found, expected = len(cleaned), plan.question_count
    if found < expected:
        first_missing = plan.first_question + found
        last = plan.numbers[-1]
        missing = (
            f"Question {first_missing}"
            if first_missing == last
            else f"Questions {first_missing}-{last}"
        )
        return (
            f"The answer key contains {found} answer(s), but this template "
            f"contains {expected} questions. Please add answers for {missing}."
        )
    return (
        f"The answer key contains {found} answer(s), but this template contains "
        f"only {expected} questions. Remove the {found - expected} extra "
        f"character(s) at the end: '{cleaned[expected:][:12]}'."
    )


def _wrong_question_issues(
    wrong_questions: Sequence[int], plan: QuestionPlan
) -> list[KeyIssue]:
    """Report wrong-question numbers that are not questions."""
    issues: list[KeyIssue] = []
    known = set(plan.numbers)
    for number in wrong_questions:
        if number in known:
            continue
        issues.append(
            KeyIssue(
                f"Question {number} is flagged as a wrong question, but this "
                f"template's questions run "
                f"{plan.first_question}-{plan.numbers[-1]}."
            )
        )
    return issues


def parse_wrong_questions(text: str, plan: QuestionPlan) -> tuple[frozenset[int], str]:
    """Read a comma-separated list of wrong-question numbers.

    Returns:
        ``(numbers, error)`` - the numbers, and an operator-facing message when
        something could not be read. An unreadable entry is **reported**, not
        skipped; a wrong-question list one entry short awards full credit to
        the wrong cohort.
    """
    numbers: set[int] = set()
    bad: list[str] = []
    for chunk in text.replace(";", ",").split(","):
        token = chunk.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError:
            bad.append(token)
            continue
        if value not in plan.numbers:
            bad.append(token)
            continue
        numbers.add(value)
    if bad:
        return frozenset(numbers), (
            f"Not a question number in this template: {', '.join(bad[:6])}. "
            f"Questions run {plan.first_question}-{plan.numbers[-1]}."
        )
    return frozenset(numbers), ""


# ----------------------------------------------------------------------
# Reading a key off a scanned solution sheet
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ScannedKey:
    """What a solution sheet was read as, and why it may not be usable.

    Attributes:
        answers: The recognised answer string, with :data:`BLANK` and
            :data:`MULTIPLE` where the sheet was not a single clean mark.
        set_code: The set code read off the sheet, if any. A suggestion for the
            operator, never an automatic choice.
        unreadable: Question numbers that came back blank or multiple. These
            are what stop a solution sheet being used as a key: an answer key
            has one answer per question, so anything else is a question for a
            person.
        registered: Whether the page rectified at all.
    """

    answers: str
    plan: QuestionPlan
    set_code: str = ""
    unreadable: tuple[int, ...] = ()
    registered: bool = True
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_clean(self) -> bool:
        """Whether every question came back as exactly one mark."""
        return self.registered and not self.unreadable

    @property
    def summary(self) -> str:
        """A one-line description for the review panel."""
        if not self.registered:
            return "The solution sheet could not be registered - no answers were read."
        if self.unreadable:
            shown = ", ".join(str(n) for n in self.unreadable[:8])
            more = "" if len(self.unreadable) <= 8 else f" (+{len(self.unreadable) - 8} more)"
            return (
                f"{len(self.unreadable)} question(s) were not a single clear "
                f"mark: {shown}{more}"
            )
        return f"All {self.plan.question_count} questions read as a single mark."


def key_from_scan(result: ScanResult, plan: QuestionPlan) -> ScannedKey:
    """Read an answer key off a recognised solution sheet.

    Uses the recognition result the existing engine produced - this module runs
    no image processing of its own.

    A blank or a double mark becomes :data:`BLANK`/:data:`MULTIPLE` and is
    listed in :attr:`ScannedKey.unreadable`. It is **not** guessed at, and the
    key cannot be verified until a person has dealt with it: a solution sheet
    is evidence of the examiner's intent, not a substitute for checking it.
    """
    from omr_scanner.services.recognition_models import RegistrationStatus

    registered = result.registration != RegistrationStatus.FAILED.value
    responses = {item.number: item.value for item in result.answers}
    known = set(plan.labels)

    characters: list[str] = []
    unreadable: list[int] = []
    for number in plan.numbers:
        raw = (responses.get(number) or "").strip().upper()
        if not raw:
            characters.append(BLANK)
            unreadable.append(number)
        elif "-" in raw or len(raw) > 1:
            characters.append(MULTIPLE)
            unreadable.append(number)
        elif raw in known:
            characters.append(raw)
        else:
            characters.append(MULTIPLE)
            unreadable.append(number)

    _LOGGER.info(
        "Solution sheet read for an answer key: questions=%d unreadable=%d "
        "registered=%s",
        plan.question_count,
        len(unreadable),
        registered,
    )
    return ScannedKey(
        answers="".join(characters),
        plan=plan,
        set_code=result.set_code_value.strip().upper(),
        unreadable=tuple(unreadable),
        registered=registered,
        warnings=tuple(result.warnings),
    )
