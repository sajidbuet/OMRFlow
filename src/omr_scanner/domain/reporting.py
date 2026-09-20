"""Vocabulary for result reports: ranking, safe text, and readiness.

Purpose:
    The words Phase 9 reasons in - how a rank is computed, how the Excel
    ``RANK.EQ`` formula this application writes is built, what makes a
    candidate's report display text safe to put in a spreadsheet cell, and
    what a "the report cannot be exported yet" finding looks like. Pure data
    and pure functions: no openpyxl, no database, no Qt, no file access.

Why ranking lives here, not in a service:
    :func:`compute_ranks` is a function of its arguments and nothing else -
    the same reason :func:`omr_scanner.domain.scoring.score_answers` is pure.
    The application's own rank has to be checked against the Excel formula's
    ``RANK.EQ`` semantics (the phase brief calls this out explicitly), and
    that comparison is only meaningful if the application's side is
    deterministic and reproducible from stored inputs, not something that
    reads a database.

What does NOT belong here:
    * Reading a template workbook, writing a report, or deciding whether a
      set is ready to export. Those are services (:mod:`omr_scanner.services.
      report_template`, :mod:`omr_scanner.services.report_readiness`) and the
      ``reporting`` package's Excel/PDF mechanics.
    * Candidate data. This module describes shapes, never people.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from fractions import Fraction
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# ----------------------------------------------------------------------
# Absence display
# ----------------------------------------------------------------------
ABSENT_MARK_DISPLAY = "ABSENT"
"""What an absent candidate's marks cell shows in a generated report.

The *recognised* absence tokens a template may already use are wider
(``ABSENT``, ``ABS``, in any case) - see
:func:`omr_scanner.domain.reconciliation.is_absent_token` - but the value this
application *writes* is always this one spelling, so every set's reports read
the same way regardless of which spelling that set's own template happened to
use beforehand.
"""

ABSENT_RANK_DISPLAY = "---"
"""What an absent candidate's Merit/Rank cell shows, both as a literal value
and as what the generated ``RANK.EQ`` formula evaluates to for that row."""

ABSENCE_TOKENS_FOR_FORMULA: tuple[str, ...] = ("ABSENT", "ABS")
"""Tokens the generated rank formula treats as "this row is absent, not zero".

