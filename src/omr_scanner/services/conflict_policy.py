"""Deciding what deserves a human (Phase 6).

Purpose:
    Turn a finished :class:`~omr_scanner.services.recognition_models.ScanResult`
    into the list of conflicts it implies - deterministically, from signals the
    recognition engine already produced.

Responsibilities:
    * :class:`ConflictPolicy` - the few choices that are genuinely policy.
    * :func:`detect_conflicts` - one sheet's conflicts.
    * :func:`detect_duplicate_identifiers` - the batch-level pass.
    * :func:`group_labels` - what a reviewer may choose from, straight from the
      template.

What does NOT belong here:
    * Persistence, Qt, or images. This module is a pure function of a result
      and a template, which is what lets every conflict type be tested without
      rendering a sheet.
    * A second opinion about the pixels. Recognition already decided what the
      sheet says; this module decides only *whether a person should look*.

What is a conflict, and what is merely a reading:
    A conflict is an ambiguity that leaves the **record** unusable - nobody can
    say whose script this is, which paper it answers, or whether the page was
    read at all. Only three things can produce one:

    * the candidate identifier (roll / student ID), per printed position;
    * the set code, per printed position;
    * the sheet itself - it would not register, or would not decode.

    An **answer is never a conflict**, however it was marked. Two options
    filled in, a mark too faint to accept, a group that could not be sampled:
    each is a fact about the paper that the recognition result already records
    (:attr:`~omr_scanner.services.recognition_models.AnswerView.status`,
    ``needs_review``, ``value``, the per-bubble fills), that the CSV already
    exports (``"B-D"``, ``"?"``, ``"B?"``), and that scoring already handles.
    None of it needs a person before the batch can go on, and routing it here
    produced a queue of a hundred entries per sheet in which the two that
    mattered could not be found.

    See :attr:`~omr_scanner.domain.review.ConflictType.requires_resolution`,
    which is where that line is drawn once for the whole application.

Why there is nothing tunable about *how sure is sure enough* here:
    The obvious design would be a pile of thresholds - "flag anything under
    0.8 confidence". That would be a second, competing set of thresholds
    alongside the template's own
    :class:`~omr_scanner.domain.template.RecognitionSettings`, which Phase 4
    exists to let an operator calibrate. Instead this module reads the statuses
    and confidences the decision layer already produced *using those settings*.
    Calibrating the template in Phase 4 therefore moves the conflict queue too,
    and there is exactly one place where that judgement is configured.
"""

from __future__ import annotations

import dataclasses
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from omr_scanner.domain.review import (
    WHOLE_FIELD,
    Candidate,
    ConflictType,
    FieldKind,
    FieldRef,
    MachineObservation,
)
from omr_scanner.domain.scan_quality import ScanQualityStatus
from omr_scanner.domain.template import (
    GridFieldDefinition,
    IgnoredFieldDefinition,
    QuestionBlockFieldDefinition,
)
from omr_scanner.recognition.fields import zone_groups
from omr_scanner.recognition.models import FieldStatus, MarkStatus
from omr_scanner.services.recognition_models import RegistrationStatus, StatusCode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

    from omr_scanner.domain.template import OmrTemplate, Zone
    from omr_scanner.services.recognition_models import (
        BubbleView,
        CharacterView,
        ScanResult,
    )

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConflictPolicy:
    """The choices about *what deserves review* that are genuinely choices.

    Both remaining flags are about the **sheet**. There is deliberately no flag
    for answers: whether an ambiguous answer is a conflict is not a policy
    question this application asks, because it is not one - the answer belongs
    in the result either way. See the module docstring.

    Attributes:
        flag_alignment_warnings: Raise a sheet-scope conflict when a page
            registered but with a reservation. Off by default: the repository's
            own real sample raises ``MULTIPLE_CORNER_CANDIDATES`` on every
            sheet, and a conflict per sheet for a warning the batch already
            reports would drown the queue.
        flag_assumed_orientation: Raise a sheet-scope conflict when the page's
            orientation was assumed rather than measured. On by default: an
            upside-down sheet read as upright produces a full set of confidently
            wrong answers, which is precisely what review exists to catch.
        flag_scan_quality: Raise a sheet-scope conflict when the page-geometry
            check found the sheet was not flat, or that part of it never reached
            the scanner. On by default, for the same reason as
            ``flag_assumed_orientation`` and unlike ``flag_alignment_warnings``:
            it does not fire on every sheet. It is raised only when the
            printing itself is measurably displaced from where the template puts
            it, which an undamaged scan - however rotated, skewed, dim or
            heavily marked - does not produce. See
            :mod:`omr_scanner.services.scan_quality`.
    """

    flag_alignment_warnings: bool = False
    flag_assumed_orientation: bool = True
    flag_scan_quality: bool = True


