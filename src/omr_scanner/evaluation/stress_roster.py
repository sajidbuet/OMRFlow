"""Deterministic synthetic candidate rosters for the stress dataset (§8).

Purpose:
    Give a :class:`~omr_scanner.evaluation.stress_dataset.StressDatasetSpec`
    a roster to reconcile against, so a stress run can exercise real
    :mod:`omr_scanner.services.reconciliation` classification - absentees,
    unknown candidates, duplicates, and an attendance-record disagreement -
    not merely per-sheet recognition. :mod:`omr_scanner.evaluation.stress_dataset`'s
    own module docstring calls this responsibility out ("represented in
    ``omr_scanner.services.stress_runner``'s roster generation instead of
    here"), but no such generation previously existed anywhere in the
    codebase; this module is where it now lives.

Responsibilities:
    * :func:`generate_stress_roster` - one
      :class:`~omr_scanner.domain.reconciliation.CandidateRecord` per sheet
      index, keyed by :func:`~omr_scanner.evaluation.stress_dataset.natural_roll`,
      deterministically from ``spec`` alone.
    * :func:`absent_with_script_candidate_id` - which candidate this module
      deliberately records absent despite a real script existing for them,
      so a test can assert on it by name rather than hand-deriving it.

What does NOT belong here:
    * Any database or file access. A roster here is a plain tuple of
      ``CandidateRecord`` - the same in-memory value object
      :mod:`omr_scanner.services.reconciliation` already consumes. Wrapping
      it in a ``RosterValidation`` for
      :func:`omr_scanner.services.reconciliation_store.import_roster` is the
      caller's job.
    * Reconciliation's classification rules themselves
      (:mod:`omr_scanner.services.reconciliation`).

Why every stress case kind's roster relationship falls out of the sheet
generator's own design, rather than being hand-listed here:
    :func:`~omr_scanner.evaluation.stress_dataset.plan_for_index` already
    gives ``DUPLICATE_ID``, ``DUPLICATE_ROLL_DIFFERENT_IMAGE`` and
    ``EXACT_DUPLICATE_SCAN`` sheets a *different* index's natural roll, not
    their own. Registering one candidate per index under its own natural
    roll therefore automatically produces, for any run with a non-zero draw
    of those kinds:

    * a genuine absentee - the drawn index's own natural-roll candidate, who
      now has no script (that index's sheet claimed someone else's roll
      instead of its own), and
    * a genuine duplicate - the partner index's candidate, who now has two
      scripts (their own natural sheet, plus the one that borrowed their
      roll).

    ``UNKNOWN_CANDIDATE`` sheets are, by
    :func:`~omr_scanner.evaluation.stress_dataset._out_of_range_roll`'s own
    construction, guaranteed to use a roll outside every index's natural
    range, so they surface as unmatched scripts against this roster with no
    extra work here either. Only ``ABSENT_WITH_SCRIPT`` needs a deliberate
    override, since nothing in ``stress_dataset`` otherwise varies
    attendance - see :func:`absent_with_script_candidate_id`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omr_scanner.domain.reconciliation import AttendanceState, CandidateRecord
from omr_scanner.evaluation.stress_dataset import StressCaseKind, natural_roll

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.evaluation.stress_dataset import StressDatasetSpec
    from omr_scanner.evaluation.test_cases import FieldLayout

__all__ = [
    "absent_with_script_candidate_id",
    "generate_stress_roster",
]


def _absent_with_script_index(spec: StressDatasetSpec) -> int | None:
    """The first index whose kind is guaranteed to keep its own natural roll.

    ``CLEAN`` specifically, not merely "any kind other than the ones that
    borrow a roll": several kinds (``LOW_CONFIDENCE``, ``AMBIGUOUS_MARK``,
    ...) keep their own roll too, but introduce recognition uncertainty a
    deterministic test should not have to tolerate on the one candidate this
    module needs recognition to read *exactly* right.
    """
    for index in range(spec.sheet_count):
        if spec.kind_for_index(index) is StressCaseKind.CLEAN:
            return index
    return None


def absent_with_script_candidate_id(
    spec: StressDatasetSpec, layout: FieldLayout
) -> str | None:
    """The candidate :func:`generate_stress_roster` deliberately marks absent.

    Returns ``None`` only when the run is too small or too oddly distributed
    to contain a single ``CLEAN`` sheet, which real distributions and
    practical sheet counts do not produce - handled rather than assumed.
    """
    index = _absent_with_script_index(spec)
    return None if index is None else natural_roll(spec, layout, index)


def generate_stress_roster(
    spec: StressDatasetSpec, layout: FieldLayout
) -> tuple[CandidateRecord, ...]:
    """Return one candidate per sheet index, keyed by its natural roll.

    Every candidate is registered :attr:`~AttendanceState.PRESENT` except the
    one named by :func:`absent_with_script_candidate_id`, which is
    deliberately recorded :attr:`~AttendanceState.ABSENT` despite a script
    existing for it - see the module docstring for why every other
    reconciliation exception this stress dataset can produce needs no
    equivalent override.

    Deterministic: the same ``spec`` always produces the same roster, in the
    same order.
    """
    forced_absent = absent_with_script_candidate_id(spec, layout)
    by_id: dict[str, CandidateRecord] = {}
    for index in range(spec.sheet_count):
        candidate_id = natural_roll(spec, layout, index)
        attendance = (
            AttendanceState.ABSENT
            if candidate_id == forced_absent
            else AttendanceState.PRESENT
        )
        # `natural_roll` is not guaranteed injective for a template with a
        # narrow identifier space run at very large sheet counts; the later
        # index's record wins, matching a roster file's own "last row for a
        # repeated ID is what a human sees last" behaviour rather than
        # raising over a collision this module cannot itself resolve.
        by_id[candidate_id] = CandidateRecord(
            candidate_id=candidate_id,
            display_name=f"Stress Candidate {index:07d}",
            source_row=index + 2,
            imported_attendance=attendance,
            imported_value=attendance.value,
        )
    return tuple(by_id.values())
