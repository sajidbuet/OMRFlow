"""The scan-quality decision layer: evidence in, decision out (0.1.1 revised phase 7).

Purpose:
    Interpret the evidence recognition already recorded about one sheet - did
    the file decode, did the page register, what did the page-geometry check
    (:class:`~omr_scanner.domain.scan_quality.ScanQualityAssessment`) find -
    as one of a few operator-facing decisions, with the reasons behind it.
    ``ARCHITECTURE_NOTES.md`` §12. Pure: no database, no Qt, no images.

The decisions:
    * :attr:`QualityDecision.ACCEPT` - nothing in the evidence argues against
      the sheet.
    * :attr:`QualityDecision.ACCEPT_WITH_WARNING` - usable, but a human should
      look (the existing ``scan_quality`` conflict remains the place they do).
    * :attr:`QualityDecision.RESCAN_REQUIRED` - the evidence says the *paper*
      should be scanned again. This produces a **suggested** rejection only;
      an operator confirms it through the existing Reject & Rescan flow.
    * :attr:`QualityDecision.RETRY_PROCESSING` - neither: the *software* failed
      (an unexpected error, a worker that died, a template the engine could not
      apply). A software fault is not a paper fault, so it never suggests a
      rescan; reading the sheet again is the remedy.

What this module never does:
    * Measure anything, or add a threshold. The geometry thresholds stay
      :class:`~omr_scanner.domain.scan_quality.ScanQualityThresholds`; this
      layer only maps their verdicts.
    * Reject, supersede or replace a scan. Those remain named-operator
      actions in :mod:`omr_scanner.services.scan_lifecycle`.
    * Treat answer ambiguity as anything: it is a reading, not a quality fact.

The policy is data:
    :class:`QualityPolicy` is versioned and fingerprinted (SHA-256 of its
    canonical JSON). A scan session **pins** the policy it first evaluated with
    (``scan_session.quality_policy_json``), so a later change of the
    application's default never silently re-interprets a session that is
    already scanning. Every stored decision records the fingerprint that
    produced it.

    **The default policy is an UNVALIDATED DEFAULT.** It is the starting
    mapping of ``ARCHITECTURE_NOTES.md`` §12, chosen by engineering judgement.
    It has not been calibrated against real rejected and rescanned examination
    scripts; that is later qualification work.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from omr_scanner.domain.scan_lifecycle import RejectionReason
from omr_scanner.domain.scan_quality import ScanQualityIssueCode, ScanQualityStatus

UNVALIDATED_NOTE = (
    "UNVALIDATED DEFAULT: the initial mapping of ARCHITECTURE_NOTES section 12, chosen "
    "by engineering judgement. Not calibrated against real rejected or rescanned "
    "examination scripts."
)
"""The wording every surface showing the default policy carries."""


class QualityDecision(StrEnum):
    """What the evidence about one sheet means for the operator."""

    ACCEPT = "accept"
    ACCEPT_WITH_WARNING = "accept_with_warning"
    RESCAN_REQUIRED = "rescan_required"
    RETRY_PROCESSING = "retry_processing"
    """Not a statement about the paper: the software failed and the sheet
    should be read again."""

    @property
    def rank(self) -> int:
        """Combination order: the strongest decision of several reasons wins.

        ``RETRY_PROCESSING`` ranks highest because a software failure means the
        paper evidence is incomplete - there is nothing reliable to accept or
        reject until the sheet has been read.
        """
        return _DECISION_RANK[self]

    @property
    def suggests_rescan(self) -> bool:
        """Whether this decision produces a suggested rejection."""
        return self is QualityDecision.RESCAN_REQUIRED

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return _DECISION_LABELS[self]


_DECISION_RANK: dict[QualityDecision, int] = {
    QualityDecision.ACCEPT: 0,
    QualityDecision.ACCEPT_WITH_WARNING: 1,
    QualityDecision.RESCAN_REQUIRED: 2,
    QualityDecision.RETRY_PROCESSING: 3,
}

_DECISION_LABELS: dict[QualityDecision, str] = {
    QualityDecision.ACCEPT: "Accept",
    QualityDecision.ACCEPT_WITH_WARNING: "Accept with warning",
    QualityDecision.RESCAN_REQUIRED: "Rescan required (suggested)",
    QualityDecision.RETRY_PROCESSING: "Retry processing (software fault)",
}


class QualityReason(StrEnum):
    """One fact from the evidence, as a stable machine-readable code.

    Declared in the canonical order reasons are reported in.
    """

    IMAGE_NOT_DECODED = "image_not_decoded"
    """The file is not a decodable image (``IMAGE_LOAD_ERROR``, or an intake
    file that never decoded)."""

    REGISTRATION_FAILED = "registration_failed"
    """The page could not be rectified (outcome ``registration_failed``)."""

    QUALITY_UNUSABLE = "quality_unusable"
    """The geometry check's verdict is ``UNUSABLE``."""

    QUALITY_REVIEW = "quality_review"
    """The geometry check's verdict is ``REVIEW``."""

    GEOMETRY_NOT_VERIFIED = "geometry_not_verified"
    """The check ran and could not confirm the page geometry
    (``GEOMETRY_NOT_VERIFIED`` issue)."""

    UNKNOWN_EVIDENCE = "unknown_evidence"
    """A quality status this build does not know (written by a newer one)."""

    PROCESSING_ERROR = "processing_error"
    """Recognition failed unexpectedly, or its worker process died."""

    TEMPLATE_ERROR = "template_error"
    """The template could not describe the sheet (``INVALID_TEMPLATE``)."""

    ALIGNMENT_WARNING = "alignment_warning"
    """Registered with a reservation. Informational; its own conflict remains."""

    QUALITY_NOT_EVALUATED = "quality_not_evaluated"
    """The geometry check could not run (template offers too little printing,
    or a pre-check build). Informational: "not contradicted", never "confirmed"."""


