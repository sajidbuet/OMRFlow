"""An authoritative candidate population, and the imperfect paperwork about it.

Purpose:
    Generate the *other half* of a synthetic examination: the attendance
    workbooks an examination office would hand over, complete with the clerical
    mistakes real ones contain, plus a ground truth recording what was actually
    true. Together with the synthetic scans from
    :mod:`omr_scanner.evaluation.synthetic_dataset` this makes the Phase 7
    reconciliation workflow testable end to end without a real examination.

Responsibilities:
    * :class:`ConflictKind` - the reconciliation situations this can stage.
    * :class:`ConflictRates` / :class:`ConflictProfile` - how often each occurs.
    * :func:`plan_population` - the decision: who exists, who attended, and
      which single conflict (if any) each candidate is subject to. A pure
      function of the configuration and the seed, returning plain data.
    * :func:`write_workbooks` - the set-specific ``.xlsx`` files.
    * :func:`write_ground_truth` - ``candidates.csv`` and
      ``reconciliation.csv``.

What does NOT belong here:
    * Rendering, recognition, or any image. This module decides *what is true*
      and *what the paperwork claims*; the renderer is told which sheets to
      draw and with which values.
    * Reconciling anything. The expected states here are derived from the plan,
      never by running
      :mod:`omr_scanner.services.reconciliation` over the output - a ground
      truth computed by the code under test is not a ground truth.

The three states, kept apart on purpose:
    Every candidate carries three separate stories, and conflating any two of
    them would destroy the point of the dataset::

        true state          what actually happened
        attendance state    what the workbook claims happened
        observed state      what is marked on the scan, if there is one

    A "present candidate wrongly marked absent" is exactly a disagreement
    between the first two, and the reconciliation engine's job is to surface
    it. :func:`write_ground_truth` writes all three, plus the state Phase 7
    should reach.

Why one conflict per candidate:
    Conflicts are assigned from a single draw per candidate rather than by
    rolling each probability independently, so a candidate cannot come out
    simultaneously a true absentee, missing a scan, and the owner of a
    duplicate script. Real datasets do contain compound failures, but a
    compound failure that arose *by accident* has no defensible expected state -
    and a qualification set whose own answer key is guesswork is worse than no
    qualification set. Deliberate combinations belong in a named stress case.

Why the workbook format is not invented here:
    :mod:`omr_scanner.services.candidate_import` already defines what OMRFlow
    accepts, and the bundled ``candidate_attendance_sample.xlsx`` shows the
    shape: a roster with a candidate-identifier column, a name, and a marks
    column in which ``ABS`` means absent and anything else - including a score
    or a blank - does not. This module writes that, so the files it produces
    are importable by the application rather than by a parallel reader written
    to match them.
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from omr_scanner.domain.reconciliation import AttendanceState, ReconciliationStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

CANDIDATES_FILENAME = "candidates.csv"
RECONCILIATION_FILENAME = "reconciliation.csv"
ATTENDANCE_DIRNAME = "attendance"

SHEET_TITLE = "Rollwise(All)"
"""The worksheet name the bundled sample workbook uses."""

HEADERS: tuple[str, ...] = ("Sl.No.", "Roll No.", "Name", "Total (90)", "Merit")
"""The sample workbook's own header row.

Copied from ``resources/templates/candidate_attendance_sample.xlsx`` rather
than chosen here. ``Roll No.`` and ``Name`` are both in
:data:`~omr_scanner.services.candidate_import.ID_HEADERS` /
:data:`~omr_scanner.services.candidate_import.NAME_HEADERS`, and ``Total (90)``
is the marks column the importer reads attendance from.
"""

ABSENT_TOKEN = "ABS"
"""What the marks column holds for an absentee.

:func:`~omr_scanner.services.candidate_import.attendance_from_cell` treats
``ABSENT``/``ABS`` in any case as absent and *everything else* - including a
blank - as not-absent. So a present candidate needs some other value, and
:data:`PRESENT_PLACEHOLDER` is it."""

PRESENT_PLACEHOLDER = "---"
"""What the marks column holds for a candidate who sat the paper.