@dataclass(frozen=True, slots=True)
class DetectedConflict:
    """One conflict, as detection produces it - before it has been stored.

    Attributes:
        conflict_type: Why a human is needed.
        field: Which response group, for a field-scope conflict.
        observation: What the machine saw, verbatim.
        severity: ``0`` ordinary, ``1`` serious. Used only for queue ordering;
            nothing branches on it.
        related_scan_ids: Other scans this conflict also concerns, for a
            batch-scope conflict such as a duplicate identifier.
    """

    conflict_type: ConflictType
    field: FieldRef = dataclasses.field(default_factory=FieldRef)
    observation: MachineObservation = dataclasses.field(default_factory=MachineObservation)
    severity: int = 0
    related_scan_ids: tuple[int, ...] = ()

    @property
    def key(self) -> tuple[str, str, int]:
        """The conflict's identity within one scan.

        ``(conflict_type, zone_id, group_key)``. Deterministic, so re-reading a
        sheet produces the same key and
        :func:`~omr_scanner.services.review_store.sync_conflicts` updates the
        existing row instead of creating a second one. This is what stops a
        Phase 5 retry filling the queue with duplicates.
        """
        return (self.conflict_type.value, self.field.zone_id, self.field.group_key)


# ----------------------------------------------------------------------
# Status -> conflict type, per kind of field
# ----------------------------------------------------------------------
_IDENTIFIER_BY_STATUS: dict[str, ConflictType] = {
    MarkStatus.MULTIPLE.value: ConflictType.IDENTIFIER_MULTIPLE,
    MarkStatus.UNCERTAIN.value: ConflictType.IDENTIFIER_UNCERTAIN,
    MarkStatus.UNREADABLE.value: ConflictType.IDENTIFIER_UNREADABLE,
    MarkStatus.BLANK.value: ConflictType.IDENTIFIER_INCOMPLETE,
    MarkStatus.RESOLVED.value: ConflictType.IDENTIFIER_LOW_CONFIDENCE,
}

_SET_CODE_BY_STATUS: dict[str, ConflictType] = {
    MarkStatus.MULTIPLE.value: ConflictType.SET_CODE_MULTIPLE,
    MarkStatus.UNCERTAIN.value: ConflictType.SET_CODE_UNCERTAIN,
    MarkStatus.UNREADABLE.value: ConflictType.SET_CODE_UNREADABLE,
    MarkStatus.BLANK.value: ConflictType.SET_CODE_BLANK,
    MarkStatus.RESOLVED.value: ConflictType.SET_CODE_LOW_CONFIDENCE,
}

"""One table per field kind rather than one table plus branching.

The two kinds genuinely differ: a blank identifier column makes the sheet
unattributable, while a blank set code means the paper cannot be marked.
Mapping them through a shared "blank" conflict type would erase that
difference.

There is no third table. A question's statuses map to no conflict type at all -
they map to an answer value, which the recognition result already carries."""

