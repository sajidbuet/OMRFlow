"""The vocabulary of human review: conflicts, resolutions and provenance.

Purpose:
    Name every state a disputed value can be in, and every way a human can act
    on it, so that the rest of Phase 6 branches on an enum rather than on a
    free-form string.

Responsibilities:
    * :class:`ConflictType` - the taxonomy, derived from what recognition
      actually produces rather than invented.
    * :class:`ConflictState`, :class:`ConflictScope`, :class:`ValueSource`,
      :class:`ReviewAction`, :class:`ReasonCode` - the small closed sets the
      persistence layer stores and the GUI renders.
    * :class:`FieldRef` - which response group a conflict is about.
    * :class:`MachineObservation` - what the engine saw, kept verbatim.
    * :class:`Provenance` - the answer to "where did this final value come
      from", which is the phase's exit criterion in one object.

What does NOT belong here:
    * Persistence, Qt, images, or any decision about *when* a conflict should
      exist. Detection policy is
      :mod:`omr_scanner.services.conflict_policy`; storage is
      :mod:`omr_scanner.services.review_store`.

The one rule this module exists to make structural:
    A human decision is *added* to a machine observation, never substituted for
    it. :class:`MachineObservation` is frozen and is copied into a conflict at
    detection time; nothing in Phase 6 has a code path that edits it. The
    effective value is computed (:class:`Provenance`) from the observation plus
    the ordered audit history, so "what did the machine say" survives any
    number of later corrections.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ConflictScope(StrEnum):
    """What a conflict is *about*, which decides how it is reviewed."""

    FIELD = "field"
    """One response group: a roll-number column, the set code, one question."""

    SHEET = "sheet"
    """The whole sheet: it did not register, or could not be read at all."""

    BATCH = "batch"
    """Only visible across sheets - two scans claiming the same roll number."""


class ConflictType(StrEnum):
    """Why a value needs a human.

    Every member below corresponds to a state the recognition engine already
    produces (:class:`~omr_scanner.recognition.models.MarkStatus`,
    :class:`~omr_scanner.recognition.models.FieldStatus`,
    :class:`~omr_scanner.services.recognition_models.StatusCode`). Nothing here
    is a new judgement about pixels - Phase 6 decides *what deserves review*,
    never *what the sheet says*.
    """

    # -- candidate identifier -------------------------------------------
    IDENTIFIER_BLANK = "identifier_blank"
    """Every position of the identifier is empty; the sheet is unattributable."""

    IDENTIFIER_INCOMPLETE = "identifier_incomplete"
    """One position is blank while others carry marks."""

    IDENTIFIER_MULTIPLE = "identifier_multiple"
    """One position carries more than one mark."""

    IDENTIFIER_UNCERTAIN = "identifier_uncertain"
    """One position was marked too faintly, or too close to its runner-up."""

    IDENTIFIER_UNREADABLE = "identifier_unreadable"
    """One position could not be sampled at all."""

    IDENTIFIER_LOW_CONFIDENCE = "identifier_low_confidence"
    """A position resolved, but below the template's own minimum confidence."""

    IDENTIFIER_DUPLICATE = "identifier_duplicate"
    """Two or more sheets in the batch resolved to the same identifier. Batch
    scope: neither sheet is wrong on its own, which is exactly why this cannot
    be detected while reading one."""

    # -- set code -------------------------------------------------------
    SET_CODE_BLANK = "set_code_blank"
    SET_CODE_MULTIPLE = "set_code_multiple"
    SET_CODE_UNCERTAIN = "set_code_uncertain"
    SET_CODE_UNREADABLE = "set_code_unreadable"
    SET_CODE_LOW_CONFIDENCE = "set_code_low_confidence"

    # -- question answers -----------------------------------------------
    ANSWER_MULTIPLE = "answer_multiple"
    """More than one option marked. **Both marks are kept**; the reviewer
    decides what the sheet meant, and the machine's ``"B-D"`` survives."""

    ANSWER_UNCERTAIN = "answer_uncertain"
    ANSWER_UNREADABLE = "answer_unreadable"
    ANSWER_LOW_CONFIDENCE = "answer_low_confidence"
    ANSWER_BLANK = "answer_blank"
    """No mark at all. **Not raised by default** - a candidate is entitled to
    leave a question blank, and treating that as an error would bury the real
    conflicts under one per unanswered question. See
    :attr:`~omr_scanner.services.conflict_policy.ConflictPolicy.flag_blank_answers`."""

    # -- sheet level ----------------------------------------------------
    REGISTRATION_FAILED = "registration_failed"
    """The page could not be rectified, so nothing on it was measured."""

    ALIGNMENT_WARNING = "alignment_warning"
    """Registered, but with a geometry reservation worth a human's eye."""

    ORIENTATION_ASSUMED = "orientation_assumed"
    """Which way up the page is was assumed rather than measured."""

    IMAGE_UNREADABLE = "image_unreadable"
    """The file could not be decoded. A *processing* failure that a human can
    still act on (re-scan, replace the file), which is why it is reviewable -
    but see :attr:`is_processing_failure`."""

    PROCESSING_ERROR = "processing_error"
    """An unexpected failure inside recognition. Reviewable only in the sense
    that a human should see it; it is not a value to be corrected."""

    @property
    def scope(self) -> ConflictScope:
        """Where this conflict lives."""
        if self is ConflictType.IDENTIFIER_DUPLICATE:
            return ConflictScope.BATCH
        if self in _SHEET_TYPES:
            return ConflictScope.SHEET
        return ConflictScope.FIELD

    @property
    def is_processing_failure(self) -> bool:
        """Whether this is a software/IO failure rather than an ambiguous mark.

        The distinction matters (Phase 6 brief §36): a corrupt JPEG is not a
        value a reviewer can choose between, and offering "pick A, B, C or D"
        for one would be nonsense. These conflicts support acknowledgement and
        deferral, never a value correction.
        """
        return self in (ConflictType.IMAGE_UNREADABLE, ConflictType.PROCESSING_ERROR)

    @property
    def allows_value_correction(self) -> bool:
        """Whether a reviewer may supply a replacement value for this conflict.

        Everything except a **sheet-scope** conflict names a value somebody
        could choose differently. That deliberately includes
        :attr:`IDENTIFIER_DUPLICATE`, which is batch-scope but is still about an
        identifier: the usual resolution for two sheets claiming ``170501`` is
        that one of them was miscoded, and correcting it is the whole point.

        A sheet-scope conflict is not a value at all - "the page would not
        rectify" and "this JPEG will not decode" cannot be answered with
        ``A``, ``B``, ``C`` or ``D``, and offering that would be nonsense. Those
        are acknowledged or deferred instead.
        """
        return self.scope is not ConflictScope.SHEET

    @property
    def label(self) -> str:
        """A short human-readable name, for the queue and the review header."""
        return _TYPE_LABELS[self]