The sample workbook's own placeholder for "no mark recorded yet", which is the
honest state before scoring: the generator knows who attended, not what they
scored."""

NAME_TEMPLATE = "Candidate {index:06d}"
"""Deliberately synthetic, and deliberately not name-shaped.

A generated dataset can end up attached to a bug report or a screenshot, so
nothing in it should be mistakable for a real person."""


class ConflictKind(StrEnum):
    """What, if anything, is wrong with one candidate's paperwork.

    Every member names a situation the Phase 7 reconciliation workflow is
    supposed to detect, and each maps to a state in
    :class:`~omr_scanner.domain.reconciliation.ReconciliationStatus` that
    already exists - none is invented here.
    """

    NONE = "none"
    """Present, scanned, correctly identified. The large majority."""

    TRUE_ABSENTEE = "true_absentee"
    """Genuinely did not attend, and the workbook says so. Not an error."""

    MARKED_ABSENT_BUT_PRESENT = "marked_absent_but_present"
    """Attended and has a script; the workbook wrongly says ``ABS``."""

    MARKED_PRESENT_BUT_ABSENT = "marked_present_but_absent"
    """Did not attend and has no script; the workbook does not say ``ABS``."""

    BLANK_CANDIDATE_ID = "blank_candidate_id"
    """Script exists, answers are filled, the identifier bubbles are not."""

    PARTIAL_CANDIDATE_ID = "partial_candidate_id"
    """Only some identifier digits are marked."""

    CANDIDATE_ID_MULTIPLE_MARK = "candidate_id_multiple_mark"
    """One identifier digit carries two marks."""

    WRONG_CANDIDATE_ID = "wrong_candidate_id"
    """The script is this candidate's, but a different identifier is marked."""

    UNKNOWN_CANDIDATE_ID = "unknown_candidate_id"
    """A script whose marked identifier is in no roster."""

    DUPLICATE_SCRIPT = "duplicate_script"
    """Two scripts carry this candidate's identifier."""

    MISSING_SCAN = "missing_scan"
    """Attended, the workbook agrees, and no scan reached the batch."""

    WRONG_SET = "wrong_set"
    """Correct identifier, but the set code of a different paper."""

    BLANK_SET = "blank_set"
    """Correct identifier, no set code marked at all."""


_REVIEW_FREE = frozenset(
    {ConflictKind.NONE, ConflictKind.TRUE_ABSENTEE}
)
"""The two outcomes a human never has to look at."""


@dataclass(frozen=True, slots=True)
class ConflictRates:
    """How often each conflict is staged, as a fraction of the population.

    Every field except :attr:`true_absentee` is a clerical or scanning error.
    They are drawn from one budget per candidate, so the values are shares of
    the population rather than independent probabilities - see the module
    docstring.

    Attributes:
        true_absentee: Genuine non-attendance. Not an error, and deliberately
            the largest of these by default: a cohort with no absentees is not
            a realistic test of an attendance workflow.
    """

    true_absentee: float = 0.05

    marked_absent_but_present: float = 0.010
    marked_present_but_absent: float = 0.010
    blank_candidate_id: float = 0.005
    partial_candidate_id: float = 0.005
    candidate_id_multiple_mark: float = 0.0025
    wrong_candidate_id: float = 0.005
    unknown_candidate_id: float = 0.0025
    duplicate_script: float = 0.0025
    missing_scan: float = 0.005
    wrong_set: float = 0.005
    blank_set: float = 0.0025

    def error_total(self) -> float:
        """The share of the population subject to some clerical/scan error."""
        return (
            self.marked_absent_but_present
            + self.marked_present_but_absent
            + self.blank_candidate_id
            + self.partial_candidate_id
            + self.candidate_id_multiple_mark
            + self.wrong_candidate_id
            + self.unknown_candidate_id
            + self.duplicate_script
            + self.missing_scan
            + self.wrong_set
            + self.blank_set
        )

    def validate(self) -> None:
        """Raise if the rates cannot be satisfied.

        Raises:
            ValueError: A rate is negative, or the whole budget exceeds the
                population. Caught here rather than producing a dataset whose
                composition silently differs from what was asked for.
        """
        for name, value in self.as_mapping().items():
            if value < 0.0:
                raise ValueError(f"{name} must not be negative (got {value})")
        total = self.true_absentee + self.error_total()
        if total > 1.0:
            raise ValueError(
                f"the conflict rates total {total:.3f} of the population, which "
                "leaves no candidates for the normal matched case"
            )

    def as_mapping(self) -> dict[str, float]:
        """Every rate by name, for the manifest and the report."""
        return {
            "true_absentee": self.true_absentee,
            "marked_absent_but_present": self.marked_absent_but_present,
            "marked_present_but_absent": self.marked_present_but_absent,
            "blank_candidate_id": self.blank_candidate_id,
            "partial_candidate_id": self.partial_candidate_id,
            "candidate_id_multiple_mark": self.candidate_id_multiple_mark,
            "wrong_candidate_id": self.wrong_candidate_id,
            "unknown_candidate_id": self.unknown_candidate_id,
            "duplicate_script": self.duplicate_script,
            "missing_scan": self.missing_scan,
            "wrong_set": self.wrong_set,
            "blank_set": self.blank_set,
        }


