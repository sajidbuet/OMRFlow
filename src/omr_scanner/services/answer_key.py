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
from dataclasses import dataclass, field, replace
from enum import StrEnum
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
from omr_scanner.domain.set_identity import SetIdentity, canonical_code, same_set
from omr_scanner.domain.template import QuestionBlockFieldDefinition
from omr_scanner.errors import OMRScannerError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

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

    withdrawn = set(wrong_questions)
    for offset, character in enumerate(cleaned[:expected]):
        number = plan.first_question + offset
        if character in labels:
            continue
        if character == BLANK and number in withdrawn:
            # A withdrawn question is marked full credit before its key answer
            # is ever consulted (see `score_answers`), so leaving it unanswered
            # is honest: a solution sheet that left it blank is not made to
            # carry an answer nobody gave.
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
    repeated: list[int] = []
    for chunk in text.replace(";", ",").replace(" ", ",").split(","):
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
        if value in numbers:
            repeated.append(value)
        numbers.add(value)
    if bad:
        return frozenset(numbers), (
            f"Not a question number in this template: {', '.join(bad[:6])}. "
            f"Questions run {plan.first_question}-{plan.numbers[-1]}."
        )
    if repeated:
        # Reported, not merged: a number typed twice is usually a different
        # number typed wrongly, and merging it hides the one that was meant.
        return frozenset(numbers), (
            "Listed more than once as a full-credit question: "
            f"{', '.join(str(n) for n in sorted(set(repeated))[:6])}."
        )
    return frozenset(numbers), ""


def format_question_numbers(numbers: Iterable[int]) -> str:
    """Write question numbers the way :func:`parse_wrong_questions` reads them."""
    return ", ".join(str(number) for number in sorted(set(numbers)))


def draft_from_key(key: AnswerKey, plan: QuestionPlan) -> KeyDraft:
    """Re-read a stored key against the current template.

    A stored key is shown through the same validation as a typed one, so a key
    written for a template that has since changed reports exactly what no
    longer fits - rather than being trusted because it was once valid.
    """
    return read_key(
        key.answers,
        plan,
        key.set_code,
        wrong_questions=sorted(key.wrong_questions),
        source=key.source,
    )


# ----------------------------------------------------------------------
# Whether a stored key still fits the active template
# ----------------------------------------------------------------------
def compatibility_issues(key: AnswerKey, plan: QuestionPlan) -> tuple[str, ...]:
    """Everything that stops ``key`` being used with the template behind ``plan``.

    Empty when the key fits. A key is **never** truncated, padded or relabelled
    to make it fit: a key with 50 answers against a 100-question paper is a key
    for a different paper, and marking the overlap would produce marks that
    look ordinary and are wrong for everybody.
    """
    issues: list[str] = []
    if key.question_count != plan.question_count:
        issues.append(
            f"This key has {key.question_count} answer(s); the active template "
            f"defines {plan.question_count} questions."
        )
    elif key.first_question != plan.first_question:
        issues.append(
            f"This key numbers its questions from {key.first_question}; the "
            f"active template numbers them from {plan.first_question}."
        )
    unknown = sorted(
        {character for character in key.answers if character not in plan.labels}
        - {BLANK}
    )
    if unknown:
        issues.append(
            "This key uses answer choices the active template does not offer: "
            f"{', '.join(unknown)} (allowed: {', '.join(plan.labels)})."
        )
    outside = sorted(number for number in key.wrong_questions if number not in plan.numbers)
    if outside:
        issues.append(
            "Full-credit question(s) outside the active template's range: "
            f"{format_question_numbers(outside)}."
        )
    return tuple(issues)


# ----------------------------------------------------------------------
# Reading a key off a scanned solution sheet
# ----------------------------------------------------------------------
class ReadingKind(StrEnum):
    """What recognition found for one question of a solution sheet."""

    CLEAR = "clear"
    BLANK = "blank"
    MULTIPLE = "multiple"
    UNREADABLE = "unreadable"

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            ReadingKind.CLEAR: "Clear mark",
            ReadingKind.BLANK: "No mark detected",
            ReadingKind.MULTIPLE: "Multiple marks",
            ReadingKind.UNREADABLE: "Not recognised",
        }[self]


