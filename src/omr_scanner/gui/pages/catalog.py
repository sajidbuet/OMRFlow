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
        phase: Roadmap phase implementing it; ``0`` marks a stage that already
            has real functionality.
        details: Bullet points describing the intended behaviour, shown on
            placeholder pages so the interface documents the plan honestly.
    """

    key: str
    title: str
    summary: str
    phase: int
    details: tuple[str, ...] = ()

    @property
    def is_implemented(self) -> bool:
        """Whether this stage has real functionality in the current build."""
        return self.phase == 0


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
        details=(
            "Load a reference sheet image and draw recognition zones.",
            "Define four registration markers and the orientation marker.",
            "Configure numeric, alphanumeric, set-code and question fields.",
            "Save and reload versioned .omrt template documents.",
        ),
    ),
    WorkflowPageSpec(
        key="scan",
        title="Scan",
        summary="Import scanned sheets, normalise them and run recognition.",
        phase=5,
        details=(
            "Import a folder of scans without copying large files unnecessarily.",
            "Align each sheet to the template's canonical page (Phase 1).",
            "Recognise field values with confidence scores (Phase 3).",
            "Report per-sheet progress and isolate failures.",
        ),
    ),
    WorkflowPageSpec(
        key="resolve",
        title="Resolve",
        summary="Review and correct sheets that recognition could not decide.",
        phase=6,
        details=(
            "Queue of missing marks, multiple marks and low-confidence values.",
            "Side-by-side original sheet, normalised sheet and zoomed field.",
            "Manual correction that preserves the machine value in an audit trail.",
        ),
    ),
    WorkflowPageSpec(
        key="attendance",
        title="Attendance",
        summary="Import the candidate list and reconcile it against the scripts.",
        phase=7,
        details=(
            "Import candidates and absentees from CSV or Excel.",
            "Detect unknown, duplicate and missing scripts.",
            "Flag absent candidates that nevertheless have a script.",
        ),
    ),
    WorkflowPageSpec(
        key="answer_key",
        title="Answer Key",
        summary="Enter or scan the answer key for each question paper set.",
        phase=8,
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
        details=(
            "Roll-wise workbook including absent candidates.",
            "Merit-wise workbook ordered by result.",
            "User-editable Excel layout and PDF export.",
        ),
    ),
)
"""The workflow stages, in the order the user walks through them."""