_SERIOUS_TYPES = frozenset(
    {
        ConflictType.IDENTIFIER_BLANK,
        ConflictType.IDENTIFIER_INCOMPLETE,
        ConflictType.IDENTIFIER_MULTIPLE,
        ConflictType.IDENTIFIER_UNREADABLE,
        ConflictType.IDENTIFIER_DUPLICATE,
        ConflictType.SET_CODE_BLANK,
        ConflictType.SET_CODE_MULTIPLE,
        ConflictType.SET_CODE_UNREADABLE,
        ConflictType.REGISTRATION_FAILED,
        ConflictType.IMAGE_UNREADABLE,
        ConflictType.PROCESSING_ERROR,
        ConflictType.ORIENTATION_ASSUMED,
    }
)
"""Conflicts that stop a sheet being usable at all, as opposed to leaving one
question in doubt. Queue ordering only."""


def _severity_of(conflict_type: ConflictType) -> int:
    """Return the queue-ordering severity of a conflict type."""
    return 1 if conflict_type in _SERIOUS_TYPES else 0


def group_labels(template: OmrTemplate, zone_id: str, group_key: int) -> tuple[str, ...]:
    """Return the symbols a reviewer may choose from for one response group.

    Args:
        template: The template the sheet was read with.
        zone_id: The zone the group belongs to.
        group_key: The group's index within that zone.

    Returns:
        The labels in printed order, or ``()`` when the zone or group is not in
        this template.

    Straight from :func:`~omr_scanner.recognition.fields.zone_groups`, which is
    the same function recognition itself used to decide the group. That is why
    the review interface can never offer a choice the sheet does not have, and
    why nothing anywhere hard-codes ``A``-``E``: a template with six options, or
    with a set code whose symbols are ``"10"``, ``"11"``, ``"12"``, produces
    those.
    """
    zone = _zone_by_id(template, zone_id)
    if zone is None:
        return ()
    for group in zone_groups(zone):
        if group.key == group_key:
            return group.labels
    return ()


def group_cells(
    template: OmrTemplate, zone_id: str, group_key: int
) -> tuple[tuple[int, int], ...]:
    """Return the ``(row, column)`` cells of one response group.

    The review workspace uses these to highlight exactly the bubbles in
    dispute - the same cells recognition measured, never a recomputed guess.
    """
    zone = _zone_by_id(template, zone_id)
    if zone is None:
        return ()
    for group in zone_groups(zone):
        if group.key == group_key:
            return group.cells
    return ()


def _zone_by_id(template: OmrTemplate, zone_id: str) -> Zone | None:
    """Return the zone with ``zone_id``, or ``None``."""
    return next((zone for zone in template.zones if zone.id == zone_id), None)


def _field_kind(result: ScanResult, zone_id: str, template: OmrTemplate) -> FieldKind:
    """Classify a zone as identifier, set code, question block or other."""
    if zone_id == result.identifier_zone_id:
        return FieldKind.IDENTIFIER
    if zone_id == result.set_code_zone_id:
        return FieldKind.SET_CODE
    zone = _zone_by_id(template, zone_id)
    if zone is not None and isinstance(zone.field, QuestionBlockFieldDefinition):
        return FieldKind.QUESTION
    return FieldKind.OTHER


def _candidates_for(
    bubbles: Sequence[BubbleView], zone_id: str, cells: Iterable[tuple[int, int]]
) -> tuple[Candidate, ...]:
    """Build the ranked candidate list for one group from its bubbles.

    Returns ``()`` when the result carries no per-bubble evidence, which is the
    normal batch setting (``keep_bubble_measurements`` is off for a batch,
    because five hundred records per sheet is most of a gigabyte over ten
    thousand sheets). The review workspace re-reads the one sheet being looked
    at to obtain them; see
    :func:`~omr_scanner.services.review_store.conflict_evidence`.
    """
    if not bubbles:
        return ()
    wanted = set(cells)
    found = [
        Candidate(label=item.label, fill_ratio=item.fill_ratio, selected=item.selected)
        for item in bubbles
        if item.zone_id == zone_id and (item.row, item.column) in wanted
    ]
    # Best first: a reviewer reads the leading candidate and its runner-up, and
    # ordering by measured fill is the only ranking the engine actually has.
    found.sort(key=lambda item: item.fill_ratio, reverse=True)
    return tuple(found)