@dataclass(frozen=True, slots=True)
class QuestionReading:
    """One question as a solution sheet was read.

    Attributes:
        number: The printed question number.
        raw: What the engine returned, uppercased - ``"B"``, ``"B-C"``, ``""``.
        kind: How that reads as an answer-key entry.
        confidence: The engine's own confidence for the question.
        low_confidence: A single clear mark the engine still flagged for review.
    """

    number: int
    raw: str
    kind: ReadingKind
    confidence: float = 0.0
    low_confidence: bool = False

    @property
    def describe(self) -> str:
        """``"Multiple marks B + C"``, ``"No mark detected"``, ``"B"``."""
        if self.kind is ReadingKind.MULTIPLE:
            return f"Multiple marks {' + '.join(part for part in self.raw.split('-') if part)}"
        if self.kind is ReadingKind.CLEAR:
            return self.raw + (" (low confidence)" if self.low_confidence else "")
        return self.kind.label


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
    readings: tuple[QuestionReading, ...] = ()
    registration_message: str = ""

    @property
    def blanks(self) -> tuple[int, ...]:
        """Questions on which no mark was detected."""
        return tuple(item.number for item in self.readings if item.kind is ReadingKind.BLANK)

    @property
    def multiples(self) -> tuple[int, ...]:
        """Questions carrying more than one mark."""
        return tuple(
            item.number for item in self.readings if item.kind is ReadingKind.MULTIPLE
        )

    @property
    def low_confidence(self) -> tuple[int, ...]:
        """Single, readable marks the engine nonetheless flagged for review."""
        return tuple(item.number for item in self.readings if item.low_confidence)

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
    views = {item.number: item for item in result.answers}
    known = set(plan.labels)

    characters: list[str] = []
    unreadable: list[int] = []
    readings: list[QuestionReading] = []
    for number in plan.numbers:
        view = views.get(number)
        raw = ((view.value if view is not None else "") or "").strip().upper()
        confidence = float(view.confidence) if view is not None else 0.0
        flagged = bool(view.needs_review) if view is not None else False
        if not registered:
            kind = ReadingKind.UNREADABLE
        elif not raw:
            kind = ReadingKind.BLANK
        elif "-" in raw or len(raw) > 1:
            kind = ReadingKind.MULTIPLE
        elif raw in known:
            kind = ReadingKind.CLEAR
        else:
            kind = ReadingKind.UNREADABLE
        if kind is ReadingKind.CLEAR:
            characters.append(raw)
        else:
            characters.append(BLANK if kind is ReadingKind.BLANK else MULTIPLE)
            unreadable.append(number)
        readings.append(
            QuestionReading(
                number=number,
                raw=raw,
                kind=kind,
                confidence=confidence,
                low_confidence=kind is ReadingKind.CLEAR and flagged,
            )
        )

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
        # What the paper says, canonicalised: a raw *physical* reading. The
        # logical set is decided by `check_sheet_set`.
        set_code=canonical_code(result.set_code_value),
        unreadable=tuple(unreadable),
        registered=registered,
        warnings=tuple(result.warnings),
        readings=tuple(readings),
        registration_message=result.registration_message,
    )


# ----------------------------------------------------------------------
# Whether a solution sheet's printed set agrees with the chosen one
# ----------------------------------------------------------------------
class SetCodeCheck(StrEnum):
    """How the set code read off a solution sheet relates to the chosen set."""

    MATCH = "match"
    """The sheet reads the set the operator chose."""

    BLANK = "blank"
    """Nothing, or nothing legible, in the sheet's set field."""

    MISMATCH = "mismatch"
    """The sheet reads a different set. Needs an explicit decision."""

    UNREPRESENTABLE = "unrepresentable"
    """The template's set field cannot print the chosen code at all.

    A project may number its sets 10/11/12 while the sheet's set field only
    offers A-D, or have no set field. The sheet's answers are still valid
    evidence; only the set cannot be confirmed from the paper.
    """

    @property
    def blocks_without_decision(self) -> bool:
        """Whether importing needs the operator to choose a set explicitly."""
        return self is SetCodeCheck.MISMATCH


@dataclass(frozen=True, slots=True)
class SetCodeVerdict:
    """The outcome of comparing a sheet's set field with the chosen set.

    Attributes:
        check: See :class:`SetCodeCheck`.
        selected: The logical set the operator chose.
        read: What the sheet's set field reads - the **physical** mark, as
            recognised.
        read_is_defined: Whether :attr:`read` names one of the project's sets.
        message: One sentence for the review panel.
        read_set: The logical set :attr:`read` names (``"10"`` for a sheet
            reading ``A`` when Set 10 is printed as ``A``), or ``""``.
        printed: What the chosen set is printed as on the sheet.
    """

    check: SetCodeCheck
    selected: str
    read: str
    read_is_defined: bool = False
    message: str = ""
    read_set: str = ""
    printed: str = ""

    @property
    def read_label(self) -> str:
        """The read set for a sentence: ``"10 (A on sheet)"``, or just the reading."""
        if self.read_set and not same_set(self.read_set, self.read):
            return f"{self.read_set} ({self.read} on sheet)"
        return self.read_set or self.read

    @property
    def sheet_set(self) -> str:
        """The logical set to file a key under "as marked on the sheet"."""
        return self.read_set or self.read