class ConflictProfile(StrEnum):
    """Named presets for :class:`ConflictRates`."""

    NONE = "none"
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CUSTOM = "custom"

    def rates(self) -> ConflictRates:
        """The rates this profile stands for.

        :attr:`CUSTOM` returns the :attr:`NORMAL` rates as a starting point;
        the caller is expected to replace them with the user's own.
        """
        if self is ConflictProfile.NONE:
            return ConflictRates(
                true_absentee=0.0,
                marked_absent_but_present=0.0,
                marked_present_but_absent=0.0,
                blank_candidate_id=0.0,
                partial_candidate_id=0.0,
                candidate_id_multiple_mark=0.0,
                wrong_candidate_id=0.0,
                unknown_candidate_id=0.0,
                duplicate_script=0.0,
                missing_scan=0.0,
                wrong_set=0.0,
                blank_set=0.0,
            )
        if self is ConflictProfile.LOW:
            normal = ConflictRates()
            halved = {name: value / 2 for name, value in normal.as_mapping().items()}
            return ConflictRates(**halved)
        if self is ConflictProfile.HIGH:
            normal = ConflictRates()
            doubled = {
                name: value * 2 for name, value in normal.as_mapping().items()
            }
            return ConflictRates(**doubled)
        return ConflictRates()


@dataclass(frozen=True, slots=True)
class SyntheticCandidate:
    """One candidate: what is true, what the workbook says, what was scanned.

    Attributes:
        candidate_uid: Stable internal identity, independent of the roll number
            the scan happens to carry. Ground truth keys on this so a test does
            not have to resolve an identifier to know which candidate it means.
        roll: The candidate's real identifier, as registered.
        name: A synthetic display name.
        set_code: The paper the candidate is registered for.
        true_attendance: Whether they actually attended.
        conflict: The single situation staged for them.
        attendance_listed: Whether they appear in the workbook at all.
        attendance_status: What the workbook claims.
        scan_present: Whether a scan of their script exists in the dataset.
        observed_roll: The identifier marked on that scan, which is *not*
            always :attr:`roll` - that is the point of several conflicts.
            ``None`` when nothing usable is marked.
        observed_set: The set code marked on that scan, or ``None``.
        duplicate_of: For the second script of a duplicate pair, the uid of the
            candidate whose identifier it carries.
    """

    candidate_uid: str
    roll: str
    name: str
    set_code: str
    true_attendance: AttendanceState
    conflict: ConflictKind

    attendance_listed: bool = True
    attendance_status: AttendanceState = AttendanceState.PRESENT

    scan_present: bool = True
    observed_roll: str | None = None
    observed_set: str | None = None
    duplicate_of: str | None = None

    @property
    def expected_status(self) -> ReconciliationStatus:
        """The state Phase 7 should reach for this candidate.

        Derived from the staged conflict, never from running the reconciler.
        """
        return _EXPECTED_STATUS[self.conflict]

    @property
    def manual_review_required(self) -> bool:
        """Whether a human has to decide something before results are final."""
        return self.conflict not in _REVIEW_FREE