def _observation_from_group(
    *,
    value: str,
    status: str,
    confidence: float,
    top_fill: float,
    margin: float,
    candidates: tuple[Candidate, ...],
) -> MachineObservation:
    """Assemble one group's observation. Kept verbatim from the result."""
    return MachineObservation(
        value=value,
        status=status,
        confidence=confidence,
        top_fill=top_fill,
        margin=margin,
        candidates=candidates,
    )


def detect_conflicts(
    result: ScanResult,
    template: OmrTemplate,
    *,
    policy: ConflictPolicy | None = None,
) -> tuple[DetectedConflict, ...]:
    """Return every conflict one finished sheet implies.

    Args:
        result: What recognition produced. Read, never modified.
        template: The template it was read with, for field kinds and labels.
        policy: What deserves review; defaults apply when omitted.

    Returns:
        The conflicts, ordered by severity then by field - deterministic, so
        the same result always produces the same list in the same order, which
        is what makes storing them idempotent.

    Only the identifier, the set code and the sheet itself can appear.
    ``result.answers`` is never consulted: an ambiguous answer is a reading,
    not a dispute, and it stays in the result. A sheet with a hundred
    double-marked questions and a legible roll number therefore yields **no**
    conflicts at all.

    A sheet that failed to register produces exactly **one** conflict. It has no
    fields and no bubbles to dispute, and emitting one conflict per unreadable
    position for a page that was never measured would be noise standing in for
    the single fact that matters.
    """
    rules = policy if policy is not None else ConflictPolicy()
    found: list[DetectedConflict] = []

    sheet_level = _detect_sheet_conflicts(result, rules)
    if any(
        item.conflict_type
        in (
            ConflictType.REGISTRATION_FAILED,
            ConflictType.IMAGE_UNREADABLE,
            ConflictType.PROCESSING_ERROR,
        )
        for item in sheet_level
    ):
        return tuple(sheet_level)
    found.extend(sheet_level)

    found.extend(_detect_field_conflicts(result, template))

    found.sort(key=lambda item: (-item.severity, item.field.zone_id, item.field.group_key))
    return tuple(found)


def _detect_sheet_conflicts(
    result: ScanResult, rules: ConflictPolicy
) -> list[DetectedConflict]:
    """Conflicts about the sheet rather than about a value on it."""
    found: list[DetectedConflict] = []

    if result.has_status(StatusCode.IMAGE_LOAD_ERROR):
        return [
            _sheet_conflict(
                ConflictType.IMAGE_UNREADABLE,
                result.registration_message or "The image file could not be decoded.",
            )
        ]
    if result.has_status(StatusCode.PROCESSING_ERROR):
        return [
            _sheet_conflict(
                ConflictType.PROCESSING_ERROR,
                result.registration_message or "Recognition failed unexpectedly.",
            )
        ]
    if result.registration is RegistrationStatus.FAILED:
        return [
            _sheet_conflict(
                ConflictType.REGISTRATION_FAILED,
                result.registration_message or "The page could not be rectified.",
            )
        ]

    geometry = result.scan_quality
    if rules.flag_scan_quality and geometry is not None and geometry.needs_attention:
        found.append(
            _sheet_conflict(
                ConflictType.SCAN_QUALITY,
                geometry.reason
                or "The page geometry could not be verified across the whole sheet.",
                severity=1 if geometry.status is ScanQualityStatus.UNUSABLE else 0,
            )
        )

    quality = result.quality
    if rules.flag_assumed_orientation and quality is not None and quality.orientation_assumed:
        found.append(
            _sheet_conflict(
                ConflictType.ORIENTATION_ASSUMED,
                "The orientation mark was not found; the page was assumed upright. "
                "An inverted sheet read this way produces a full set of wrong answers.",
            )
        )
    if rules.flag_alignment_warnings and result.warnings:
        found.append(
            _sheet_conflict(
                ConflictType.ALIGNMENT_WARNING,
                "Registered with reservations: " + ", ".join(result.warnings) + ".",
            )
        )
    return found