_REASON_ORDER: dict[QualityReason, int] = {item: index for index, item in enumerate(QualityReason)}


@dataclass(frozen=True, slots=True)
class QualityEvidence:
    """What recognition recorded about one **read** sheet, reduced to the facts used here.

    Built by :func:`omr_scanner.services.quality_decisions.evidence_from_result`
    from a stored result, or directly for an intake file that never decoded.

    Attributes:
        decode_failed: The image could not be decoded.
        registration_failed: The page could not be registered.
        processing_error: The software failed (not the paper).
        template_error: The template could not be applied.
        quality_status: The geometry verdict's value (``pass`` / ``review`` /
            ``unusable``), or ``""`` when no assessment exists.
        quality_evaluated: Whether the geometry check reached a conclusion.
        issue_codes: The assessment's issue codes, most severe first.
        unusable_issue_codes: Those issues whose own status is ``unusable``.
        areas: Affected page areas, in reading order (provenance only).
        alignment_warning: Registered with a warning.
    """

    decode_failed: bool = False
    registration_failed: bool = False
    processing_error: bool = False
    template_error: bool = False
    quality_status: str = ""
    quality_evaluated: bool = False
    issue_codes: tuple[str, ...] = ()
    unusable_issue_codes: tuple[str, ...] = ()
    areas: tuple[str, ...] = ()
    alignment_warning: bool = False

    def reasons(self) -> tuple[QualityReason, ...]:
        """Every :class:`QualityReason` the evidence supports, in canonical order."""
        found: set[QualityReason] = set()
        if self.decode_failed:
            found.add(QualityReason.IMAGE_NOT_DECODED)
        if self.processing_error:
            found.add(QualityReason.PROCESSING_ERROR)
        if self.template_error:
            found.add(QualityReason.TEMPLATE_ERROR)
        if self.registration_failed:
            found.add(QualityReason.REGISTRATION_FAILED)
        failed = self.decode_failed or self.processing_error or self.registration_failed
        status = self.quality_status
        if not failed:
            # A page that never registered carries a geometry assessment only
            # as a by-product of the failure (recognition records it as not
            # verifiable): the failure is the evidence, not a second finding.
            if status == ScanQualityStatus.UNUSABLE.value:
                found.add(QualityReason.QUALITY_UNUSABLE)
            elif status == ScanQualityStatus.REVIEW.value:
                found.add(QualityReason.QUALITY_REVIEW)
            elif status not in ("", ScanQualityStatus.PASS.value):
                found.add(QualityReason.UNKNOWN_EVIDENCE)
            if ScanQualityIssueCode.GEOMETRY_NOT_VERIFIED.value in self.issue_codes:
                found.add(QualityReason.GEOMETRY_NOT_VERIFIED)
        if self.alignment_warning and not failed:
            found.add(QualityReason.ALIGNMENT_WARNING)
        if (
            not failed
            and not self.template_error
            and not self.quality_evaluated
            and status in ("", ScanQualityStatus.PASS.value)
            and QualityReason.GEOMETRY_NOT_VERIFIED not in found
        ):
            found.add(QualityReason.QUALITY_NOT_EVALUATED)
        return tuple(sorted(found, key=_REASON_ORDER.__getitem__))


