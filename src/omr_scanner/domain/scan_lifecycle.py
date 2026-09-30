"""The vocabulary of Reject & Rescan: a scan's logical lifecycle.

Purpose:
    Name the states a *physical scan* can be in once an operator has judged it
    unusable, the reasons they give, and the storage state of its image - as
    closed sets the persistence layer stores and the GUI renders.

What does NOT belong here:
    * Persistence, file handling or Qt. Storage is
      :mod:`omr_scanner.services.scan_lifecycle`; this module describes shapes.

The rule this module exists to make structural:
    **Logical lifecycle and physical file existence are different facts.**
    :class:`LifecycleState` says what a scan *means* for results - whether it
    may contribute to a mark. :class:`FileState` says whether its image is
    still on disk. Purging an image changes the second and never the first, so
    "this scan existed, was rejected, and was replaced by that one" stays true
    and readable after the bytes are gone.

Processing state is a third, separate fact:
    :class:`~omr_scanner.database.models.ScanJobStatus` (pending, completed,
    failed...) says whether recognition has run. A rejected scan may be
    reprocessed, resumed or retried and stays rejected, because the rejection
    is not stored on the processing row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class LifecycleState(StrEnum):
    """What one scan means for results.

    Only :attr:`ACTIVE` is result-eligible. Every other state keeps the scan's
    row, its recognition result, its conflicts and its history exactly as they
    were - it simply stops contributing to reconciliation validity, scoring,
    results and exports.
    """

    ACTIVE = "active"
    """An ordinary scan. Also the state a rejection returns to when it is
    undone."""

    REJECTED_PENDING_RESCAN = "rejected_pending_rescan"
    """An operator judged the scan unusable and a rescan is outstanding."""

    SUPERSEDED_BY_REPLACEMENT = "superseded_by_replacement"
    """A rejected scan whose rescan has been explicitly confirmed. Never
    result-eligible again while the replacement link stands."""

    REIMPORT_OF_REJECTED = "reimport_of_rejected"
    """A scan whose bytes are identical to a rejected or superseded scan's.

    Importing the same file again is not a rescan and must not resurrect it,
    so such a scan is linked back to the original's history rather than
    treated as a new submission or offered as a replacement."""

    EXCLUDED = "excluded"
    """An operator decided the sheet must not take part in this examination's
    results at all, and **no rescan is expected**: a duplicate copy of a
    script that is kept, an accidental scan, the wrong exam's sheet, a blank
    or administrative page. Reversible (*Restore*). Recorded from the
    Attendance stage."""

    DEFERRED = "deferred"
    """An operator postponed the decision about this sheet. Left out of
    reconciliation counts, scoring and results **while deferred**, but still
    an open review item - visibly marked, counted, and restorable to active
    review at any time. Never a final disposition."""

    @property
    def is_result_eligible(self) -> bool:
        """Whether a scan in this state may contribute to any result."""
        return self is LifecycleState.ACTIVE

    @property
    def is_outstanding(self) -> bool:
        """Whether this state represents unfinished *physical* work - a rescan.

        A deferred sheet is unfinished too, but what is outstanding is a
        decision, not a sheet to fetch: see :attr:`awaits_decision`.
        """
        return self is LifecycleState.REJECTED_PENDING_RESCAN

    @property
    def awaits_decision(self) -> bool:
        """Whether an operator still owes this sheet a decision (deferred)."""
        return self is LifecycleState.DEFERRED

    @property
    def is_disposition(self) -> bool:
        """Whether this is an Attendance disposition - excluded or deferred.

        Both are reversible with *Restore*; neither involves a rescan or a
        replacement link.
        """
        return self in (LifecycleState.EXCLUDED, LifecycleState.DEFERRED)

    @property
    def label(self) -> str:
        """Operator-facing wording. Words, never colour alone."""
        return _STATE_LABELS[self]

    @property
    def marker(self) -> str:
        """A glyph carrying the same distinction as the wording."""
        return _STATE_MARKERS[self]


_STATE_LABELS: dict[LifecycleState, str] = {
    LifecycleState.ACTIVE: "Active",
    LifecycleState.REJECTED_PENDING_RESCAN: "REJECTED — RESCAN REQUIRED",
    LifecycleState.SUPERSEDED_BY_REPLACEMENT: "Rejected — replaced by rescan",
    LifecycleState.REIMPORT_OF_REJECTED: "Re-import of a rejected scan",
    LifecycleState.EXCLUDED: "REJECTED — EXCLUDED FROM RESULTS",
    LifecycleState.DEFERRED: "DEFERRED — DECISION POSTPONED",
}

_STATE_MARKERS: dict[LifecycleState, str] = {
    LifecycleState.ACTIVE: "✓",
    LifecycleState.REJECTED_PENDING_RESCAN: "✖",  # heavy multiplication x
    LifecycleState.SUPERSEDED_BY_REPLACEMENT: "⇄",  # replaced
    LifecycleState.REIMPORT_OF_REJECTED: "⧉",  # copy
    LifecycleState.EXCLUDED: "⊘",  # circled division slash
    LifecycleState.DEFERRED: "⏸",  # pause
}

STATE_EXPLANATIONS: dict[LifecycleState, str] = {
    LifecycleState.EXCLUDED: (
        "Reject / Exclude: you have decided this sheet must not take part in "
        "this examination's results - a duplicate copy, an accidental scan, "
        "the wrong exam's sheet, a blank or administrative page. It leaves "
        "reconciliation, duplicate detection, scoring and every result and "
        "export. Nothing is deleted: the scan, its recognition result, the "
        "original image and the history are all kept, and Restore brings it "
        "back. No rescan is expected (use Resolve > Reject & Rescan for that)."
    ),
    LifecycleState.DEFERRED: (
        "Defer: you have postponed the decision. While deferred the sheet is "
        "left out of reconciliation counts, scoring and results, but it is "
        "still an outstanding review item - counted, listed under Deferred, "
        "and warned about on the Results stage - until you Restore it to "
        "active review or Reject / Exclude it. Nothing is deleted."
    ),
}
"""What each Attendance disposition means, for tooltips and confirmations."""


class FileState(StrEnum):
    """Whether a rejected scan's image is still where it was."""

    PRESENT = "present"
    """Untouched - the file is wherever it was imported from."""

    QUARANTINED = "quarantined"
    """Moved into the project's quarantine folder by *Purge Rejects*."""

    PURGED = "purged"
    """Permanently deleted by *Purge Rejects*. The database row, the rejection
    record and the audit history remain."""

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            FileState.PRESENT: "Image present",
            FileState.QUARANTINED: "Image quarantined",
            FileState.PURGED: "Image permanently removed",
        }[self]

    @property
    def unavailable_note(self) -> str:
        """Why nothing can reactivate the scan, for an image that is gone.

        One sentence, shown by the case panel beside the disabled actions and
        returned by the service when either action is attempted anyway. Empty
        for :attr:`PRESENT`. Restoring a quarantined file by hand does not
        change the recorded state; that restoration is not supported yet.
        """
        where = {
            FileState.PRESENT: "",
            FileState.QUARANTINED: "it was moved to the project's quarantine folder",
            FileState.PURGED: "it was permanently deleted",
        }[self]
        if not where:
            return ""
        return (
            f"Original image is no longer available ({where}); the rejection "
            "cannot be undone and the replacement link is kept."
        )


