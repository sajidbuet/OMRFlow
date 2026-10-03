"""The scan-quality decision layer (0.1.1 revised phase 7, ARCHITECTURE_NOTES §12).

Table-driven over every row of the policy, with the evidence built the way the
service builds it from a real stored result (``evidence_from_result``), plus
the policy's version, fingerprint and the suggested-rejection mapping.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from omr_scanner.domain.quality_decision import (
    DEFAULT_POLICY,
    UNVALIDATED_NOTE,
    QualityDecision,
    QualityEvidence,
    QualityPolicy,
    QualityReason,
)
from omr_scanner.domain.scan_lifecycle import RESCAN_REASONS, RejectionReason
from omr_scanner.domain.scan_quality import (
    DEFAULT_THRESHOLDS,
    ScanQualityAssessment,
    ScanQualityIssue,
    ScanQualityIssueCode,
    ScanQualityStatus,
    ScanQualityThresholds,
)
from omr_scanner.services.quality_decisions import decode_failure_verdict, evidence_from_result
from omr_scanner.services.recognition_models import (
    RecognitionOutcome,
    RegistrationStatus,
    ScanResult,
    StatusCode,
)

D = QualityDecision
R = QualityReason
Code = ScanQualityIssueCode
Status = ScanQualityStatus


def issue(code: ScanQualityIssueCode, status: ScanQualityStatus) -> ScanQualityIssue:
    return ScanQualityIssue(code=code, status=status, detail="test")


def assessment(*issues: ScanQualityIssue, evaluated: bool = True) -> ScanQualityAssessment:
    ordered = tuple(sorted(issues, key=lambda item: -item.status.rank))
    return ScanQualityAssessment(
        status=Status.worse_of(*(item.status for item in ordered)),
        issues=ordered,
        evaluated=evaluated,
    )


def result(
    outcome: RecognitionOutcome = RecognitionOutcome.COMPLETE,
    *,
    registration: RegistrationStatus = RegistrationStatus.REGISTERED,
    codes: tuple[str, ...] = (),
    quality: ScanQualityAssessment | None = None,
) -> ScanResult:
    return ScanResult(
        source_path=Path("sheet.png"),
        outcome=outcome,
        registration=registration,
        status_codes=codes,
        scan_quality=quality,
    )


CLEAN = assessment()
FAILED_NOT_EVALUATED = ScanQualityAssessment(evaluated=False)

# (case id, stored result, decision, reasons, suggested rejection)
ROWS = [
    (
        "decode_failure",
        result(
            RecognitionOutcome.ERROR,
            registration=RegistrationStatus.FAILED,
            codes=(StatusCode.IMAGE_LOAD_ERROR.value, StatusCode.ALIGNMENT_FAILED.value),
            quality=FAILED_NOT_EVALUATED,
        ),
        D.RESCAN_REQUIRED,
        (R.IMAGE_NOT_DECODED,),
        RejectionReason.POOR_QUALITY,
    ),
    (
        "registration_failure",
        result(
            RecognitionOutcome.REGISTRATION_FAILED,
            registration=RegistrationStatus.FAILED,
            codes=(StatusCode.MARKER_NOT_FOUND.value, StatusCode.ALIGNMENT_FAILED.value),
            quality=FAILED_NOT_EVALUATED,
        ),
        D.RESCAN_REQUIRED,
        (R.REGISTRATION_FAILED,),
        RejectionReason.REGISTRATION,
    ),
    (
        "unusable_fold",
        result(
            RecognitionOutcome.REVIEW,
            codes=(StatusCode.SCAN_QUALITY_UNUSABLE.value,),
            quality=assessment(issue(Code.PAGE_GEOMETRY_DISTORTION, Status.UNUSABLE)),
        ),
        D.RESCAN_REQUIRED,
        (R.QUALITY_UNUSABLE,),
        RejectionReason.FOLDED,
    ),
    (
        "unusable_clipped_identifier",
        result(
            RecognitionOutcome.REVIEW,
            quality=assessment(
                issue(Code.PARTIAL_PAGE, Status.UNUSABLE),
                issue(Code.CRITICAL_REGION_UNREADABLE, Status.UNUSABLE),
            ),
        ),
        D.RESCAN_REQUIRED,
        (R.QUALITY_UNUSABLE,),
        RejectionReason.CLIPPED,
    ),
    (
        "unusable_identifier_unreadable",
        result(
            RecognitionOutcome.REVIEW,
            quality=assessment(issue(Code.CRITICAL_REGION_UNREADABLE, Status.UNUSABLE)),
        ),
        D.RESCAN_REQUIRED,
        (R.QUALITY_UNUSABLE,),
        RejectionReason.ID_UNREADABLE,
    ),
    (
        "unusable_marker_geometry",
        result(
            RecognitionOutcome.REVIEW,
            quality=assessment(issue(Code.MARKER_GEOMETRY_ERROR, Status.UNUSABLE)),
        ),
        D.RESCAN_REQUIRED,
        (R.QUALITY_UNUSABLE,),
        RejectionReason.REGISTRATION,
    ),
    (
        "review",
        result(
            RecognitionOutcome.REVIEW,
            codes=(StatusCode.SCAN_QUALITY_REVIEW.value,),
            quality=assessment(issue(Code.REGION_REGISTRATION_ERROR, Status.REVIEW)),
        ),
        D.ACCEPT_WITH_WARNING,
        (R.QUALITY_REVIEW,),
        None,
    ),
    (
        "geometry_not_verified",
        result(
            RecognitionOutcome.REVIEW,
            quality=assessment(issue(Code.GEOMETRY_NOT_VERIFIED, Status.REVIEW), evaluated=False),
        ),
        D.ACCEPT_WITH_WARNING,
        (R.QUALITY_REVIEW, R.GEOMETRY_NOT_VERIFIED),
        None,
    ),
    (
        "processing_error",
        result(
            RecognitionOutcome.ERROR,
            registration=RegistrationStatus.FAILED,
            codes=(StatusCode.PROCESSING_ERROR.value, StatusCode.ALIGNMENT_FAILED.value),
            quality=FAILED_NOT_EVALUATED,
        ),
        D.RETRY_PROCESSING,
        (R.PROCESSING_ERROR,),
        None,
    ),
    (
        "worker_died_pool_result",
        # recognition_pool.error_result: outcome error, registration failed,
        # no status codes at all - a software fault, never a paper fault.
        result(RecognitionOutcome.ERROR, registration=RegistrationStatus.FAILED),
        D.RETRY_PROCESSING,
        (R.PROCESSING_ERROR,),
        None,
    ),
    (
        "invalid_template",
        result(
            RecognitionOutcome.ERROR,
            registration=RegistrationStatus.FAILED,
            codes=(StatusCode.INVALID_TEMPLATE.value, StatusCode.PROCESSING_ERROR.value),
        ),
        D.RETRY_PROCESSING,
        (R.PROCESSING_ERROR, R.TEMPLATE_ERROR),
        None,
    ),
    ("clean", result(quality=CLEAN), D.ACCEPT, (), None),
    (
        "alignment_warning_only",
        result(
            registration=RegistrationStatus.REGISTERED_WITH_WARNING,
            codes=(StatusCode.ALIGNMENT_WARNING.value,),
            quality=CLEAN,
        ),
        D.ACCEPT,
        (R.ALIGNMENT_WARNING,),
        None,
    ),
    (
        "not_evaluated_by_template",
        result(quality=ScanQualityAssessment(evaluated=False)),
        D.ACCEPT,
        (R.QUALITY_NOT_EVALUATED,),
        None,
    ),
    ("no_assessment_at_all", result(quality=None), D.ACCEPT, (R.QUALITY_NOT_EVALUATED,), None),
    (
        "multiple_reasons_strongest_wins",
        result(
            RecognitionOutcome.REVIEW,
            registration=RegistrationStatus.REGISTERED_WITH_WARNING,
            quality=assessment(
                issue(Code.GEOMETRY_NOT_VERIFIED, Status.REVIEW),
                issue(Code.PAGE_GEOMETRY_DISTORTION, Status.UNUSABLE),
            ),
        ),
        D.RESCAN_REQUIRED,
        (R.QUALITY_UNUSABLE, R.GEOMETRY_NOT_VERIFIED, R.ALIGNMENT_WARNING),
        RejectionReason.FOLDED,
    ),
]


@pytest.mark.parametrize(
    ("stored", "decision", "reasons", "suggested"),
    [row[1:] for row in ROWS],
    ids=[row[0] for row in ROWS],
)
def test_every_policy_row(stored, decision, reasons, suggested):
    verdict = DEFAULT_POLICY.decide(evidence_from_result(stored))
    assert verdict.decision is decision
    assert verdict.reasons == reasons
    assert verdict.suggested_rejection is suggested
    assert verdict.policy_version == DEFAULT_POLICY.version == 1
    assert verdict.policy_fingerprint == DEFAULT_POLICY.fingerprint


def test_rows_survive_the_stored_round_trip():
    """Decisions are re-derived from ``result_json`` (recovery, backfill): same answer."""
    for _name, stored, decision, reasons, suggested in ROWS:
        reread = ScanResult.from_dict(json.loads(json.dumps(stored.to_dict())))
        verdict = DEFAULT_POLICY.decide(evidence_from_result(reread))
        assert (verdict.decision, verdict.reasons, verdict.suggested_rejection) == (
            decision,
            reasons,
            suggested,
        )


def test_unknown_quality_status_is_looked_at_never_accepted_silently():
    evidence = QualityEvidence(quality_status="crumpled", quality_evaluated=True)
    verdict = DEFAULT_POLICY.decide(evidence)
    assert verdict.decision is D.ACCEPT_WITH_WARNING
    assert verdict.reasons == (R.UNKNOWN_EVIDENCE,)


def test_an_intake_file_that_never_decodes_suggests_a_rescan():
    verdict = decode_failure_verdict()
    assert verdict.decision is D.RESCAN_REQUIRED
    assert verdict.suggested_rejection is RejectionReason.POOR_QUALITY


def test_a_software_fault_never_suggests_a_rescan_whatever_else_is_true():
    evidence = QualityEvidence(processing_error=True, quality_status="unusable")
    verdict = DEFAULT_POLICY.decide(evidence)
    assert verdict.decision is D.RETRY_PROCESSING
    assert verdict.suggested_rejection is None


def test_every_suggested_reason_is_an_existing_reject_and_rescan_reason():
    for _key, reason in DEFAULT_POLICY.rejection_reasons:
        assert reason in RESCAN_REASONS
    with pytest.raises(ValueError, match="Reject & Rescan"):
        QualityPolicy(
            version=9,
            rules=dict(DEFAULT_POLICY.rules),
            rejection_reasons=(("quality_unusable", RejectionReason.ACCIDENTAL_SCAN),),
        )


def test_a_policy_must_decide_every_reason():
    rules = dict(DEFAULT_POLICY.rules)
    rules.pop(R.QUALITY_REVIEW)
    with pytest.raises(ValueError, match="quality_review"):
        QualityPolicy(version=2, rules=rules, rejection_reasons=())


class TestVersionAndFingerprint:
    def test_the_default_is_labelled_unvalidated(self):
        assert DEFAULT_POLICY.validated is False
        assert "UNVALIDATED DEFAULT" in DEFAULT_POLICY.note == UNVALIDATED_NOTE
        stored = json.loads(DEFAULT_POLICY.to_json())
        assert stored["validated"] is False
        assert "UNVALIDATED DEFAULT" in stored["note"]

    def test_fingerprint_is_stable_and_reproducible(self):
        assert len(DEFAULT_POLICY.fingerprint) == 64
        again = QualityPolicy.from_json(DEFAULT_POLICY.to_json())
        assert again == DEFAULT_POLICY
        assert again.fingerprint == DEFAULT_POLICY.fingerprint

    def test_changing_content_changes_the_fingerprint(self):
        rules = dict(DEFAULT_POLICY.rules)
        rules[R.QUALITY_REVIEW] = D.RESCAN_REQUIRED
        stricter = dataclasses.replace(DEFAULT_POLICY, rules=rules)
        assert stricter.fingerprint != DEFAULT_POLICY.fingerprint
        assert (
            dataclasses.replace(DEFAULT_POLICY, version=2).fingerprint
            != DEFAULT_POLICY.fingerprint
        )
        assert (
            dataclasses.replace(DEFAULT_POLICY, validated=True).fingerprint
            != DEFAULT_POLICY.fingerprint
        )

    def test_precedence_order_is_content_but_mapping_order_is_not(self):
        reordered_rules = dataclasses.replace(
            DEFAULT_POLICY, rules=dict(reversed(list(DEFAULT_POLICY.rules.items())))
        )
        assert reordered_rules.fingerprint == DEFAULT_POLICY.fingerprint
        swapped = dataclasses.replace(
            DEFAULT_POLICY, rejection_reasons=tuple(reversed(DEFAULT_POLICY.rejection_reasons))
        )
        assert swapped.fingerprint != DEFAULT_POLICY.fingerprint

    def test_human_wording_is_not_content(self):
        renamed = dataclasses.replace(DEFAULT_POLICY, name="Renamed", note="other words")
        assert renamed.fingerprint == DEFAULT_POLICY.fingerprint

    def test_a_stored_policy_from_elsewhere_is_refused(self):
        with pytest.raises(ValueError):
            QualityPolicy.from_json(json.dumps({"kind": "something else"}))


def test_no_geometric_threshold_is_introduced():
    """The layer interprets ScanQualityThresholds' verdicts; their defaults are unchanged."""
    assert ScanQualityThresholds() == DEFAULT_THRESHOLDS
    import omr_scanner.domain.quality_decision as module

    numbers = [
        name for name, value in vars(module).items()
        if isinstance(value, float) and not name.startswith("_")
    ]
    assert numbers == []