_SHEET_TYPES = frozenset(
    {
        ConflictType.REGISTRATION_FAILED,
        ConflictType.ALIGNMENT_WARNING,
        ConflictType.ORIENTATION_ASSUMED,
        ConflictType.IMAGE_UNREADABLE,
        ConflictType.PROCESSING_ERROR,
    }
)

_TYPE_LABELS: dict[ConflictType, str] = {
    ConflictType.IDENTIFIER_BLANK: "Student ID blank",
    ConflictType.IDENTIFIER_INCOMPLETE: "Student ID incomplete",
    ConflictType.IDENTIFIER_MULTIPLE: "Student ID multiple marks",
    ConflictType.IDENTIFIER_UNCERTAIN: "Student ID uncertain",
    ConflictType.IDENTIFIER_UNREADABLE: "Student ID unreadable",
    ConflictType.IDENTIFIER_LOW_CONFIDENCE: "Student ID low confidence",
    ConflictType.IDENTIFIER_DUPLICATE: "Duplicate student ID",
    ConflictType.SET_CODE_BLANK: "Set code blank",
    ConflictType.SET_CODE_MULTIPLE: "Set code multiple marks",
    ConflictType.SET_CODE_UNCERTAIN: "Set code uncertain",
    ConflictType.SET_CODE_UNREADABLE: "Set code unreadable",
    ConflictType.SET_CODE_LOW_CONFIDENCE: "Set code low confidence",
    ConflictType.ANSWER_MULTIPLE: "Multiple answers marked",
    ConflictType.ANSWER_UNCERTAIN: "Answer uncertain",
    ConflictType.ANSWER_UNREADABLE: "Answer unreadable",
    ConflictType.ANSWER_LOW_CONFIDENCE: "Answer low confidence",
    ConflictType.ANSWER_BLANK: "Answer blank",
    ConflictType.REGISTRATION_FAILED: "Registration failed",
    ConflictType.ALIGNMENT_WARNING: "Alignment warning",
    ConflictType.ORIENTATION_ASSUMED: "Orientation assumed",
    ConflictType.IMAGE_UNREADABLE: "Image unreadable",
    ConflictType.PROCESSING_ERROR: "Processing error",
}