def _sheet_conflict(
    conflict_type: ConflictType, detail: str, *, severity: int | None = None
) -> DetectedConflict:
    """Build one sheet-scope conflict.

    ``severity`` overrides the type's own rank for the one case where a single
    type spans both: a scan-quality finding is ordinary when a corner curled and
    serious when what curled was the candidate identifier.
    """
    return DetectedConflict(
        conflict_type=conflict_type,
        field=FieldRef(kind=FieldKind.OTHER, label="Sheet"),
        observation=MachineObservation(detail=detail),
        severity=_severity_of(conflict_type) if severity is None else severity,
    )


def _detect_field_conflicts(
    result: ScanResult, template: OmrTemplate
) -> list[DetectedConflict]:
    """Conflicts in the identifier and the set code, and nothing else.

    Every other grid field is skipped. A template may carry any number of
    numeric or alphanumeric regions - a centre number, a subject code, a
    date - and none of them decides whose script this is or which paper it
    answers. Treating "numeric" as "identity" would put them all in the queue;
    the test is semantic, and :func:`_field_kind` makes it by asking the result
    which zone it read the identifier and the set code from.
    """
    found: list[DetectedConflict] = []

    for item in result.fields:
        kind = _field_kind(result, item.zone_id, template)
        table = _table_for(kind)
        if table is None:
            continue

        # A wholly blank identifier is one fact, not one per column.
        if (
            kind is FieldKind.IDENTIFIER
            and item.status == FieldStatus.BLANK.value
            and item.characters
        ):
            found.append(
                DetectedConflict(
                    conflict_type=ConflictType.IDENTIFIER_BLANK,
                    field=FieldRef(
                        zone_id=item.zone_id,
                        group_key=WHOLE_FIELD,
                        kind=kind,
                        label=item.label,
                    ),
                    observation=MachineObservation(
                        value=item.value,
                        status=item.status,
                        detail="No mark was found in any position of this field.",
                    ),
                    severity=_severity_of(ConflictType.IDENTIFIER_BLANK),
                )
            )
            continue

        for character in item.characters:
            conflict_type = _conflict_for_group(character, table, kind)
            if conflict_type is None:
                continue
            cells = group_cells(template, item.zone_id, character.position)
            found.append(
                DetectedConflict(
                    conflict_type=conflict_type,
                    field=FieldRef(
                        zone_id=item.zone_id,
                        group_key=character.position,
                        kind=kind,
                        label=item.label,
                    ),
                    observation=_observation_from_group(
                        value=character.value,
                        status=character.status,
                        confidence=character.confidence,
                        top_fill=character.top_fill,
                        margin=character.margin,
                        candidates=_candidates_for(result.bubbles, item.zone_id, cells),
                    ),
                    severity=_severity_of(conflict_type),
                )
            )
    return found


def _table_for(kind: FieldKind) -> dict[str, ConflictType] | None:
    """Return the status table for a field kind, or ``None`` to skip it.

    ``None`` for every kind that is not
    :attr:`~omr_scanner.domain.review.FieldKind.is_record_identity` - which is
    what makes "only identity fields raise conflicts" a property of the type
    system rather than of a comment.
    """
    if not kind.is_record_identity:
        return None
    return _IDENTIFIER_BY_STATUS if kind is FieldKind.IDENTIFIER else _SET_CODE_BY_STATUS


