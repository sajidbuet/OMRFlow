"""Read a marked solution sheet as an answer key - and as nothing else.

Purpose:
    Run one image through the project's ordinary recognition engine, with the
    project's template (whose recognition settings *are* its calibration), and
    turn the result into something the Answer Key stage can show for review.

What a solution sheet is not:
    A candidate script. Nothing here touches a batch, the review ledger,
    attendance, reconciliation or results: the sheet is read in memory, and the
    only thing that can ever reach the database is an answer-key revision the
    operator confirms and saves. So a solution sheet can never appear as a
    candidate, create a duplicate-script conflict, or change a scan count.

What it records:
    The image's SHA-256 and the facts about how it was read (registration,
    set-code check, which questions were blank or multiple), because the file
    itself may be moved or deleted after the key is saved and the revision
    must still say what it came from.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from omr_scanner.services.answer_key import (
    QuestionPlan,
    ScannedKey,
    SetCodeVerdict,
    check_sheet_set,
    key_from_scan,
)
from omr_scanner.services.scan_provenance import hash_file

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence
    from pathlib import Path

    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services.recognition_models import ScanResult

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SolutionSheetReading:
    """One solution sheet, read and ready for an operator's review.

    Attributes:
        path: The image.
        result: The full recognition result, kept for the preview and its
            overlay; it is never stored.
        scanned: The answers as read, with every blank and multiple listed.
        verdict: How the sheet's set field compares with the chosen set.
        sha256: The image's content hash, or ``""`` if it could not be read.
    """

    path: Path
    result: ScanResult
    scanned: ScannedKey
    verdict: SetCodeVerdict
    sha256: str = ""

    @property
    def registered(self) -> bool:
        """Whether the page could be registered at all."""
        return self.scanned.registered

    def metadata(
        self, *, target_set: str, final_answers: str, decision: str = ""
    ) -> dict[str, Any]:
        """The facts a saved revision keeps about this reading.

        Args:
            target_set: The set the answers were finally imported into.
            final_answers: The answers as confirmed, so a later reader can see
                which questions a person changed from what was read.
            decision: How a set-code mismatch was resolved, when there was one.
        """
        read = self.scanned.answers
        corrected = [
            number
            for offset, number in enumerate(self.scanned.plan.numbers)
            if offset < len(final_answers)
            and offset < len(read)
            and final_answers[offset] != read[offset]
        ]
        return {
            "kind": "solution_sheet",
            "file_name": self.path.name,
            "read_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "engine": f"{self.result.engine_name} {self.result.engine_version}",
            "registration": str(self.result.registration),
            "sheet_set_code": self.verdict.read,
            "set_code_check": self.verdict.check.value,
            "set_code_decision": decision,
            "imported_into_set": target_set,
            "recognised_answers": read,
            "blank_at_read": list(self.scanned.blanks),
            "multiple_at_read": list(self.scanned.multiples),
            "low_confidence_at_read": list(self.scanned.low_confidence),
            "corrected_by_operator": corrected,
        }


def read_solution_sheet(
    path: Path,
    template: OmrTemplate,
    plan: QuestionPlan,
    *,
    selected_set: str,
    defined_sets: Sequence[str] = (),
) -> SolutionSheetReading:
    """Recognise ``path`` with the project's template and describe it for review.

    Uses :class:`~omr_scanner.services.recognition_service.RecognitionEngine`
    with its default options - the same engine, registration and the
    template's own calibrated thresholds a batch uses, plus a preview for the
    review dialog. There is no second recognition pipeline for keys.

    Never raises for a bad sheet: a page that will not register comes back as
    a reading whose :attr:`SolutionSheetReading.registered` is ``False``.
    """
    from omr_scanner.services.recognition_service import RecognitionEngine

    result = RecognitionEngine().process(path, template)
    scanned = key_from_scan(result, plan)
    verdict = check_sheet_set(
        template, selected=selected_set, read=scanned.set_code, defined=defined_sets
    )
    try:
        digest = hash_file(path)
    except OSError:  # pragma: no cover - the engine has just read the file
        digest = ""
    _LOGGER.info(
        "Solution sheet read for set %s: registered=%s blank=%d multiple=%d "
        "set_check=%s",
        selected_set,
        scanned.registered,
        len(scanned.blanks),
        len(scanned.multiples),
        verdict.check.value,
    )
    return SolutionSheetReading(
        path=path, result=result, scanned=scanned, verdict=verdict, sha256=digest
    )


__all__ = ["SolutionSheetReading", "read_solution_sheet"]