_EXPECTED_STATUS: Mapping[ConflictKind, ReconciliationStatus] = {
    ConflictKind.NONE: ReconciliationStatus.MATCHED,
    ConflictKind.TRUE_ABSENTEE: ReconciliationStatus.ABSENT_CONFIRMED,
    # The workbook says absent and a script exists - Phase 7's own name for it.
    ConflictKind.MARKED_ABSENT_BUT_PRESENT: ReconciliationStatus.ABSENT_WITH_SCRIPT,
    ConflictKind.MARKED_PRESENT_BUT_ABSENT: ReconciliationStatus.PRESENT_WITHOUT_SCRIPT,
    # Nothing usable is marked, so the script cannot be attributed yet. All
    # three identifier defects land in the same state by design: the engine
    # cannot tell a blank field from an unreadable one, and should not pretend
    # to. Which of the three it was is recorded in `conflict_type`.
    ConflictKind.BLANK_CANDIDATE_ID: ReconciliationStatus.UNRESOLVED_CANDIDATE_ID,
    ConflictKind.PARTIAL_CANDIDATE_ID: ReconciliationStatus.UNRESOLVED_CANDIDATE_ID,
    ConflictKind.CANDIDATE_ID_MULTIPLE_MARK: ReconciliationStatus.UNRESOLVED_CANDIDATE_ID,
    # Their script went somewhere else, so from the roster's point of view the
    # candidate attended and nothing arrived. The stray script is reported
    # separately, as an unmatched sheet.
    ConflictKind.WRONG_CANDIDATE_ID: ReconciliationStatus.PRESENT_WITHOUT_SCRIPT,
    ConflictKind.UNKNOWN_CANDIDATE_ID: ReconciliationStatus.UNKNOWN_ID,
    ConflictKind.DUPLICATE_SCRIPT: ReconciliationStatus.DUPLICATE_SCRIPT,
    ConflictKind.MISSING_SCAN: ReconciliationStatus.PRESENT_WITHOUT_SCRIPT,
    # A set disagreement does not stop the script being matched to its owner;
    # it stops it being scored against the right key, which is a separate
    # decision the Answer Key stage surfaces. Matched, but not finished with.
    ConflictKind.WRONG_SET: ReconciliationStatus.MATCHED,
    ConflictKind.BLANK_SET: ReconciliationStatus.MATCHED,
}


@dataclass(frozen=True, slots=True)
class Population:
    """An authoritative roster and everything derived from it.

    Attributes:
        candidates: Every registered candidate, in roll order.
        stray_sheets: Scripts that belong to no registered candidate - the
            second half of a duplicate pair, and scripts marked with an
            unknown identifier. Kept apart from :attr:`candidates` because they
            are sheets, not people.
        seed: The master seed the whole plan derives from.
        rates: The rates actually applied.
    """

    candidates: tuple[SyntheticCandidate, ...]
    stray_sheets: tuple[SyntheticCandidate, ...] = ()
    seed: int = 0
    rates: ConflictRates = field(default_factory=ConflictRates)

    def by_set(self) -> dict[str, list[SyntheticCandidate]]:
        """Registered candidates grouped by the set they are registered for."""
        grouped: dict[str, list[SyntheticCandidate]] = {}
        for candidate in self.candidates:
            grouped.setdefault(candidate.set_code, []).append(candidate)
        return grouped

    def sheets_to_render(self) -> tuple[SyntheticCandidate, ...]:
        """Every script the image generator should draw, in order."""
        return tuple(
            entry
            for entry in (*self.candidates, *self.stray_sheets)
            if entry.scan_present
        )

    def counts(self) -> dict[str, int]:
        """How many of each conflict were actually staged.

        Actual, not requested: a rate of 0.25 per cent over 100 candidates
        cannot produce a quarter of a sheet, and a report that quoted the
        request rather than the outcome would be describing a dataset that was
        never generated.
        """
        tally = {kind.value: 0 for kind in ConflictKind}
        for candidate in self.candidates:
            tally[candidate.conflict.value] += 1
        return tally