def _conflict_for_group(
    character: CharacterView, table: dict[str, ConflictType], kind: FieldKind
) -> ConflictType | None:
    """Decide whether one identifier/set-code position needs a human.

    A blank position is a conflict for these fields even when the engine was
    confident about the blankness: an identifier with a gap cannot name a
    candidate, and a missing set code means the paper cannot be marked. That is
    the opposite of the rule for questions, and deliberately so.
    """
    status = character.status
    if status == MarkStatus.RESOLVED.value:
        return table[status] if character.confidence and _is_low(character) else None
    if status == MarkStatus.BLANK.value:
        return table[status]
    mapped = table.get(status)
    if mapped is None:  # pragma: no cover - every MarkStatus is in the tables
        _LOGGER.warning("Unmapped %s status %r during conflict detection", kind.value, status)
    return mapped


def _is_low(character: CharacterView) -> bool:
    """Whether a resolved position was nonetheless flagged for review.

    Reads the engine's own ``needs_review``-equivalent rather than comparing
    against a threshold of this module's own: a ``CharacterView`` carries no
    ``needs_review`` flag, but a resolved group is flagged exactly when its
    confidence fell below the template's ``min_confidence``, and a confidence
    of ``1.0`` means the separation the template demanded was fully achieved.
    """
    return character.confidence < 1.0


def detect_duplicate_identifiers(
    identifiers: Sequence[tuple[int, str]],
) -> dict[int, DetectedConflict]:
    """Find scans that resolved to the same candidate identifier.

    Args:
        identifiers: ``(scan_id, identifier_value)`` for every scan whose
            identifier the engine considered reliable. A value it did not trust
            must not be passed here - two sheets both read as ``"21?312"`` are
            not evidence of anything, and raising a duplicate conflict for them
            would bury the real duplicates.

    Returns:
        One conflict per scan involved, keyed by scan id. Each carries the
        other scans in :attr:`DetectedConflict.related_scan_ids`, so the review
        interface can offer to jump between them.

    Why this is a separate pass:
        A duplicate is not a property of one sheet - both sheets are perfectly
        legible - so it cannot be detected while reading one. Making a worker
        aware of the other sheets would mean shared mutable state across
        processes, which is exactly what the Phase 5 architecture avoids. This
        runs in the coordinator once the batch's identifiers are known.
    """
    grouped: dict[str, list[int]] = defaultdict(list)
    for scan_id, value in identifiers:
        if value:
            grouped[value].append(scan_id)

    conflicts: dict[int, DetectedConflict] = {}
    for value, scan_ids in grouped.items():
        if len(scan_ids) < 2:
            continue
        ordered = sorted(scan_ids)
        for scan_id in ordered:
            others = tuple(item for item in ordered if item != scan_id)
            conflicts[scan_id] = DetectedConflict(
                conflict_type=ConflictType.IDENTIFIER_DUPLICATE,
                field=FieldRef(kind=FieldKind.IDENTIFIER, label="Student ID"),
                observation=MachineObservation(
                    value=value,
                    detail=(
                        f"{len(ordered)} sheets in this batch were read as "
                        f"'{value}'. Each may be a different candidate, a sheet "
                        "scanned twice, or a miscoded identifier."
                    ),
                ),
                severity=_severity_of(ConflictType.IDENTIFIER_DUPLICATE),
                related_scan_ids=others,
            )
    return conflicts


def describe_template_choices(template: OmrTemplate) -> dict[str, tuple[str, ...]]:
    """Return every zone's symbol set, for a review UI building its buttons.

    Keyed by zone id. Ignored zones are omitted. Used so that a reviewer is
    offered the template's own alphabet - never a hard-coded ``A``-``E``.
    """
    choices: dict[str, tuple[str, ...]] = {}
    for zone in template.zones:
        field = zone.field
        if isinstance(field, IgnoredFieldDefinition):
            continue
        if isinstance(field, GridFieldDefinition):
            choices[zone.id] = field.symbols
        elif isinstance(field, QuestionBlockFieldDefinition):
            choices[zone.id] = field.answer_labels
    return choices


__all__ = [
    "ConflictPolicy",
    "DetectedConflict",
    "describe_template_choices",
    "detect_conflicts",
    "detect_duplicate_identifiers",
    "group_cells",
    "group_labels",
]
