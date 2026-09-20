"""Decide whether one set's report may be finally exported, and why not.

Purpose:
    Cross-check a result template's roster against Phase 7's reconciliation
    and Phase 8's scoring, and turn every disagreement into a
    :class:`~omr_scanner.domain.reporting.ReadinessIssue` a person can act
    on - never a silently dropped row and never a Python traceback (phase
    brief §8, §32).

Scope:
    Pure comparison logic over already-read data. No file access (the
    template was already read by
    :mod:`omr_scanner.services.report_template`), no writing.

The rule this module exists to enforce:
    A final report must not silently conceal an inconsistency. Every check
    below produces a message an operator can read and act on, and
    :meth:`ReadinessReport.is_ready` is what **Final Export** is gated on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omr_scanner.domain.reconciliation import AttendanceState, ReconciliationStatus
from omr_scanner.domain.reporting import ReadinessIssue, ReadinessIssueKind, ReadinessReport
from omr_scanner.domain.scoring import ResultStatus

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from omr_scanner.domain.reconciliation import ReconciliationEntry
    from omr_scanner.services.report_template import TemplateRoster
    from omr_scanner.services.scoring_store import StoredResult


def evaluate(
    *,
    set_code: str,
    roster: TemplateRoster | None,
    entries: Sequence[ReconciliationEntry],
    results_by_candidate: Mapping[str, StoredResult],
    has_verified_key: bool,
) -> ReadinessReport:
    """Build the full readiness report for one set.

    Args:
        set_code: The set being checked.
        roster: The associated template's roster, or ``None`` when no
            template has been selected yet.
        entries: Every reconciliation entry for the project's active roster
            and batch - the whole cohort, not merely this set's rows; a
            candidate missing from the template but present in the project is
            exactly the defect §8 requires this function to catch.
        results_by_candidate: Every stored Phase 8 result, keyed by candidate
            ID.
        has_verified_key: Whether the set has a verified answer key.

    Returns:
        Every issue found. Blocking issues (the default) prevent **Final
        Export**; a handful - a stale result, a rank mismatch - are recorded
        as warnings a preview may still show (phase brief §8: "A preview/
        draft workflow may display the problems, but never present an
        incomplete report as successfully finalised").
    """
    issues: list[ReadinessIssue] = []

    if roster is None:
        issues.append(
            ReadinessIssue(
                ReadinessIssueKind.NO_TEMPLATE,
                f"Set {set_code} has no result template selected.",
            )
        )
        return ReadinessReport(set_code=set_code, issues=tuple(issues))

    if not has_verified_key:
        issues.append(
            ReadinessIssue(
                ReadinessIssueKind.NO_VERIFIED_KEY,
                f"Set {set_code} has no verified answer key.",
            )
        )

    entries_by_id = {entry.candidate_id: entry for entry in entries}
    template_rolls = set(roster.roll_numbers)

    issues.extend(_duplicate_roll_issues(roster))
    issues.extend(_duplicate_registration_issues(entries))
    issues.extend(
        _cross_reference_issues(
            roster=roster,
            entries_by_id=entries_by_id,
            template_rolls=template_rolls,
            results_by_candidate=results_by_candidate,
            set_code=set_code,
        )
    )
    issues.extend(_stale_result_issues(roster, results_by_candidate))
    return ReadinessReport(set_code=set_code, issues=tuple(issues))


def _duplicate_roll_issues(roster: TemplateRoster) -> list[ReadinessIssue]:
    """A Roll No. repeated within the template itself."""
    return [
        ReadinessIssue(
            ReadinessIssueKind.DUPLICATE_ROLL_IN_TEMPLATE,
            f"Template contains duplicate Roll No. {roll}.",
            roll=roll,
        )
        for roll in roster.duplicate_rolls
        if roll
    ]


def _duplicate_registration_issues(
    entries: Sequence[ReconciliationEntry],
) -> list[ReadinessIssue]:
    """The same candidate registered more than once in the project roster.

    Reconciliation itself keys one entry per candidate ID, so a true
    duplicate registration would already have collided there; this exists as
    a defence for whatever the roster importer did not catch, and is expected
    to be empty in the ordinary case.
    """
    seen: dict[str, int] = {}
    for entry in entries:
        if not entry.is_registered or not entry.candidate_id:
            continue
        seen[entry.candidate_id] = seen.get(entry.candidate_id, 0) + 1
    return [
        ReadinessIssue(
            ReadinessIssueKind.DUPLICATE_REGISTERED_CANDIDATE,
            f"Candidate {candidate_id} is registered more than once.",
            roll=candidate_id,
        )
        for candidate_id, count in seen.items()
        if count > 1
    ]


def _cross_reference_issues(
    *,
    roster: TemplateRoster,
    entries_by_id: Mapping[str, ReconciliationEntry],
    template_rolls: set[str],
    results_by_candidate: Mapping[str, StoredResult],
    set_code: str,
) -> list[ReadinessIssue]:
    """Every disagreement between the template, reconciliation and scoring."""
    issues: list[ReadinessIssue] = []

    for row in roster.rows:
        if not row.roll:
            continue
        entry = entries_by_id.get(row.roll)
        if entry is None or not entry.is_registered:
            issues.append(
                ReadinessIssue(
                    ReadinessIssueKind.CANDIDATE_NOT_IN_PROJECT,
                    f"Roll {row.roll} exists in the template but is not a "
                    "registered candidate in this project.",
                    roll=row.roll,
                )
            )
            continue

        if entry.needs_attention:
            issues.append(
                ReadinessIssue(
                    ReadinessIssueKind.UNRESOLVED_EXCEPTION,
                    f"Roll {row.roll} has an unresolved reconciliation "
                    f"exception ({entry.status.label}).",
                    roll=row.roll,
                )
            )

        template_absent = row.marks_says_absent
        actually_absent = entry.effective_attendance is AttendanceState.ABSENT
        if template_absent != actually_absent and not entry.needs_attention:
            # An entry already flagged as needing attention (e.g.
            # ABSENT_WITH_SCRIPT) would otherwise report this same
            # disagreement twice under two names.
            issues.append(
                ReadinessIssue(
                    ReadinessIssueKind.ABSENTEE_STATUS_MISMATCH,
                    f"Roll {row.roll}: the template marks this candidate "
                    f"{'absent' if template_absent else 'present'}, but "
                    f"reconciliation says {entry.effective_attendance.label.lower()}.",
                    roll=row.roll,
                )
            )

        if actually_absent and entry.status is ReconciliationStatus.ABSENT_WITH_SCRIPT:
            issues.append(
                ReadinessIssue(
                    ReadinessIssueKind.ABSENT_WITH_UNRESOLVED_SCRIPT,
                    f"Roll {row.roll} is recorded absent but has an "
                    "unresolved script.",
                    roll=row.roll,
                )
            )

        if not actually_absent:
            result = results_by_candidate.get(row.roll)
            if result is None or not result.has_mark:
                issues.append(
                    ReadinessIssue(
                        ReadinessIssueKind.PRESENT_WITHOUT_SCORE,
                        f"Roll {row.roll} is present but has no final score.",
                        roll=row.roll,
                    )
                )
            elif result.set_code and result.set_code != set_code:
                issues.append(
                    ReadinessIssue(
                        ReadinessIssueKind.SET_MISMATCH,
                        f"Roll {row.roll} is on the Set {set_code} template "
                        f"but was scored against Set {result.set_code}.",
                        roll=row.roll,
                    )
                )

    for entry in entries_by_id.values():
        if not entry.is_registered or entry.candidate_id in template_rolls:
            continue
        if entry.effective_attendance is AttendanceState.PRESENT:
            result = results_by_candidate.get(entry.candidate_id)
            if result is not None and result.set_code == set_code:
                issues.append(
                    ReadinessIssue(
                        ReadinessIssueKind.CANDIDATE_MISSING_FROM_TEMPLATE,
                        f"Roll {entry.candidate_id} was scored against Set "
                        f"{set_code} but is missing from that set's template.",
                        roll=entry.candidate_id,
                    )
                )

    return issues


def _stale_result_issues(
    roster: TemplateRoster, results_by_candidate: Mapping[str, StoredResult]
) -> list[ReadinessIssue]:
    """A scored candidate whose result needs recomputing first.

    Non-blocking: the phase brief allows a preview to show a stale mark, but
    **Final Export** must never present one as current, so this is a
    blocking issue precisely when the operator has said "export the final
    version" - the caller decides which. Here it is reported at all times;
    :meth:`ReadinessReport.by_kind` lets a caller treat it as a warning during
    preview and as a block for final export.
    """
    issues: list[ReadinessIssue] = []
    for row in roster.rows:
        result = results_by_candidate.get(row.roll)
        if result is not None and result.is_stale and result.status is ResultStatus.SCORED:
            issues.append(
                ReadinessIssue(
                    ReadinessIssueKind.STALE_RESULT,
                    f"Roll {row.roll}'s result needs recomputing before export.",
                    blocking=False,
                    roll=row.roll,
                )
            )
    return issues


def block_stale_results_for_final_export(report: ReadinessReport) -> ReadinessReport:
    """Promote every stale-result warning to a blocking issue.

    Used specifically by **Final Export** (never by a preview): the phase
    brief distinguishes "a preview may show problems" from "a final report
    must never look finished while any of them stand" (§8), and a stale mark
    presented as final is exactly that.
    """
    promoted = tuple(
        item if item.kind is not ReadinessIssueKind.STALE_RESULT else _blocked(item)
        for item in report.issues
    )
    return ReadinessReport(set_code=report.set_code, issues=promoted)


def _blocked(issue: ReadinessIssue) -> ReadinessIssue:
    """Return ``issue`` with :attr:`~ReadinessIssue.blocking` forced true."""
    return ReadinessIssue(
        kind=issue.kind, message=issue.message, blocking=True, roll=issue.roll
    )