class ConflictState(StrEnum):
    """Where a conflict is in its review life.

    Explicit states rather than an ``is_resolved`` flag, because "nobody has
    looked at this", "a human decided" and "a human deliberately postponed" are
    three different things and a boolean can only tell two of them apart.
    """

    OPEN = "open"
    """Awaiting review. The state every conflict is created in, and the state a
    reopened conflict returns to."""

    RESOLVED = "resolved"
    """A named reviewer decided - by accepting the machine value or by
    correcting it. Never means "the problem went away"."""

    DEFERRED = "deferred"
    """A named reviewer deliberately postponed it. Distinct from OPEN so that a
    queue can show what has genuinely never been looked at."""

    WITHDRAWN = "withdrawn"
    """Re-reading the sheet no longer produces this conflict - a retry that
    succeeded, say. Applied only to conflicts **no human has acted on**; a
    resolved or deferred conflict keeps its state and its history whatever a
    later re-read says. Withdrawn conflicts are kept, never deleted, because
    the fact that the machine once disputed this value is itself evidence."""

    @property
    def is_open(self) -> bool:
        """Whether this conflict still needs a decision."""
        return self is ConflictState.OPEN

    @property
    def needs_attention(self) -> bool:
        """Whether this conflict counts against "unresolved" in a summary."""
        return self in (ConflictState.OPEN, ConflictState.DEFERRED)

    @property
    def is_human_touched(self) -> bool:
        """Whether a reviewer has acted on this conflict.

        The test :func:`~omr_scanner.services.review_store.sync_conflicts` uses
        to decide whether a re-read may withdraw a conflict: a machine may
        withdraw its own complaint, never a human's decision.
        """
        return self in (ConflictState.RESOLVED, ConflictState.DEFERRED)


class ValueSource(StrEnum):
    """Where an effective value came from."""

    MACHINE = "machine"
    """Recognition's own reading, never reviewed by a human."""

    HUMAN = "human"
    """A named reviewer's decision - including a decision to accept the machine
    value, which is a review event and not the same thing as MACHINE."""


class ReviewAction(StrEnum):
    """What one audit event records.

    Every state change appends exactly one of these. A correction that also
    changes state appends one event, not two, so the history reads as a
    sequence of human decisions rather than of internal bookkeeping.
    """

    DETECTED = "detected"
    """The machine raised this conflict. Written once, at detection, so the
    history always begins with what recognition saw."""

    RE_RECOGNISED = "re_recognised"
    """The sheet was read again (a Phase 5 retry) and the machine observation
    changed. Recorded rather than silently overwriting the earlier observation
    - which is the same rule as for human corrections, applied to the machine."""

    ACCEPTED = "accepted"
    """A reviewer inspected the conflict and confirmed the machine value."""

    CORRECTED = "corrected"
    """A reviewer replaced the value."""

    DEFERRED = "deferred"
    REOPENED = "reopened"
    WITHDRAWN = "withdrawn"
    """The machine no longer reports this conflict."""

    @property
    def is_human(self) -> bool:
        """Whether this action was taken by a person.

        Machine-authored events (``DETECTED``, ``RE_RECOGNISED``,
        ``WITHDRAWN``) carry no reviewer, and requiring one would either force a
        fake name into the ledger or stop conflict detection working headlessly.
        """
        return self in (
            ReviewAction.ACCEPTED,
            ReviewAction.CORRECTED,
            ReviewAction.DEFERRED,
            ReviewAction.REOPENED,
        )

    @property
    def sets_effective_value(self) -> bool:
        """Whether this action decides the field's effective value.

        ``REOPENED`` deliberately does not: reopening withdraws the *decision*,
        so the effective value falls back to the machine's until a new decision
        is made. That is what makes the projection in
        :func:`~omr_scanner.services.review_store.provenance_from` a pure
        left-fold over the history.
        """
        return self in (ReviewAction.ACCEPTED, ReviewAction.CORRECTED)


