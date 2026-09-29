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

What counts as a conflict at all:
    Only an ambiguity that leaves the *record* unusable - who the script
    belongs to (:attr:`FieldKind.IDENTIFIER`), which paper it answers
    (:attr:`FieldKind.SET_CODE`), or whether the page was read at all. An
    ambiguous or multiply-marked **answer is not a conflict**: it is a
    recognition result, it exports and scores as one, and it never waits for a
    human. :attr:`ConflictType.requires_resolution` is where that line is
    drawn; :data:`LEGACY_ANSWER_TYPES` is what an older project may still hold.

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
    SET_CODE_UNDEFINED = "set_code_undefined"
    """The set code was read, but it is not one of this project's sets.

    Raised by a batch-level pass (the project's sets are not known while one
    sheet is being read), like :attr:`IDENTIFIER_DUPLICATE`. Without it such a
    script belongs to no set's reconciliation and would simply vanish from the
    Attendance stage. As a whole-field set-code record it is corrected with
    the ordinary full set-code editor."""

    # -- operator-initiated ---------------------------------------------
    MANUAL_OVERRIDE = "manual_override"
    """An operator overrode a position the machine read **confidently**.

    **Not a detection.** Recognition raised nothing here; the record exists
    only because a person, using the explicit whole-field editor, typed a value
    that disagrees with a confident reading - a digit read cleanly as ``9``
    that the paper shows is an ``8``. A human decision has to be recorded
    against *something*, and this is the smallest thing that can carry one
    without pretending the machine had doubted it: the history of such a
    record begins with the operator's correction, never with a ``DETECTED``
    event, and every event it holds is marked as an explicit override.

    Never open. With no decision standing - its override undone or reopened -
    it rests as :attr:`ConflictState.WITHDRAWN` and the machine's reading
    stands again, so it can neither enter the working queue nor block a
    batch. See :attr:`is_operator_override`."""

    # -- question answers (legacy; no longer raised) ---------------------
    #
    # These five are **never produced** any more. An ambiguous answer is a
    # recognition result, not a dispute about who a sheet belongs to or which
    # paper it answers, so it stays in the result and never enters the review
    # queue - see :attr:`requires_resolution` and ``docs/conflict_review.md``.
    #
    # The members remain so that a project written by an earlier build still
    # deserialises: its stored rows say ``"answer_multiple"``, and refusing to
    # name that value would make an old project unreadable rather than merely
    # out of date.
    ANSWER_MULTIPLE = "answer_multiple"
    """More than one option marked. **Legacy.** Both marks are kept in the
    recognition result (``"B-D"``); no conflict is raised for it."""

    ANSWER_UNCERTAIN = "answer_uncertain"
    """Legacy. The result carries the uncertainty; no conflict is raised."""

    ANSWER_UNREADABLE = "answer_unreadable"
    """Legacy. The result carries the failure; no conflict is raised."""

    ANSWER_LOW_CONFIDENCE = "answer_low_confidence"
    """Legacy. The result carries the confidence; no conflict is raised."""

    ANSWER_BLANK = "answer_blank"
    """No mark at all. **Legacy.** A candidate is entitled to leave a question
    blank, and it was never raised by default even when answers were
    conflicts."""

    # -- sheet level ----------------------------------------------------
    REGISTRATION_FAILED = "registration_failed"
    """The page could not be rectified, so nothing on it was measured."""

    ALIGNMENT_WARNING = "alignment_warning"
    """Registered, but with a geometry reservation worth a human's eye."""

    ORIENTATION_ASSUMED = "orientation_assumed"
    """Which way up the page is was assumed rather than measured."""

    SCAN_QUALITY = "scan_quality"
    """The paper itself was not flat, or part of it never reached the scanner.

    A **different kind of doubt** from every other member here, and the reason
    it is its own type rather than another ``alignment_warning``. The others say
    a *value* is disputed, or that the page could not be rectified at all. This
    one says the page rectified successfully and the values were read - but the
    mapping from template coordinates to paper stopped being trustworthy over
    part of the sheet, so the values read *there* may have come from the wrong
    place. It names the area and, where the template says so, the questions.

    Sheet-scope, so it is acknowledged or deferred rather than corrected: "the
    lower-right corner was curled" is not a question with an answer of A, B, C
    or D. The recognised values stay on the result either way - see
    :class:`~omr_scanner.domain.scan_quality.ScanQualityAssessment` and
    ``docs/SCAN_QUALITY.md``."""

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
    def requires_resolution(self) -> bool:
        """Whether this conflict belongs in the Conflict Resolution queue.

        **The one place the answer/identity distinction is named.** Everything
        that counts, lists, blocks on or displays conflicts asks here, so there
        is a single definition of "a conflict" rather than one per consumer.

        ``False`` for the five :data:`LEGACY_ANSWER_TYPES`. An ambiguous or
        multiply-marked answer is a *recognition result*: it says what is on the
        paper, it is exported and scored as such, and no human decision is
        needed before the batch can go on. Routing it through resolution made a
        queue of a hundred entries per sheet that nobody could work through, and
        buried the handful of conflicts that genuinely stop a script being
        attributed or marked.

        ``True`` for everything else - the identifier, the set code, and the
        sheet-scope failures (a page that would not register, an image that
        would not decode). Those are all *record*-level: without them settled,
        nobody knows whose script this is, which paper it answers, or whether it
        was read at all.
        """
        return self not in LEGACY_ANSWER_TYPES

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
    def is_operator_override(self) -> bool:
        """Whether this record was opened by a person rather than by detection.

        The one distinction :attr:`MANUAL_OVERRIDE` exists to keep: a history
        reader, an export and the review store all need to tell "the machine
        doubted this and somebody decided" from "the machine was sure and
        somebody overruled it".
        """
        return self is ConflictType.MANUAL_OVERRIDE

    @property
    def label(self) -> str:
        """A short human-readable name, for the queue and the review header."""
        return _TYPE_LABELS[self]


