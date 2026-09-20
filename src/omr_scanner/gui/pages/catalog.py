"""Declarative list of the workflow stages shown in the main window.

Purpose:
    Keep the navigation structure as data rather than as a sequence of widget
    construction calls, so adding a stage - or replacing a placeholder with a
    real page in a later phase - is a one-line change.

What does NOT belong here:
    * Widget code. This module has no Qt import, which also makes the workflow
      structure testable without a display.
    * Any statement that a stage works when it does not: ``phase`` is the phase
      that will implement the stage and is shown to the user.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowPageSpec:
    """Description of one workflow stage.

    Attributes:
        key: Stable identifier, used by the main window to look up a page.
        title: Navigation label.
        summary: One line describing what the stage is for.
        phase: Roadmap phase that introduced this stage's functionality - shown
            to the user on a placeholder page. Distinct from
            :attr:`implemented`: Phase 2's Template stage keeps ``phase=2`` as a
            historical record even after it stops being a placeholder.
        details: Bullet points describing the intended behaviour, shown on
            placeholder pages so the interface documents the plan honestly.
        implemented: Whether this stage has real functionality in the current
            build. Defaults to whether ``phase`` is the foundation phase (``0``);
            a stage implemented in a later phase sets this explicitly once it
            stops being a placeholder.
    """

    key: str
    title: str
    summary: str
    phase: int
    details: tuple[str, ...] = ()
    implemented: bool | None = None

    @property
    def is_implemented(self) -> bool:
        """Whether this stage has real functionality in the current build."""
        return self.implemented if self.implemented is not None else self.phase == 0


WORKFLOW_PAGES: tuple[WorkflowPageSpec, ...] = (
    WorkflowPageSpec(
        key="project",
        title="Project",
        summary="Create or open an examination project and review its details.",
        phase=0,
    ),
    WorkflowPageSpec(
        key="template",
        title="Template",
        summary="Design and calibrate the OMR sheet template.",
        phase=2,
        implemented=True,
        details=(
            "Load a reference sheet image and draw recognition zones.",
            "Define four registration markers and the orientation marker.",
            "Configure numeric, alphanumeric, set-code and question fields.",
            "Save and reload versioned .omrt template documents.",
        ),
    ),
    WorkflowPageSpec(
        key="calibration",
        title="Calibrate",
        summary=(
            "Test the template against representative real scans and tune "
            "recognition thresholds before running a batch."
        ),
        phase=4,
        implemented=True,
        details=(
            "Load one or more representative scans and run the existing "
            "recognition pipeline on them in diagnostic mode.",
            "Inspect registration, detected/expected marker positions, bubble "
            "sampling geometry and per-bubble fill scores.",
            "Adjust recognition thresholds and see recognition update "
            "immediately, without repeating registration.",
            "Get an explicit validation status - passed, passed with "
            "warnings, needs review, or failed - never a confident-looking "
            "result from a template that does not actually match the scan.",
            "Save the working thresholds back to the template once satisfied.",
        ),
    ),
    WorkflowPageSpec(
        key="scan",
        title="Scan",
        summary="Import scanned sheets, normalise them and run recognition.",
        phase=3,
        implemented=True,
        details=(
            "Import individual scans or a whole folder, in natural order.",
            "Align each sheet to the template's canonical page (Phase 1).",
            "Recognise roll number, set code and answers, flagging what is unclear.",
            "Rename recognised sheets by roll number without ever overwriting one.",
            "Export the batch to CSV.",
        ),
    ),
    WorkflowPageSpec(
        key="resolve",
        title="Resolve",
        summary="Review and correct sheets that recognition could not decide.",
        phase=6,
        implemented=True,
        details=(
            "Queue of missing marks, multiple marks and low-confidence values.",
            "Original sheet, normalised sheet and a zoomed view of the field.",
            "Manual correction that preserves the machine value in an audit trail.",
            "Every decision named, reasoned and recorded append-only.",
        ),
    ),
    WorkflowPageSpec(
        key="attendance",
        title="Attendance",
        summary="Import the candidate list and reconcile it against the scripts.",
        phase=7,
        implemented=True,
        details=(
            "Import candidates and absentees from CSV or Excel.",
            "Detect unknown, duplicate and missing scripts.",
            "Flag absent candidates that nevertheless have a script.",
            "Record every decision beside the imported and recognised values, "
            "never over them.",
        ),
    ),
    WorkflowPageSpec(
        key="answer_key",
        title="Answer Key",
        summary="Enter or scan the answer key for each question paper set.",
        phase=8,
        implemented=True,
        details=(
            "Manual entry and recognition from solution sheets.",
            "Independent keys per set, verified before results are calculated.",
        ),
    ),
    WorkflowPageSpec(
        key="results",
        title="Results",
        summary="Configure marking and calculate candidate results.",
        phase=8,
        implemented=True,
        details=(
            "Configure marks for correct, incorrect and blank answers.",
            "Enable or disable negative marking.",
            "Calculate marks, ranks and processing status per candidate.",
        ),
    ),
    WorkflowPageSpec(
        key="reports",
        title="Reports",
        summary="Export roll-wise and merit-wise reports.",
        phase=9,
        implemented=True,
        details=(
            "Associate an independent result template with each set.",
            "Roll-wise workbook including absent candidates, with Excel "
            "rank formulas.",
            "Merit-wise workbook ordered by result.",
            "Summary, Answer Key and Processing Log sheets.",
            "User-editable report header, logo, fonts and page setup.",
            "PDF export where LibreOffice is available.",
        ),
    ),
)
"""The workflow stages, in the order the user walks through them."""