Matches :func:`omr_scanner.domain.reconciliation.is_absent_token`'s case-
insensitive vocabulary, spelled out here because the formula text has to name
them literally - a spreadsheet formula cannot call a Python function.
"""


# ----------------------------------------------------------------------
# Ranking
# ----------------------------------------------------------------------
def compute_ranks(
    scores: Sequence[tuple[str, Fraction | None]],
) -> dict[str, int | None]:
    """Assign standard competition ranks ("1224") to a set of candidates.

    Args:
        scores: ``(candidate_id, final_score)`` pairs. A score of ``None``
            means the candidate does not participate in ranking - an
            absentee, or anyone otherwise excluded - and always receives
            rank ``None``, never a number that would let them be sorted
            among the ranked.

    Returns:
        Every candidate's rank, keyed by ID. Two candidates with an equal
        score receive an equal rank, and the rank after a tie skips the
        number of tied places - "90, 88, 88, 85" ranks "1, 2, 2, 4" - which is
        exactly what Excel's ``RANK.EQ(value, range, 0)`` computes for
        descending order. This is verified against a hand-built ``RANK.EQ``
        table in ``tests/unit/test_reporting.py``.

    Determinism:
        A pure function of its argument. Feed it the same scores in the same
        order and it produces the same ranks - the property that lets the
        phase brief's requirement ("application rank must match the Excel
        formula") be an assertion rather than an inspection.
    """
    participants = sorted(
        ((candidate_id, score) for candidate_id, score in scores if score is not None),
        key=lambda item: item[1],
        reverse=True,
    )
    ranks: dict[str, int | None] = {candidate_id: None for candidate_id, _ in scores}
    previous_score: Fraction | None = None
    previous_rank = 0
    for position, (candidate_id, score) in enumerate(participants, start=1):
        # Only a genuine tie keeps the previous rank; the position itself
        # always advances, which is what makes the rank *after* a tie skip
        # the tied places rather than merely incrementing by one.
        rank = previous_rank if score == previous_score else position
        ranks[candidate_id] = rank
        previous_score = score
        previous_rank = rank
    return ranks


def column_letter(index: int) -> str:
    """Convert a 1-based column index to its Excel letter (1 -> ``A``).

    A small, dependency-free equivalent of ``openpyxl.utils.get_column_letter``
    - kept here, rather than imported, so this module stays free of a
    third-party import: the rank-formula text it builds is plain string
    arithmetic, not a spreadsheet operation.

    Raises:
        ValueError: ``index`` is less than 1.
    """
    if index < 1:
        raise ValueError(f"column index must be 1 or greater, got {index}")
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def rank_formula(
    *,
    marks_column: str,
    first_data_row: int,
    last_data_row: int,
    row: int,
    absence_tokens: Sequence[str] = ABSENCE_TOKENS_FOR_FORMULA,
) -> str:
    """Build the ``RANK.EQ`` formula for one Rollwise row.

    Args:
        marks_column: The Excel column letter holding marks (derived from the
            template's own mapping - never hard-coded).
        first_data_row: The first *candidate* row, excluding the header -
            derived from the template's actual layout, never a fixed ``2``.
        last_data_row: The last candidate row - derived from the template's
            actual row count, never a fixed ``230``.
        row: The row this formula is written into.
        absence_tokens: Case-insensitive text values that mean "absent, not a
            number" rather than "excluded from ranking because blank".

    Returns:
        A formula equivalent to the phase brief's worked example, generalised
        over the mapped column and the real first/last row:

        ``=IF(OR(UPPER(TRIM(D2))="ABSENT",UPPER(TRIM(D2))="ABS"),"---",
        IF(ISNUMBER(D2),RANK.EQ(D2,$D$2:$D$230,0),""))``

    Why a blank cell is never ranked:
        ``ISNUMBER`` is false for both an absence token and an empty cell, so
        a candidate whose mark has not yet been entered - as opposed to one
        recorded absent - gets ``""`` (blank), never a rank. A blank
        participating in ``RANK.EQ`` would either error or silently rank an
        unresolved candidate among the scored ones; neither is acceptable,
        which is why :mod:`omr_scanner.services.report_readiness` blocks
        export while any present candidate lacks a final score in the first
        place - this formula is the second line of defence, not the first.
    """
    cell = f"{marks_column}{row}"
    value_range = f"${marks_column}${first_data_row}:${marks_column}${last_data_row}"
    absence_checks = ",".join(
        f'UPPER(TRIM({cell}))="{token.upper()}"' for token in absence_tokens
    )
    return (
        f"=IF(OR({absence_checks}),\"{ABSENT_RANK_DISPLAY}\","
        f"IF(ISNUMBER({cell}),RANK.EQ({cell},{value_range},0),\"\"))"
    )


# ----------------------------------------------------------------------
# Spreadsheet-injection safety
# ----------------------------------------------------------------------
_FORMULA_TRIGGER_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@")
"""Characters that make Excel/LibreOffice interpret a cell as a formula.

The tab character (``\\t``) and carriage return can also trigger this in some
readers; they are stripped rather than escaped, since a tab inside a report
title or a candidate name is never meaningful text to begin with.
"""


def safe_cell_text(value: str | None) -> str:
    """Return ``value`` as text that a spreadsheet reader cannot execute.

    Args:
        value: User-controlled text destined for a **text** cell - a report
            title, a header, a footer, a candidate name. Never call this on
            a numeric mark or on a formula this application generated itself
            (:func:`rank_formula`); prefixing ``RANK.EQ(...)`` with a quote
            would turn the working formula into inert text.

    Returns:
        ``value`` unchanged, unless it begins with ``=``, ``+``, ``-`` or
        ``@`` (after stripping leading/trailing whitespace and control
        characters), in which case a leading apostrophe is added. Excel and
        LibreOffice both render a leading apostrophe as "this cell is text,
        not a formula" and do not display the apostrophe itself.

    Why this matters here specifically:
        A candidate name is not typed by the operator - it comes from a
        roster file, or from OCR - so "nobody would type a formula" is not a
        safety argument. A name that happens to start with ``=`` (a
        transliteration artefact, a deliberately hostile roster row) must not
        become a formula the moment somebody opens the generated report in
        Excel.
    """
    text = "" if value is None else str(value)
    cleaned = "".join(character for character in text if character not in "\t\r\n")
    if cleaned and cleaned[0] in _FORMULA_TRIGGER_PREFIXES:
        return "'" + cleaned
    return cleaned


# ----------------------------------------------------------------------
# Safe filenames
# ----------------------------------------------------------------------
_INVALID_FILENAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
"""The union of characters Windows and POSIX both forbid in a filename -
the same character class :mod:`omr_scanner.domain.project` rejects in a
project name, for the same cross-platform reason: a report generated on one
OS must still be a legal filename on another."""

MAX_OUTPUT_FILENAME_LENGTH = 180
"""Conservative ceiling well under Windows' 260-character path limit, leaving
room for a long project directory path in front of the filename."""


def safe_filename_component(text: str, *, fallback: str = "report") -> str:
    """Sanitise one piece of a filename (a project name, a set code, ...).

    Invalid characters become ``_``; the result is trimmed of the trailing
    dots and spaces Windows silently strips (so the sanitised name and the
    file actually written never disagree), and truncated to a sane length. An
    input that sanitises to nothing (an empty string, or one made entirely of
    invalid characters) falls back to ``fallback`` rather than producing a
    blank path component.
    """
    cleaned = _INVALID_FILENAME_CHARACTERS.sub("_", text).strip().rstrip(". ")
    if not cleaned or not cleaned.strip("_"):
        # Either nothing survived, or nothing but underscores did - "///"
        # sanitises to "___", which is technically non-empty but names
        # nothing. Both cases mean the input had no usable text.
        cleaned = fallback
    return cleaned[:MAX_OUTPUT_FILENAME_LENGTH]


# ----------------------------------------------------------------------
# Total-marks header
# ----------------------------------------------------------------------
def total_header_for(maximum_score: Fraction) -> str:
    """Return the marks-column header for a paper marked out of ``maximum_score``.

    ``Total (90)`` for a whole number; ``Total (72.5)`` for a paper whose
    scoring policy does not land on a whole number (a wrong-question
    withdrawal, an unusual mark-per-question value). Never silently rounds -
    a header that lies about the maximum is worse than one with a decimal in
    it.
    """
    from omr_scanner.domain.scoring import format_mark

    if maximum_score == maximum_score.__trunc__():
        return f"Total ({maximum_score.__trunc__()})"
    return f"Total ({format_mark(maximum_score)})"


def header_matches_maximum(header_text: str, maximum_score: Fraction) -> bool:
    """Whether ``header_text`` already states the correct maximum.

    Tolerant of the exact digits appearing anywhere in the header (`"Total
    (90)"`, `"Marks (out of 90)"`), because the supplied template's own
    wording is preserved wherever it is already correct (see
    :mod:`omr_scanner.services.report_template`) - this only has to catch the
    case where the number is *wrong*, not police the wording.
    """
    digits = maximum_score.__trunc__() if maximum_score == maximum_score.__trunc__() else None
    if digits is not None:
        return str(digits) in header_text
    from omr_scanner.domain.scoring import format_mark

    return format_mark(maximum_score) in header_text


# ----------------------------------------------------------------------
# Readiness
# ----------------------------------------------------------------------
class ReadinessIssueKind(StrEnum):
    """Why a set is not ready for final export.

    Every member describes something a person must look at; none is ever
    resolved by guessing, matching :class:`omr_scanner.domain.scoring.BlockReason`'s
    own design.
    """

    NO_TEMPLATE = "no_template"
    TEMPLATE_UNREADABLE = "template_unreadable"
    TEMPLATE_MAPPING_AMBIGUOUS = "template_mapping_ambiguous"
    NO_VERIFIED_KEY = "no_verified_key"
    SCORING_INCOMPLETE = "scoring_incomplete"
    RECONCILIATION_INCOMPLETE = "reconciliation_incomplete"
    CANDIDATE_NOT_IN_PROJECT = "candidate_not_in_project"
    CANDIDATE_MISSING_FROM_TEMPLATE = "candidate_missing_from_template"
    DUPLICATE_ROLL_IN_TEMPLATE = "duplicate_roll_in_template"
    DUPLICATE_REGISTERED_CANDIDATE = "duplicate_registered_candidate"
    SET_MISMATCH = "set_mismatch"
    ABSENTEE_STATUS_MISMATCH = "absentee_status_mismatch"
    PRESENT_WITHOUT_SCORE = "present_without_score"
    ABSENT_WITH_UNRESOLVED_SCRIPT = "absent_with_unresolved_script"
    UNRESOLVED_EXCEPTION = "unresolved_exception"
    STALE_RESULT = "stale_result"
    RANK_MISMATCH = "rank_mismatch"

    @property
    def label(self) -> str:
        """Operator-facing wording for the readiness checklist."""
        return {
            ReadinessIssueKind.NO_TEMPLATE: "No result template selected",
            ReadinessIssueKind.TEMPLATE_UNREADABLE: "The selected template could not be read",
            ReadinessIssueKind.TEMPLATE_MAPPING_AMBIGUOUS: (
                "The template's columns could not be identified automatically"
            ),
            ReadinessIssueKind.NO_VERIFIED_KEY: "No verified answer key for this set",
            ReadinessIssueKind.SCORING_INCOMPLETE: "Scoring has not been run for this set",
            ReadinessIssueKind.RECONCILIATION_INCOMPLETE: "Reconciliation is not complete",
            ReadinessIssueKind.CANDIDATE_NOT_IN_PROJECT: (
                "A template row is not a registered candidate"
            ),
            ReadinessIssueKind.CANDIDATE_MISSING_FROM_TEMPLATE: (
                "A registered candidate is missing from the template"
            ),
            ReadinessIssueKind.DUPLICATE_ROLL_IN_TEMPLATE: (
                "The template lists the same Roll No. more than once"
            ),
            ReadinessIssueKind.DUPLICATE_REGISTERED_CANDIDATE: (
                "The same candidate is registered more than once"
            ),
            ReadinessIssueKind.SET_MISMATCH: (
                "A candidate's script was read as a different set"
            ),
            ReadinessIssueKind.ABSENTEE_STATUS_MISMATCH: (
                "The template's absence marker disagrees with reconciliation"
            ),
            ReadinessIssueKind.PRESENT_WITHOUT_SCORE: (
                "A present candidate has no final score"
            ),
            ReadinessIssueKind.ABSENT_WITH_UNRESOLVED_SCRIPT: (
                "A candidate recorded absent has an unresolved script"
            ),
            ReadinessIssueKind.UNRESOLVED_EXCEPTION: (
                "A candidate has an unresolved reconciliation exception"
            ),
            ReadinessIssueKind.STALE_RESULT: (
                "A candidate's result needs recomputing before export"
            ),
            ReadinessIssueKind.RANK_MISMATCH: (
                "The application's rank does not match the generated formula"
            ),
        }[self]


@dataclass(frozen=True, slots=True)
class ReadinessIssue:
    """One reason a set cannot be finally exported yet.

    Attributes:
        kind: The category, for grouping and for tests.
        message: The full operator-facing sentence, already naming whatever
            it can safely name (a set code, a row number, a template column)
            - **never a candidate name or Roll No.** in the string used for
            logging; the GUI may show the roll where a human is already
            looking at the row (see ``docs/reconciliation.md`` on privacy).
        blocking: Whether this alone prevents **Final Export**. A non-blocking
            issue may still be shown in a preview/draft.
        roll: The candidate identifier this concerns, if any. Carried
            separately from ``message`` so a caller that must not display or
            log identifiers can omit it deliberately.
    """

    kind: ReadinessIssueKind
    message: str
    blocking: bool = True
    roll: str = ""


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """Every reason one set is, or is not, ready for final export."""

    set_code: str
    issues: tuple[ReadinessIssue, ...] = field(default_factory=tuple)

    @property
    def is_ready(self) -> bool:
        """Whether **Final Export** may proceed for this set."""
        return not any(item.blocking for item in self.issues)

    @property
    def has_warnings(self) -> bool:
        """Whether anything is worth showing even though export is allowed."""
        return any(not item.blocking for item in self.issues)

    def describe(self) -> tuple[str, ...]:
        """Every issue's message, for a checklist dialog."""
        return tuple(item.message for item in self.issues)

    def by_kind(self, kind: ReadinessIssueKind) -> tuple[ReadinessIssue, ...]:
        """Every issue of one kind - for a test asserting a specific defect."""
        return tuple(item for item in self.issues if item.kind is kind)