def _quota(rate: float, population: int) -> int:
    """How many candidates a rate claims, rounded to whole people."""
    return max(0, round(rate * population))


def plan_population(
    *,
    count: int,
    set_codes: Sequence[str],
    seed: int,
    rates: ConflictRates | None = None,
    first_roll: int = 10000001,
    roll_digits: int | None = None,
    include_edge_cases: bool = True,
) -> Population:
    """Decide who exists, who attended, and what went wrong.

    Args:
        count: Registered candidates.
        set_codes: The question-paper sets, distributed round-robin so every
            set gets a comparable share and the assignment is reproducible.
        seed: Master seed.
        rates: How often each conflict occurs. Defaults to
            :meth:`ConflictProfile.NORMAL`'s rates.
        first_roll: The lowest registered identifier.
        roll_digits: Width to zero-pad identifiers to. Defaults to the width of
            the highest roll, so a cohort starting at 10000001 gets 8 digits.
        include_edge_cases: Guarantee one of every conflict, even when the
            rates are too low to produce one at this population size. This is
            what makes a 100-sheet dataset a complete reconciliation test
            rather than a sample of the common cases.

    Returns:
        The :class:`Population`, fully decided. No file is written and no image
        is drawn; this is the plan everything else is derived from.

    Raises:
        ValueError: ``count`` is not positive, ``set_codes`` is empty, or the
            rates cannot be satisfied.

    Deterministic in the strong sense the brief requires: the same arguments
    give the same plan, and the plan is decided before any sheet is rendered,
    so a multi-worker render cannot influence it.
    """
    if count <= 0:
        raise ValueError("count must be positive")
    if not set_codes:
        raise ValueError("at least one set code is required")
    applied = rates if rates is not None else ConflictRates()
    applied.validate()

    digits = roll_digits if roll_digits is not None else len(str(first_roll + count - 1))
    rng = random.Random(seed)

    assignments = _assign_conflicts(
        count=count,
        rates=applied,
        rng=rng,
        include_edge_cases=include_edge_cases,
    )

    candidates: list[SyntheticCandidate] = []
    strays: list[SyntheticCandidate] = []
    for index in range(count):
        roll = f"{first_roll + index:0{digits}d}"
        candidate = SyntheticCandidate(
            candidate_uid=f"C{index + 1:06d}",
            roll=roll,
            name=NAME_TEMPLATE.format(index=index + 1),
            set_code=set_codes[index % len(set_codes)],
            true_attendance=AttendanceState.PRESENT,
            conflict=assignments[index],
        )
        resolved, stray = _apply_conflict(
            candidate,
            set_codes=set_codes,
            first_roll=first_roll,
            count=count,
            digits=digits,
            rng=rng,
        )
        candidates.append(resolved)
        if stray is not None:
            strays.append(stray)

    return Population(
        candidates=tuple(candidates),
        stray_sheets=tuple(strays),
        seed=seed,
        rates=applied,
    )