def set_field_symbols(template: OmrTemplate) -> tuple[tuple[str, ...], int] | None:
    """The set-code field's symbols and position count, or ``None`` without one."""
    from omr_scanner.domain.template import FieldType, GridFieldDefinition

    for zone in template.zones:
        field_def = zone.field
        if isinstance(field_def, GridFieldDefinition) and field_def.type is FieldType.SET_CODE:
            return tuple(field_def.symbols), field_def.character_count
    return None


def can_print_set_code(template: OmrTemplate, code: str) -> bool:
    """Whether the template's set field can physically carry ``code``.

    True when ``code`` splits into at most as many of the field's symbols as it
    has positions - ``"10"`` on a two-position digit field, or on a
    one-position field whose symbols include ``"10"``.
    """
    found = set_field_symbols(template)
    if found is None or not code:
        return False
    symbols, positions = found
    wanted = canonical_code(code)
    options = {canonical_code(symbol) for symbol in symbols}

    def fits(rest: str, remaining: int) -> bool:
        if not rest:
            return True
        if remaining == 0:
            return False
        return any(
            rest.startswith(symbol) and fits(rest[len(symbol):], remaining - 1)
            for symbol in options
            if symbol
        )

    return fits(wanted, positions)


def check_sheet_set(
    template: OmrTemplate,
    *,
    selected: str,
    read: str,
    defined: Sequence[str] = (),
    identity: SetIdentity | None = None,
) -> SetCodeVerdict:
    """Compare the set read off a solution sheet with the set the operator chose.

    The operator's choice is authoritative for a solution sheet - they are
    telling OMRFlow which paper this key answers - but a sheet that clearly
    says otherwise must not be filed silently under the chosen set.

    Args:
        template: The project's template.
        selected: The logical set chosen.
        read: What the sheet's set field reads (physical).
        defined: The set codes the page offers, used when ``identity`` is not
            given (sets with no physical marks).
        identity: The project's sets with their physical marks. The read mark
            is compared with **the chosen set's printed mark**, so a Set 10
            printed as ``A`` matches a sheet reading ``A``.

    Comparisons go through :mod:`omr_scanner.domain.set_identity`.
    """
    if identity is None:
        from omr_scanner.domain.exam_sets import ExamSet

        identity = SetIdentity(
            ExamSet(set_id=code, code=code) for code in dict.fromkeys(defined) if code
        )
    chosen = selected.strip()
    seen = read.strip()
    if seen and set(seen) <= RESERVED_SYMBOLS:
        # Every position blank ("__") or unresolved ("??"): nothing legible.
        seen = ""
    chosen_set = identity.logical(chosen)
    printed = chosen_set.printed_as if chosen_set is not None else chosen
    mapped = not same_set(printed, chosen)
    read_found = identity.for_reading(seen) if seen else None
    read_set = read_found.code if read_found is not None else ""
    is_defined = read_found is not None
    if not can_print_set_code(template, printed):
        target = f"Set {chosen} (printed as {printed})" if mapped else f"Set {chosen}"
        return SetCodeVerdict(
            SetCodeCheck.UNREPRESENTABLE,
            chosen,
            seen,
            is_defined,
            message=(
                f"This template's set field cannot print {target}, so the "
                "set marked on the sheet could not be used to confirm it"
                + (f" (the sheet reads '{seen}')" if seen else "")
                + ". The answers are imported into the set you selected."
            ),
            read_set=read_set,
            printed=printed,
        )
    if not seen:
        return SetCodeVerdict(
            SetCodeCheck.BLANK,
            chosen,
            seen,
            message=(
                "No set is marked on this sheet (or it could not be read). The "
                f"answers are imported into Set {chosen}, the set you selected."
            ),
            printed=printed,
        )
    same_chosen = (
        read_found is not None
        and chosen_set is not None
        and read_found.set_id == chosen_set.set_id
    )
    if same_chosen or same_set(seen, printed):
        return SetCodeVerdict(
            SetCodeCheck.MATCH, chosen, seen, is_defined,
            message=(
                f"The sheet is marked {seen}, which is Set {chosen}, matching your selection."
                if mapped
                else f"The sheet is marked Set {seen}, matching your selection."
            ),
            read_set=read_set or chosen,
            printed=printed,
        )
    verdict = SetCodeVerdict(
        SetCodeCheck.MISMATCH, chosen, seen, is_defined, read_set=read_set, printed=printed
    )
    return replace(
        verdict,
        message=(
            f"This sheet appears to be Set {verdict.read_label}, but you selected "
            f"Set {chosen}"
            + (f" (printed as {printed})" if mapped else "")
            + "."
            + ("" if is_defined else f" Set {seen} is not one of this project's sets.")
        ),
    )