class RejectionReason(StrEnum):
    """Why an operator judged a scan unusable."""

    FOLDED = "folded"
    POOR_QUALITY = "poor_quality"
    REGISTRATION = "registration"
    CLIPPED = "clipped"
    SKEW = "skew"
    WRONG_DOCUMENT = "wrong_document"
    ID_UNREADABLE = "id_unreadable"
    DUPLICATE = "duplicate"
    ACCIDENTAL_SCAN = "accidental_scan"
    BLANK_UNUSABLE = "blank_unusable"
    OTHER = "other"

    @property
    def label(self) -> str:
        """The wording shown in the reason selector."""
        return _REASON_LABELS[self]

    @property
    def requires_note(self) -> bool:
        """Whether a note must accompany this reason."""
        return self is RejectionReason.OTHER


_REASON_LABELS: dict[RejectionReason, str] = {
    RejectionReason.FOLDED: "Folded / physically damaged",
    RejectionReason.POOR_QUALITY: "Poor image quality",
    RejectionReason.REGISTRATION: "Registration / geometry failure",
    RejectionReason.CLIPPED: "Clipped page",
    RejectionReason.SKEW: "Severe skew / perspective",
    RejectionReason.WRONG_DOCUMENT: "Wrong page / wrong document",
    RejectionReason.ID_UNREADABLE: "Student ID unreadable",
    RejectionReason.DUPLICATE: "Duplicate scan of a kept script",
    RejectionReason.ACCIDENTAL_SCAN: "Accidental / unwanted scan",
    RejectionReason.BLANK_UNUSABLE: "Blank or unusable sheet",
    RejectionReason.OTHER: "Other (explain in the note)",
}