def _assign_conflicts(
    *,
    count: int,
    rates: ConflictRates,
    rng: random.Random,
    include_edge_cases: bool,
) -> list[ConflictKind]:
    """One conflict per candidate, by quota rather than by repeated rolling.

    Quotas, because independent per-candidate rolls make the composition of a
    small dataset a lottery - at 100 candidates a 0.25 per cent rate produces a
    duplicate script about a fifth of the time, so most runs would silently
    omit a case the dataset is supposed to cover. Filling exact quotas and then
    shuffling gives the same expected composition with none of the variance,
    and keeps every conflict mutually exclusive by construction.
    """
    planned: list[ConflictKind] = []
    for kind, rate in (
        (ConflictKind.TRUE_ABSENTEE, rates.true_absentee),
        (ConflictKind.MARKED_ABSENT_BUT_PRESENT, rates.marked_absent_but_present),
        (ConflictKind.MARKED_PRESENT_BUT_ABSENT, rates.marked_present_but_absent),
        (ConflictKind.BLANK_CANDIDATE_ID, rates.blank_candidate_id),
        (ConflictKind.PARTIAL_CANDIDATE_ID, rates.partial_candidate_id),
        (ConflictKind.CANDIDATE_ID_MULTIPLE_MARK, rates.candidate_id_multiple_mark),
        (ConflictKind.WRONG_CANDIDATE_ID, rates.wrong_candidate_id),
        (ConflictKind.UNKNOWN_CANDIDATE_ID, rates.unknown_candidate_id),
        (ConflictKind.DUPLICATE_SCRIPT, rates.duplicate_script),
        (ConflictKind.MISSING_SCAN, rates.missing_scan),
        (ConflictKind.WRONG_SET, rates.wrong_set),
        (ConflictKind.BLANK_SET, rates.blank_set),
    ):
        planned.extend([kind] * _quota(rate, count))

    if include_edge_cases:
        # One of everything, whatever the rates said. A dataset that omits a
        # case cannot be used to prove that case is handled.
        for kind in ConflictKind:
            if kind is not ConflictKind.NONE and kind not in planned:
                planned.append(kind)

    if len(planned) > count:
        # The guarantee above can overflow a very small population. Keep the
        # edge cases and drop the surplus of whichever kind is most numerous,
        # so coverage survives and only the *proportions* give way.
        planned = _trim_to(planned, count)

    planned.extend([ConflictKind.NONE] * (count - len(planned)))
    rng.shuffle(planned)
    return planned


def _trim_to(planned: list[ConflictKind], limit: int) -> list[ConflictKind]:
    """Drop duplicates of the commonest kinds until ``planned`` fits."""
    trimmed = list(planned)
    while len(trimmed) > limit:
        counts: dict[ConflictKind, int] = {}
        for kind in trimmed:
            counts[kind] = counts.get(kind, 0) + 1
        commonest = max(counts, key=lambda kind: (counts[kind], kind.value))
        if counts[commonest] == 1:
            # Every kind is down to one instance; coverage now costs more than
            # the size limit is worth, so drop from the end deterministically.
            trimmed.pop()
            continue
        trimmed.remove(commonest)
    return trimmed