_SHEET_TYPES = frozenset(
    {
        ConflictType.REGISTRATION_FAILED,
        ConflictType.ALIGNMENT_WARNING,
        ConflictType.ORIENTATION_ASSUMED,
        ConflictType.SCAN_QUALITY,
        ConflictType.IMAGE_UNREADABLE,
        ConflictType.PROCESSING_ERROR,
    }
)

LEGACY_ANSWER_TYPES: frozenset[ConflictType] = frozenset(
    {
        ConflictType.ANSWER_MULTIPLE,
        ConflictType.ANSWER_UNCERTAIN,
        ConflictType.ANSWER_UNREADABLE,
        ConflictType.ANSWER_LOW_CONFIDENCE,
        ConflictType.ANSWER_BLANK,
    }
)
"""Conflict types this build never raises, kept so old projects still load.

A project scanned by an earlier build may hold thousands of these rows. They
are not deleted - a stored observation is evidence, and a decision somebody
made on one is still theirs - but they are excluded from every active queue
and count by :attr:`ConflictType.requires_resolution`.
"""

RESOLUTION_TYPES: tuple[ConflictType, ...] = tuple(
    item for item in ConflictType if item.requires_resolution
)
"""Every conflict type that may appear in the resolution queue, in declaration
order. Built from :attr:`ConflictType.requires_resolution` rather than listed
again, so a new member cannot be added to one and forgotten in the other."""

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
    ConflictType.SET_CODE_UNDEFINED: "Set code not a defined set",
    ConflictType.MANUAL_OVERRIDE: "Operator field override",
    ConflictType.ANSWER_MULTIPLE: "Multiple answers marked",
    ConflictType.ANSWER_UNCERTAIN: "Answer uncertain",
    ConflictType.ANSWER_UNREADABLE: "Answer unreadable",
    ConflictType.ANSWER_LOW_CONFIDENCE: "Answer low confidence",
    ConflictType.ANSWER_BLANK: "Answer blank",
    ConflictType.REGISTRATION_FAILED: "Registration failed",
    ConflictType.ALIGNMENT_WARNING: "Alignment warning",
    ConflictType.ORIENTATION_ASSUMED: "Orientation assumed",
    ConflictType.SCAN_QUALITY: "Scan quality",
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
    the fact that the machine once disputed this value is itself evidence.

    Also where an :attr:`ConflictType.MANUAL_OVERRIDE` record rests when no
    override stands on it any more. The meaning is the same - nothing here is
    awaiting anybody - and a sixth state would have to be taught to every
    count, filter and SQL predicate in the application."""

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

    @property
    def label(self) -> str:
        """The wording a queue cell uses for this state."""
        return _STATE_LABELS[self]

    @property
    def marker(self) -> str:
        """A one-character glyph carrying the same distinction as the colour.

        The queue tints a row by state, and a tint alone is unreadable to a
        colour-blind reviewer and invisible in a printed screenshot. Every row
        therefore carries the state as a **word** and this glyph beside it, so
        the colour is the third way of saying it rather than the only one.
        """
        return _STATE_MARKERS[self]


_STATE_LABELS: dict[ConflictState, str] = {
    ConflictState.OPEN: "Open",
    ConflictState.RESOLVED: "Resolved",
    ConflictState.DEFERRED: "Deferred",
    ConflictState.WITHDRAWN: "Withdrawn",
}

_STATE_MARKERS: dict[ConflictState, str] = {
    ConflictState.OPEN: "●",  # filled circle - still to do
    ConflictState.RESOLVED: "✓",  # tick
    ConflictState.DEFERRED: "⏸",  # pause
    ConflictState.WITHDRAWN: "⊘",  # circled slash
}

REOPENED_LABEL = "Reopened"
"""What a conflict that is open *because somebody put it back* is called.