RESCAN_REASONS: tuple[RejectionReason, ...] = (
    RejectionReason.FOLDED,
    RejectionReason.POOR_QUALITY,
    RejectionReason.REGISTRATION,
    RejectionReason.CLIPPED,
    RejectionReason.SKEW,
    RejectionReason.WRONG_DOCUMENT,
    RejectionReason.ID_UNREADABLE,
    RejectionReason.OTHER,
)
"""Reasons offered by *Reject & Rescan* - why a sheet must be scanned again."""

EXCLUSION_REASONS: tuple[RejectionReason, ...] = (
    RejectionReason.WRONG_DOCUMENT,
    RejectionReason.ACCIDENTAL_SCAN,
    RejectionReason.DUPLICATE,
    RejectionReason.BLANK_UNUSABLE,
    RejectionReason.OTHER,
)
"""Reasons offered by *Reject / Exclude* - why a sheet takes no part at all."""


class LifecycleAction(StrEnum):
    """What one lifecycle audit event records.

    Written to the shared append-only ledger under ``entity_type`` of
    :data:`LIFECYCLE_ENTITY` (``entity_id`` is the scan id) or
    :data:`EXPORT_ENTITY`. Every value fits the ledger's 20-character
    ``action`` column.
    """

    REJECTED = "rejected"
    """``ACTIVE -> REJECTED_PENDING_RESCAN``."""

    REJECT_UNDONE = "reject_undone"
    """``REJECTED_PENDING_RESCAN -> ACTIVE``."""

    REPLACED = "replaced"
    """``REJECTED_PENDING_RESCAN -> SUPERSEDED_BY_REPLACEMENT``, on the
    original."""

    LINKED_AS_REPLACEMENT = "linked_replacement"
    """The same confirmation, recorded on the replacement scan's own history."""

    REPLACEMENT_REMOVED = "replacement_removed"
    """``SUPERSEDED_BY_REPLACEMENT -> REJECTED_PENDING_RESCAN``: a mistaken
    link withdrawn. Recorded on both scans."""

    REIMPORT_LINKED = "reimport_linked"
    """A scan with a rejected scan's exact bytes was linked back to it."""

    IMAGE_QUARANTINED = "image_quarantined"
    """A superseded original's image moved into project quarantine."""

    IMAGE_PURGED = "image_purged"
    """A superseded original's image permanently deleted."""

    EXPORT_INCOMPLETE = "export_incomplete"
    """A final export produced while rescans were outstanding, acknowledged
    by a named operator."""

    EXCLUDED = "excluded"
    """``ACTIVE -> EXCLUDED``: Reject / Exclude from the Attendance stage."""

    DEFERRED = "deferred"
    """``ACTIVE -> DEFERRED``: the decision postponed."""

    RESTORED = "restored"
    """``EXCLUDED`` or ``DEFERRED`` ``-> ACTIVE``. ``previous_value`` says
    which."""

    DISPOSITION_CHANGED = "disposition_changed"
    """``DEFERRED -> EXCLUDED``: a postponed decision taken."""

    KEPT_CANONICAL = "kept_canonical"
    """Recorded on the script an operator kept when resolving a duplicate
    group; each other copy records its own :attr:`EXCLUDED`."""

    @property
    def label(self) -> str:
        """Operator-facing wording, for a history view."""
        return {
            LifecycleAction.REJECTED: "Rejected - rescan required",
            LifecycleAction.REJECT_UNDONE: "Rejection undone",
            LifecycleAction.REPLACED: "Replaced by a confirmed rescan",
            LifecycleAction.LINKED_AS_REPLACEMENT: "Confirmed as the rescan",
            LifecycleAction.REPLACEMENT_REMOVED: "Replacement link removed",
            LifecycleAction.REIMPORT_LINKED: "Re-import of rejected content",
            LifecycleAction.IMAGE_QUARANTINED: "Image moved to quarantine",
            LifecycleAction.IMAGE_PURGED: "Image permanently deleted",
            LifecycleAction.EXPORT_INCOMPLETE: "Incomplete results exported",
            LifecycleAction.EXCLUDED: "Rejected / excluded from results",
            LifecycleAction.DEFERRED: "Deferred - decision postponed",
            LifecycleAction.RESTORED: "Restored to active review",
            LifecycleAction.DISPOSITION_CHANGED: "Disposition changed",
            LifecycleAction.KEPT_CANONICAL: "Kept as the candidate's script",
        }[self]