@dataclass(frozen=True, slots=True)
class QualityPolicy:
    """How evidence becomes a decision: versioned, fingerprinted data.

    Attributes:
        version: Increases whenever the meaning changes.
        rules: The decision each :class:`QualityReason` calls for. A sheet's
            decision is the strongest (:attr:`QualityDecision.rank`) of its
            reasons'; a sheet with no reason is accepted.
        rejection_reasons: For a ``RESCAN_REQUIRED`` sheet, which existing
            :class:`~omr_scanner.domain.scan_lifecycle.RejectionReason` to
            suggest - the first entry whose key matches one of the sheet's
            reasons or *unusable* issue codes, in this order (order matters,
            and is part of the fingerprint).
        validated: Whether real-data qualification has validated the policy.
            ``False`` for the default - see :data:`UNVALIDATED_NOTE`.
        name: A human label (not part of the fingerprint).
        note: Human wording about its status (not part of the fingerprint).
    """

    version: int
    rules: Mapping[QualityReason, QualityDecision]
    rejection_reasons: tuple[tuple[str, RejectionReason], ...]
    validated: bool = False
    name: str = "OMRFlow default scan-quality policy"
    note: str = UNVALIDATED_NOTE
    _fingerprint: str = field(default="", init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Refuse a policy that leaves a reason undecided or suggests an Attendance reason."""
        missing = [item.value for item in QualityReason if item not in self.rules]
        if missing:
            raise ValueError(f"QualityPolicy leaves reasons undecided: {', '.join(missing)}")
        for _key, reason in self.rejection_reasons:
            if reason not in _RESCAN_REASON_SET:
                raise ValueError(
                    f"QualityPolicy may only suggest Reject & Rescan reasons, not {reason.value!r}"
                )
        object.__setattr__(self, "_fingerprint", _digest(self.canonical()))

    # --- identity --------------------------------------------------------
    def canonical(self) -> dict[str, Any]:
        """The fingerprinted content: everything that changes a decision.

        ``rules`` is a mapping, so its keys are sorted (their order means
        nothing); ``rejection_reasons`` is a precedence list, so its order is
        kept (it means something). Human wording is left out.
        """
        return {
            "kind": "omrflow.scan_quality_policy",
            "version": self.version,
            "validated": self.validated,
            "rules": {reason.value: self.rules[reason].value for reason in self.rules},
            "rejection_reasons": [[key, reason.value] for key, reason in self.rejection_reasons],
        }

    @property
    def fingerprint(self) -> str:
        """SHA-256 of :meth:`canonical` as sorted, compact JSON."""
        return self._fingerprint

    def to_json(self) -> str:
        """The stored form (canonical content plus the human name and note)."""
        data = self.canonical()
        data["name"] = self.name
        data["note"] = self.note
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> QualityPolicy:
        """Read a stored policy. Unknown reasons or decisions are refused."""
        data = json.loads(text)
        if data.get("kind") != "omrflow.scan_quality_policy":
            raise ValueError("not a stored scan-quality policy")
        return cls(
            version=int(data["version"]),
            rules={
                QualityReason(key): QualityDecision(value)
                for key, value in dict(data["rules"]).items()
            },
            rejection_reasons=tuple(
                (str(key), RejectionReason(value)) for key, value in data["rejection_reasons"]
            ),
            validated=bool(data.get("validated", False)),
            name=str(data.get("name", "")),
            note=str(data.get("note", "")),
        )

    # --- decisions ---------------------------------------------------------
    def decide(self, evidence: QualityEvidence) -> QualityVerdict:
        """The decision ``evidence`` calls for under this policy. Pure, deterministic."""
        reasons = evidence.reasons()
        decision = QualityDecision.ACCEPT
        for reason in reasons:
            candidate = self.rules[reason]
            if candidate.rank > decision.rank:
                decision = candidate
        suggested = (
            self._suggested_reason(reasons, evidence.unusable_issue_codes)
            if decision.suggests_rescan
            else None
        )
        return QualityVerdict(
            decision=decision,
            reasons=reasons,
            issue_codes=tuple(evidence.issue_codes),
            areas=tuple(evidence.areas),
            suggested_rejection=suggested,
            policy_version=self.version,
            policy_fingerprint=self.fingerprint,
        )

    def _suggested_reason(
        self, reasons: Sequence[QualityReason], unusable_codes: Iterable[str]
    ) -> RejectionReason:
        keys = {reason.value for reason in reasons if self.rules[reason].suggests_rescan}
        keys |= set(unusable_codes) if QualityReason.QUALITY_UNUSABLE in reasons else set()
        for key, reason in self.rejection_reasons:
            if key in keys:
                return reason
        return RejectionReason.POOR_QUALITY


_RESCAN_REASON_SET = frozenset(
    {
        RejectionReason.FOLDED,
        RejectionReason.POOR_QUALITY,
        RejectionReason.REGISTRATION,
        RejectionReason.CLIPPED,
        RejectionReason.SKEW,
        RejectionReason.WRONG_DOCUMENT,
        RejectionReason.ID_UNREADABLE,
        RejectionReason.OTHER,
    }
)
""":data:`~omr_scanner.domain.scan_lifecycle.RESCAN_REASONS`, as a set."""


def _digest(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class QualityVerdict:
    """One sheet's decision, why, and which policy produced it.

    Attributes:
        decision: The decision.
        reasons: Every reason found, in canonical order (informational ones
            included - an accepted sheet may still say *not evaluated*).
        issue_codes: The geometry issue codes behind it (provenance).
        areas: The page areas they concern (provenance).
        suggested_rejection: For ``RESCAN_REQUIRED``, the existing rejection
            reason an operator is offered. ``None`` otherwise. **A suggestion,
            never a rejection.**
        policy_version / policy_fingerprint: The policy that decided.
    """

    decision: QualityDecision
    reasons: tuple[QualityReason, ...]
    issue_codes: tuple[str, ...] = ()
    areas: tuple[str, ...] = ()
    suggested_rejection: RejectionReason | None = None
    policy_version: int = 0
    policy_fingerprint: str = ""


DEFAULT_POLICY = QualityPolicy(
    version=1,
    rules={
        QualityReason.IMAGE_NOT_DECODED: QualityDecision.RESCAN_REQUIRED,
        QualityReason.REGISTRATION_FAILED: QualityDecision.RESCAN_REQUIRED,
        QualityReason.QUALITY_UNUSABLE: QualityDecision.RESCAN_REQUIRED,
        QualityReason.QUALITY_REVIEW: QualityDecision.ACCEPT_WITH_WARNING,
        QualityReason.GEOMETRY_NOT_VERIFIED: QualityDecision.ACCEPT_WITH_WARNING,
        QualityReason.UNKNOWN_EVIDENCE: QualityDecision.ACCEPT_WITH_WARNING,
        QualityReason.PROCESSING_ERROR: QualityDecision.RETRY_PROCESSING,
        QualityReason.TEMPLATE_ERROR: QualityDecision.RETRY_PROCESSING,
        QualityReason.ALIGNMENT_WARNING: QualityDecision.ACCEPT,
        QualityReason.QUALITY_NOT_EVALUATED: QualityDecision.ACCEPT,
    },
    rejection_reasons=(
        (QualityReason.IMAGE_NOT_DECODED.value, RejectionReason.POOR_QUALITY),
        (QualityReason.REGISTRATION_FAILED.value, RejectionReason.REGISTRATION),
        (ScanQualityIssueCode.PARTIAL_PAGE.value, RejectionReason.CLIPPED),
        (ScanQualityIssueCode.PAGE_GEOMETRY_DISTORTION.value, RejectionReason.FOLDED),
        (ScanQualityIssueCode.REGION_REGISTRATION_ERROR.value, RejectionReason.FOLDED),
        (ScanQualityIssueCode.MARKER_GEOMETRY_ERROR.value, RejectionReason.REGISTRATION),
        (ScanQualityIssueCode.CRITICAL_REGION_UNREADABLE.value, RejectionReason.ID_UNREADABLE),
        (QualityReason.QUALITY_UNUSABLE.value, RejectionReason.POOR_QUALITY),
    ),
    validated=False,
)
"""The policy a session pins when it first evaluates a sheet. **UNVALIDATED.**

Exactly the mapping of ``ARCHITECTURE_NOTES.md`` §12:

========================  =======================================
Evidence                  Decision
========================  =======================================
image does not decode     RESCAN_REQUIRED (suggest *poor quality*)
registration failed       RESCAN_REQUIRED (suggest *registration*)
``UNUSABLE``              RESCAN_REQUIRED (reason from the issue)
``REVIEW``                ACCEPT_WITH_WARNING
``GEOMETRY_NOT_VERIFIED`` ACCEPT_WITH_WARNING
processing / template     RETRY_PROCESSING (never a rescan)
========================  =======================================

plus three rows the table implies: a quality status this build does not know
is looked at (ACCEPT_WITH_WARNING); an alignment warning and an assessment
that could not run are recorded but do not escalate (their own conflicts, where
the conflict policy raises them, are unchanged).
"""


__all__ = [
    "DEFAULT_POLICY",
    "UNVALIDATED_NOTE",
    "QualityDecision",
    "QualityEvidence",
    "QualityPolicy",
    "QualityReason",
    "QualityVerdict",
]