def _apply_conflict(
    candidate: SyntheticCandidate,
    *,
    set_codes: Sequence[str],
    first_roll: int,
    count: int,
    digits: int,
    rng: random.Random,
) -> tuple[SyntheticCandidate, SyntheticCandidate | None]:
    """Derive the workbook and scan state implied by a candidate's conflict.

    Returns:
        The resolved candidate, and a stray sheet when the conflict produces a
        script that belongs to nobody on the roster.
    """
    kind = candidate.conflict
    present = AttendanceState.PRESENT
    absent = AttendanceState.ABSENT

    if kind is ConflictKind.NONE:
        return (
            replace(
                candidate,
                attendance_status=present,
                observed_roll=candidate.roll,
                observed_set=candidate.set_code,
            ),
            None,
        )

    if kind is ConflictKind.TRUE_ABSENTEE:
        return (
            replace(
                candidate,
                true_attendance=absent,
                attendance_status=absent,
                scan_present=False,
                observed_roll=None,
                observed_set=None,
            ),
            None,
        )

    if kind is ConflictKind.MARKED_ABSENT_BUT_PRESENT:
        # Attended, sat the paper, and the clerk ticked the wrong box.
        return (
            replace(
                candidate,
                attendance_status=absent,
                observed_roll=candidate.roll,
                observed_set=candidate.set_code,
            ),
            None,
        )

    if kind is ConflictKind.MARKED_PRESENT_BUT_ABSENT:
        return (
            replace(
                candidate,
                true_attendance=absent,
                attendance_status=present,
                scan_present=False,
                observed_roll=None,
                observed_set=None,
            ),
            None,
        )

    if kind in {
        ConflictKind.BLANK_CANDIDATE_ID,
        ConflictKind.PARTIAL_CANDIDATE_ID,
        ConflictKind.CANDIDATE_ID_MULTIPLE_MARK,
    }:
        # The script exists and the answers are real; only the identifier is
        # unusable. `observed_roll` is None because nothing the engine could
        # act on was marked - the raw marks are the renderer's business and are
        # recorded per sheet, not here.
        return (
            replace(
                candidate,
                attendance_status=present,
                observed_roll=None,
                observed_set=candidate.set_code,
            ),
            None,
        )

    if kind is ConflictKind.WRONG_CANDIDATE_ID:
        # A transposition a real candidate makes: the right script, the wrong
        # number. Deliberately *outside* the registered block, so it cannot
        # collide with another candidate and turn into an accidental duplicate
        # whose expected state nobody decided.
        stray_roll = f"{first_roll + count + 500 + _index_of(candidate):0{digits}d}"
        stray = replace(
            candidate,
            candidate_uid=f"{candidate.candidate_uid}-STRAY",
            observed_roll=stray_roll,
            observed_set=candidate.set_code,
            scan_present=True,
        )
        return (
            replace(
                candidate,
                attendance_status=present,
                scan_present=False,
                observed_roll=None,
                observed_set=None,
            ),
            stray,
        )

    if kind is ConflictKind.UNKNOWN_CANDIDATE_ID:
        # A script from nobody on this roster - a stray from another hall.
        unknown_roll = f"{first_roll + count + 9000 + _index_of(candidate):0{digits}d}"
        return (
            replace(
                candidate,
                attendance_status=present,
                observed_roll=unknown_roll,
                observed_set=candidate.set_code,
            ),
            None,
        )

    if kind is ConflictKind.DUPLICATE_SCRIPT:
        # Two physical scripts carrying one identifier: a rescan, or a
        # candidate who started a second sheet. Both images are kept.
        duplicate = replace(
            candidate,
            candidate_uid=f"{candidate.candidate_uid}-DUP",
            observed_roll=candidate.roll,
            observed_set=candidate.set_code,
            duplicate_of=candidate.candidate_uid,
            scan_present=True,
        )
        return (
            replace(
                candidate,
                attendance_status=present,
                observed_roll=candidate.roll,
                observed_set=candidate.set_code,
            ),
            duplicate,
        )

    if kind is ConflictKind.MISSING_SCAN:
        return (
            replace(
                candidate,
                attendance_status=present,
                scan_present=False,
                observed_roll=None,
                observed_set=None,
            ),
            None,
        )

    if kind is ConflictKind.WRONG_SET:
        others = [code for code in set_codes if code != candidate.set_code]
        marked = others[rng.randrange(len(others))] if others else candidate.set_code
        return (
            replace(
                candidate,
                attendance_status=present,
                observed_roll=candidate.roll,
                observed_set=marked,
            ),
            None,
        )

    # ConflictKind.BLANK_SET
    return (
        replace(
            candidate,
            attendance_status=present,
            observed_roll=candidate.roll,
            observed_set=None,
        ),
        None,
    )


def _index_of(candidate: SyntheticCandidate) -> int:
    """The candidate's ordinal, recovered from its uid.

    Used to keep every derived identifier distinct without another counter to
    keep in step with the roster.
    """
    return int(candidate.candidate_uid.lstrip("C").split("-")[0])


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------
def write_workbooks(population: Population, directory: Path) -> tuple[Path, ...]:
    """Write one attendance workbook per set. Returns the paths, in set order.

    The workbook is the shape
    :mod:`omr_scanner.services.candidate_import` reads: the sample's header
    row, one row per candidate registered for that set, and ``ABS`` in the
    marks column for whoever the workbook *claims* was absent - which, for the
    candidates carrying an attendance conflict, is deliberately not who
    actually was.
    """
    from openpyxl import Workbook

    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for set_code, members in sorted(population.by_set().items()):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = SHEET_TITLE
        sheet.append(list(HEADERS))
        for serial, candidate in enumerate(members, start=1):
            listed_absent = candidate.attendance_status is AttendanceState.ABSENT
            sheet.append(
                [
                    serial,
                    candidate.roll,
                    candidate.name,
                    ABSENT_TOKEN if listed_absent else PRESENT_PLACEHOLDER,
                    "---",
                ]
            )
        path = directory / f"Set_{set_code}_Attendance.xlsx"
        workbook.save(path)
        written.append(path)
    return tuple(written)