LIFECYCLE_ENTITY = "scan_lifecycle"
"""``audit_event.entity_type`` for lifecycle events; ``entity_id`` is the scan
id. Distinct from reconciliation's ``script`` entity so neither history view
has to interpret the other's actions."""

EXPORT_ENTITY = "results_export"
"""``audit_event.entity_type`` for an acknowledged incomplete export;
``entity_id`` is the set code."""


class PurgeMode(StrEnum):
    """What *Purge Rejects* does with an eligible image."""

    QUARANTINE = "quarantine"
    """Move it into ``<project>/quarantine/``. Recoverable by hand. The
    default, and the recommendation for alpha builds."""

    DELETE = "delete"
    """Remove it permanently."""


@dataclass(frozen=True, slots=True)
class RescanCase:
    """One rejected scan and everything recorded about it.

    A detached value object: the GUI can hold hundreds without holding ORM
    rows. Everything needed to understand the case - which file, why, by
    whom, what replaced it - is here even after the image has been purged.

    Attributes:
        scan_id: The rejected (original) scan.
        batch_id: Its batch.
        state: Its lifecycle state.
        reason: Why it was rejected.
        note: The operator's note.
        declared_candidate_id: A Student ID the operator supplied *for this
            case* - used to identify the case and find its rescan, never as the
            scan's effective Student ID.
        declared_set_code: Likewise for the set code.
        recognised_candidate_id: The effective Student ID at rejection time.
        recognised_set_code: The effective set code at rejection time.
        source_name: The original file name.
        source_path: Where it was imported from.
        content_sha256: Its content hash at rejection time, when known.
        rejected_by / rejected_at: Who and when.
        replacement_scan_id: The confirmed rescan, when there is one.
        replacement_name: That rescan's file name.
        replacement_batch_id: The batch it was read into - possibly not this
            case's own; it counts in this case's batch either way.
        replaced_by / replaced_at: Who confirmed it, and when.
        reimport_of_scan_id: For a :attr:`LifecycleState.REIMPORT_OF_REJECTED`
            record, the rejected scan whose bytes it repeats.
        file_state: Whether the image is still present.
        file_action_at: When it was quarantined or purged.
    """

    scan_id: int
    batch_id: str
    state: LifecycleState
    reason: RejectionReason | None = None
    note: str = ""
    declared_candidate_id: str = ""
    declared_set_code: str = ""
    recognised_candidate_id: str = ""
    recognised_set_code: str = ""
    source_name: str = ""
    source_path: str = ""
    content_sha256: str = ""
    rejected_by: str = ""
    rejected_at: datetime | None = None
    replacement_scan_id: int | None = None
    replacement_name: str = ""
    replacement_batch_id: str = ""
    replaced_by: str = ""
    replaced_at: datetime | None = None
    reimport_of_scan_id: int | None = None
    file_state: FileState = FileState.PRESENT
    file_action_at: datetime | None = None

    @property
    def identity(self) -> str:
        """The Student ID this case is known by, or ``""`` when unknown.

        The operator's declared ID wins; otherwise the recognised one, but only
        when it is fully read - an ID with an unread position is not an
        identity anybody can look a rescan up by.
        """
        if self.declared_candidate_id:
            return self.declared_candidate_id
        return _reliable(self.recognised_candidate_id)

    @property
    def set_code(self) -> str:
        """The set code this case is known by, or ``""`` when unknown."""
        if self.declared_set_code:
            return self.declared_set_code
        return _reliable(self.recognised_set_code)

    @property
    def is_outstanding(self) -> bool:
        """Whether a rescan is still awaited."""
        return self.state.is_outstanding

    @property
    def reason_label(self) -> str:
        """The reason's wording, with the note when there is one."""
        if self.reason is None:
            return self.note
        return f"{self.reason.label} - {self.note}" if self.note else self.reason.label

    @property
    def status_text(self) -> str:
        """One line saying where the case stands."""
        if self.state is LifecycleState.SUPERSEDED_BY_REPLACEMENT:
            where = self.replacement_name or f"scan {self.replacement_scan_id}"
            text = f"{self.state.label}: {where}"
        else:
            text = self.state.label
        if self.file_state is not FileState.PRESENT:
            text += f" · {self.file_state.label}"
        return text


def _reliable(value: str) -> str:
    """``value`` when it has no unread position, else ``""``."""
    cleaned = value.strip()
    if not cleaned or "?" in cleaned or "_" in cleaned:
        return ""
    return cleaned