Not a :class:`ConflictState` member: reopening returns a conflict to ``OPEN``,
and inventing a sixth state would mean every count, filter and SQL predicate in
the application had to learn about it. It is a presentation distinction, drawn
from the audit ledger - see
:attr:`~omr_scanner.services.review_store.ConflictRecord.reversed_before`."""

REOPENED_MARKER = "↺"
"""The anticlockwise-arrow glyph for :data:`REOPENED_LABEL`."""


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

    The ledger is append-only, so *undo is itself an event*. A human command
    (:attr:`ACCEPTED`, :attr:`CORRECTED`, :attr:`DEFERRED`, :attr:`REOPENED`)
    is pushed onto a stack as the history is folded, and :attr:`UNDONE` pops
    the top of that stack. Nothing is erased: the undone decision keeps its own
    reviewer, reason and timestamp, and the reversal is attributed to whoever
    performed it. See
    :func:`~omr_scanner.services.review_store.standing_commands`.
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

    UNDONE = "undone"
    """A reviewer took back their own most recent command on this conflict.

    **Not a deletion.** The command it reverses stays in the ledger exactly as
    it was recorded, and this event names who reversed it and when, so a
    history reads ``machine -> decision -> undo -> decision`` rather than
    pretending the first decision never happened.

    Distinct from :attr:`REOPENED`, which discards *every* standing decision
    and returns the conflict to the machine's own value. An undo steps back one
    command: a conflict corrected by X and then corrected again by Y returns to
    X's value, not to the machine's.
    """

    WITHDRAWN = "withdrawn"
    """The machine no longer reports this conflict."""

    REDETECTED = "redetected"
    """The machine reports, again, a conflict it had itself withdrawn.

    Machine-authored, like :attr:`WITHDRAWN`, and its mirror image: a
    duplicate Student ID that stopped being one when a sheet was rejected
    becomes one again when the rejection is undone. The detection pass that
    withdrew it raises it again, returning it to ``OPEN``. Only ever applied
    to a conflict **no person has decided** - a human decision is never
    reopened by the machine."""

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
            ReviewAction.UNDONE,
        )

    @property
    def is_command(self) -> bool:
        """Whether this action pushes a command onto the standing stack.

        Every action that *changes where the conflict stands* - a decision, a
        deferral, a reopening, a machine withdrawal - rather than merely
        recording something (``DETECTED``, ``RE_RECOGNISED``) or reversing an
        earlier command (:attr:`UNDONE`, which pops instead of pushing).
        """
        return self in (
            ReviewAction.ACCEPTED,
            ReviewAction.CORRECTED,
            ReviewAction.DEFERRED,
            ReviewAction.REOPENED,
            ReviewAction.WITHDRAWN,
            ReviewAction.REDETECTED,
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

    @property
    def is_reversal(self) -> bool:
        """Whether this action took back a decision somebody had made.

        Read by the queue so that a conflict which is open *because a person
        put it back* can say so, rather than looking identical to one nobody
        has ever touched.
        """
        return self in (ReviewAction.REOPENED, ReviewAction.UNDONE)


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
    WRONG_ID_ENTERED = "wrong_id_entered"
    """The candidate filled in a roll number that is not theirs - often a
    neighbour's, or an absent candidate's. The marks were read correctly; the
    person who made them wrote the wrong number. Distinct from
    :attr:`MISCLASSIFICATION`, which says the machine misread the paper."""
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
    ReasonCode.WRONG_ID_ENTERED: "Candidate entered a wrong roll number / ID",
    ReasonCode.MACHINE_CONFIRMED: "Machine result visually confirmed",
    ReasonCode.OTHER: "Other (explain below)",
}


class FieldKind(StrEnum):
    """Which part of a sheet a field-scope conflict belongs to."""

    IDENTIFIER = "identifier"
    SET_CODE = "set_code"
    QUESTION = "question"
    OTHER = "other"

    @property
    def is_record_identity(self) -> bool:
        """Whether ambiguity here leaves the *record* unidentifiable.

        The semantic test conflict detection applies: an unreadable roll number
        means nobody knows whose script this is, and an unreadable set code
        means nobody knows which paper it answers. Both stop the sheet being
        used at all.

        A question does not. One answer in doubt leaves the other ninety-nine
        perfectly usable, and what the sheet says about it is already recorded
        in the recognition result.

        Deliberately a property of the *field kind* rather than a check against
        a zone's name or a GUI label: a template calls its identifier whatever
        it likes, and matching on "roll" or "Question" would break the first
        time somebody labelled a field in Bengali.
        """
        return self in (FieldKind.IDENTIFIER, FieldKind.SET_CODE)


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
    "LEGACY_ANSWER_TYPES",
    "REOPENED_LABEL",
    "REOPENED_MARKER",
    "RESOLUTION_TYPES",
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