def write_ground_truth(population: Population, directory: Path) -> tuple[Path, Path]:
    """Write ``candidates.csv`` and ``reconciliation.csv``.

    Returns:
        The two paths, in that order.

    ``candidates.csv`` is the authoritative roster and carries only what was
    *true*. ``reconciliation.csv`` is the comparison: true state beside what
    the workbook claimed beside what the scan showed, and the state Phase 7
    should reach. Keeping the corrupted attendance out of the first file is
    deliberate - a truth file that already contains the errors is not a truth
    file.
    """
    directory.mkdir(parents=True, exist_ok=True)

    candidates_path = directory / CANDIDATES_FILENAME
    with candidates_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["candidate_uid", "roll", "name", "set_code", "true_attendance"]
        )
        for candidate in population.candidates:
            writer.writerow(
                [
                    candidate.candidate_uid,
                    candidate.roll,
                    candidate.name,
                    candidate.set_code,
                    candidate.true_attendance.value,
                ]
            )

    reconciliation_path = directory / RECONCILIATION_FILENAME
    with reconciliation_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "candidate_uid",
                "true_roll",
                "true_set",
                "true_attendance",
                "attendance_record_present",
                "attendance_status",
                "scan_present",
                "omr_roll",
                "omr_set",
                "conflict_type",
                "expected_reconciliation_state",
                "manual_review_required",
            ]
        )
        # Resolved once. Testing membership against the candidate tuple per row
        # would be a linear scan inside a loop over the same tuple, which at
        # the 100,000-candidate scale this generator is meant to reach is
        # several minutes of nothing.
        registered = {candidate.candidate_uid for candidate in population.candidates}
        for entry in (*population.candidates, *population.stray_sheets):
            is_person = entry.candidate_uid in registered
            writer.writerow(
                [
                    entry.candidate_uid,
                    entry.roll,
                    entry.set_code,
                    entry.true_attendance.value,
                    # A stray sheet is a script, not a person, so it appears in
                    # no workbook and these two columns are blank for it.
                    str(entry.attendance_listed).lower() if is_person else "",
                    entry.attendance_status.value if is_person else "",
                    str(entry.scan_present).lower(),
                    # Blank, never invented: an unresolved identifier has no
                    # value, and writing one would make the truth file agree
                    # with a guess.
                    entry.observed_roll or "",
                    entry.observed_set or "",
                    entry.conflict.value,
                    entry.expected_status.value,
                    str(entry.manual_review_required).lower(),
                ]
            )

    return candidates_path, reconciliation_path


def summarise(population: Population) -> dict[str, object]:
    """Counts for the generation report and the completion dialog.

    Intentionally staged conflicts are reported as coverage, not as failures -
    a dataset containing a duplicate script it was asked to contain is working
    exactly as intended.
    """
    present = sum(
        1
        for candidate in population.candidates
        if candidate.true_attendance is AttendanceState.PRESENT
    )
    return {
        "registered_candidates": len(population.candidates),
        "true_present": present,
        "true_absent": len(population.candidates) - present,
        "sheets_to_render": len(population.sheets_to_render()),
        "stray_sheets": len(population.stray_sheets),
        "sets": sorted(population.by_set()),
        "expected_conflicts": population.counts(),
        "seed": population.seed,
        "rates": population.rates.as_mapping(),
    }


def expected_states(population: Population) -> dict[str, ReconciliationStatus]:
    """Every candidate's expected reconciliation state, by uid.

    The comparison key for a later qualification run: recognise the images,
    import the workbooks, reconcile, and check the result against this.
    """
    return {
        entry.candidate_uid: entry.expected_status
        for entry in (*population.candidates, *population.stray_sheets)
    }


__all__ = [
    "ABSENT_TOKEN",
    "ATTENDANCE_DIRNAME",
    "CANDIDATES_FILENAME",
    "HEADERS",
    "PRESENT_PLACEHOLDER",
    "RECONCILIATION_FILENAME",
    "SHEET_TITLE",
    "ConflictKind",
    "ConflictProfile",
    "ConflictRates",
    "Population",
    "SyntheticCandidate",
    "expected_states",
    "plan_population",
    "summarise",
    "write_ground_truth",
    "write_workbooks",
]