@dataclass(frozen=True, slots=True)
class ReplacementCandidate:
    """A scan that might be the rescan of a rejected sheet.

    **A suggestion, never a decision.** Nothing in the application links a
    candidate by itself; the operator confirms it explicitly.

    Attributes:
        scan_id: The new scan.
        source_name: Its file name.
        candidate_id: Its effective Student ID.
        set_code: Its effective set code.
        set_code_agrees: Whether that set code matches the case's, or ``None``
            when either is unknown. Additional evidence only.
        batch_id: The batch the scan was read into.
        batch_label: That batch as an operator reads it.
        other_batch: Whether it is a different batch from the rejected
            sheet's. Said on screen so the operator knows where it came from -
            never used as evidence either way.
        read_at: When it was read, when known.
    """

    scan_id: int
    source_name: str
    candidate_id: str
    set_code: str = ""
    set_code_agrees: bool | None = None
    batch_id: str = ""
    batch_label: str = ""
    other_batch: bool = False
    read_at: datetime | None = None

    @property
    def evidence(self) -> str:
        """Why this scan is suggested, in an operator's words."""
        base = f"Same Student ID ({self.candidate_id})"
        if self.set_code_agrees is True:
            return f"{base}; same set code ({self.set_code})"
        if self.set_code_agrees is False:
            return f"{base}; set code differs ({self.set_code or 'unread'})"
        return f"{base}; set code not compared"


@dataclass(frozen=True, slots=True)
class RescanCounts:
    """How many rejected scans a batch has in each state."""

    outstanding: int = 0
    superseded: int = 0
    reimports: int = 0
    excluded: int = 0
    deferred: int = 0

    @property
    def total(self) -> int:
        """Every Reject & Rescan record that is not undone.

        Excluded and deferred sheets are Attendance dispositions, counted on
        their own, not rescan cases.
        """
        return self.outstanding + self.superseded + self.reimports


@dataclass(frozen=True, slots=True)
class PurgeFile:
    """One file *Purge Rejects* would move or delete.

    Attributes:
        path: Its validated, resolved location inside project storage.
        size_bytes: Its size.
        role: ``"source"`` for the scan itself, ``"copy"`` for a renamed
            output copy, ``"quarantined"`` for a file already in quarantine.
    """

    path: str
    size_bytes: int
    role: str


@dataclass(frozen=True, slots=True)
class PurgeItem:
    """One superseded original, and what purging it would touch.

    Attributes:
        case: The rejection record.
        files: Project-owned files that may be removed.
        skipped: One sentence per file that will be left in place, and why -
            outside project storage, changed since import, still used by
            another scan.
    """

    case: RescanCase
    files: tuple[PurgeFile, ...] = ()
    skipped: tuple[str, ...] = ()

    @property
    def size_bytes(self) -> int:
        """How much purging this item would free."""
        return sum(item.size_bytes for item in self.files)


@dataclass(frozen=True, slots=True)
class PurgePlan:
    """What *Purge Rejects* would do, before it does anything.

    Attributes:
        eligible: Superseded originals whose replacement is active.
        pending: Rejected scans still awaiting rescan - never removed.
        blocked: Superseded originals that are not eligible (their
            replacement is itself rejected, say), with the reason.
    """

    eligible: tuple[PurgeItem, ...] = ()
    pending: int = 0
    blocked: tuple[tuple[RescanCase, str], ...] = field(default_factory=tuple)

    @property
    def size_bytes(self) -> int:
        """How much the whole purge would free."""
        return sum(item.size_bytes for item in self.eligible)

    @property
    def removable_files(self) -> int:
        """How many files the purge would actually touch."""
        return sum(len(item.files) for item in self.eligible)


@dataclass(frozen=True, slots=True)
class PurgeOutcome:
    """What a purge actually did."""

    mode: PurgeMode
    processed: int = 0
    files_moved: int = 0
    bytes_freed: int = 0
    skipped: tuple[str, ...] = ()


def format_bytes(size: int) -> str:
    """Render a byte count for a summary line."""
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"  # pragma: no cover - loop always returns


__all__ = [
    "EXCLUSION_REASONS",
    "EXPORT_ENTITY",
    "LIFECYCLE_ENTITY",
    "RESCAN_REASONS",
    "STATE_EXPLANATIONS",
    "FileState",
    "LifecycleAction",
    "LifecycleState",
    "PurgeFile",
    "PurgeItem",
    "PurgeMode",
    "PurgeOutcome",
    "PurgePlan",
    "RejectionReason",
    "ReplacementCandidate",
    "RescanCase",
    "RescanCounts",
    "format_bytes",
]