class ReasonCode(StrEnum):
    """Why a reviewer decided what they decided.

    A short closed list plus free text, because an examination office reviewing
    four hundred conflicts needs the *pattern* ("two hundred of these were
    threshold errors") and typing that two hundred times in prose produces
    neither a pattern nor a reason.
    """

    CLEAR_VISUAL_MARK = "clear_visual_mark"
    DOMINANT_MARK = "dominant_mark"
    ERASED_RESPONSE = "erased_response"
    THRESHOLD_ERROR = "threshold_error"
    ALIGNMENT_ISSUE = "alignment_issue"
    STRAY_MARK = "stray_mark"
    MISCLASSIFICATION = "misclassification"
    MACHINE_CONFIRMED = "machine_confirmed"
    """The default for accepting a machine value: no typing required, and the
    ledger still says why."""

    OTHER = "other"
    """Requires free text - see
    :func:`~omr_scanner.services.review_store.validate_reason`."""

    @property
    def label(self) -> str:
        """The wording shown in the reason selector."""
        return _REASON_LABELS[self]

    @property
    def requires_text(self) -> bool:
        """Whether free text must accompany this reason."""
        return self is ReasonCode.OTHER


_REASON_LABELS: dict[ReasonCode, str] = {
    ReasonCode.CLEAR_VISUAL_MARK: "Clear visual mark",
    ReasonCode.DOMINANT_MARK: "Multiple marks - dominant mark selected",
    ReasonCode.ERASED_RESPONSE: "Student erased previous response",
    ReasonCode.THRESHOLD_ERROR: "Recognition threshold error",
    ReasonCode.ALIGNMENT_ISSUE: "Alignment or crop issue",
    ReasonCode.STRAY_MARK: "Handwritten or stray mark",
    ReasonCode.MISCLASSIFICATION: "Machine misclassification",
    ReasonCode.MACHINE_CONFIRMED: "Machine result visually confirmed",
    ReasonCode.OTHER: "Other (explain below)",
}


class FieldKind(StrEnum):
    """Which part of a sheet a field-scope conflict belongs to."""

    IDENTIFIER = "identifier"
    SET_CODE = "set_code"
    QUESTION = "question"
    OTHER = "other"


WHOLE_FIELD = -1
"""``group_key`` meaning "the field as a whole, not one position of it".

Used by :attr:`ConflictType.IDENTIFIER_BLANK`, which is about an identifier
with nothing marked anywhere: raising one conflict per empty column would bury
the queue under six copies of one fact."""


@dataclass(frozen=True, slots=True)
class FieldRef:
    """Which response group a conflict is about.

    ``(zone_id, group_key)`` is the identity. It is the same key
    :func:`~omr_scanner.recognition.fields.zone_groups` uses - the character
    position for a grid field, the question *offset* for a question block - so
    a conflict can always be mapped back to its bubbles and its permitted
    labels without storing either.

    Attributes:
        zone_id: The template zone.
        group_key: The response group within it, or :data:`WHOLE_FIELD`.
        kind: Which part of the sheet this is.
        label: The zone's human-readable name, denormalised so a queue row can
            be rendered without loading the template.
        question_number: The printed question number, when ``kind`` is
            ``QUESTION``. Presentation only; ``group_key`` remains the identity.
    """

    zone_id: str = ""
    group_key: int = WHOLE_FIELD
    kind: FieldKind = FieldKind.OTHER
    label: str = ""
    question_number: int | None = None

    @property
    def is_whole_field(self) -> bool:
        """Whether this refers to a field rather than one position of it."""
        return self.group_key == WHOLE_FIELD

    def describe(self) -> str:
        """One short phrase naming the field, for a queue cell."""
        if self.kind is FieldKind.QUESTION and self.question_number is not None:
            return f"Question {self.question_number}"
        if self.is_whole_field:
            return self.label or self.zone_id
        return f"{self.label or self.zone_id} - position {self.group_key + 1}"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One option the machine measured, with the evidence behind it.

    Attributes:
        label: The symbol this bubble stands for.
        fill_ratio: The measured fill, in ``[0, 1]``.
        selected: Whether the decision layer counted it as marked.

    Deliberately called a *fill score* everywhere it is shown, never a
    probability: the engine measures coverage, and presenting 0.71 as "71%
    confident" would invent a calibration nobody has performed.
    """

    label: str
    fill_ratio: float
    selected: bool = False


@dataclass(frozen=True, slots=True)
class MachineObservation:
    """What recognition saw. Frozen, and never edited after detection.

    Attributes:
        value: The engine's own value string, in its own vocabulary -
            ``""`` for blank, ``"B"``, ``"B-D"`` for a double mark. Kept
            verbatim: ``"B-D"`` says what is on the paper and a reviewer
            choosing ``"B"`` does not make that untrue.
        status: The group's :class:`~omr_scanner.recognition.models.MarkStatus`
            or :class:`~omr_scanner.recognition.models.FieldStatus` value.
        confidence: The engine's bounded decision score, ``0.0`` when it
            declined to produce one.
        top_fill: Highest fill ratio in the group.
        margin: Separation between the darkest bubble and the next.
        candidates: Every option with its measured fill, best first. Empty when
            the result was produced without per-bubble evidence, which is the
            normal batch setting - see
            :func:`~omr_scanner.services.review_store.conflict_evidence`.
        detail: A plain sentence for sheet-scope conflicts, where there is no
            group and no candidates - the registration message, say.
    """

    value: str = ""
    status: str = ""
    confidence: float = 0.0
    top_fill: float = 0.0
    margin: float = 0.0
    candidates: tuple[Candidate, ...] = ()
    detail: str = ""

    @property
    def has_evidence(self) -> bool:
        """Whether per-bubble candidates were kept for this observation."""
        return bool(self.candidates)

    @property
    def runner_up(self) -> Candidate | None:
        """The second-best candidate, when there is one."""
        return self.candidates[1] if len(self.candidates) > 1 else None


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a final value came from. The exit criterion, as one object.

    Attributes:
        value: The effective value a downstream consumer should use.
        source: Machine or human.
        machine_value: What recognition read, always - including when a human
            replaced it.
        machine_confidence: The engine's score for that reading.
        reviewer: Who decided, when ``source`` is ``HUMAN``.
        reason: The reason code's stored value.
        reason_text: Free text, when the reviewer supplied any.
        decided_at: ISO-8601 UTC timestamp of the deciding event.
        audit_event_id: The event that set this value, so a report can link
            straight to the history entry.
        conflict_id: The conflict this value was disputed under.
        state: The conflict's current state, so a consumer can tell "a human
            confirmed this" from "nobody has looked yet".
    """

    value: str = ""
    source: ValueSource = ValueSource.MACHINE
    machine_value: str = ""
    machine_confidence: float = 0.0
    reviewer: str = ""
    reason: str = ""
    reason_text: str = ""
    decided_at: str = ""
    audit_event_id: int | None = None
    conflict_id: int | None = None
    state: ConflictState | None = None

    @property
    def is_human_decided(self) -> bool:
        """Whether a named reviewer decided this value."""
        return self.source is ValueSource.HUMAN

    @property
    def was_corrected(self) -> bool:
        """Whether the effective value differs from what the machine read."""
        return self.is_human_decided and self.value != self.machine_value

    def describe(self) -> str:
        """One line naming the value and its origin, for a report or a tooltip."""
        if not self.is_human_decided:
            return f"{self.value or '(blank)'} (machine)"
        who = self.reviewer or "unknown reviewer"
        if self.was_corrected:
            return (
                f"{self.value or '(blank)'} (corrected by {who} from "
                f"{self.machine_value or '(blank)'})"
            )
        return f"{self.value or '(blank)'} (confirmed by {who})"


@dataclass(frozen=True, slots=True)
class ReviewCounts:
    """How many conflicts are in each state, for a summary panel.

    Attributes:
        total: Every conflict, whatever its state.
        open_count: Never looked at.
        resolved: Decided by a named reviewer.
        deferred: Deliberately postponed.
        withdrawn: No longer reported by the machine and never human-touched.
        by_type: Count per :class:`ConflictType` value, for a breakdown.
    """

    total: int = 0
    open_count: int = 0
    resolved: int = 0
    deferred: int = 0
    withdrawn: int = 0
    by_type: dict[str, int] = field(default_factory=dict)

    @property
    def unresolved(self) -> int:
        """Conflicts that still need a decision - open plus deferred.

        Deferred counts as unresolved on purpose: postponing is not deciding,
        and a batch with five deferred conflicts is not a finished batch.
        """
        return self.open_count + self.deferred

    @property
    def is_clear(self) -> bool:
        """Whether nothing is waiting for a human."""
        return self.unresolved == 0


__all__ = [
    "WHOLE_FIELD",
    "Candidate",
    "ConflictScope",
    "ConflictState",
    "ConflictType",
    "FieldKind",
    "FieldRef",
    "MachineObservation",
    "Provenance",
    "ReasonCode",
    "ReviewAction",
    "ReviewCounts",
    "ValueSource",
]
