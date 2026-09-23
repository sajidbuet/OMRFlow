"""Page for a workflow stage that a later phase will implement.

Purpose:
    State plainly that a stage is not implemented yet, and describe what it will
    do, so the interface documents the roadmap instead of pretending to work.

What does NOT belong here:
    * Disabled buttons or mock controls that imply working functionality.
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QWidget

from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.gui.pages.catalog import WorkflowPageSpec


class PlaceholderPage(WorkflowPage):
    """Announces the phase that will implement a workflow stage.

    Args:
        spec: Stage description, including the implementing phase.
        parent: Optional Qt parent.
    """

    def __init__(self, spec: WorkflowPageSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent)

        # The stage's own sentence, in the body rather than in a heading. No
        # page carries a heading naming its stage any more - the workflow
        # ribbon does that - but a stage that does not exist yet still has to
        # say what it will be for, and that sentence used to live in the
        # heading's summary row.
        self.summary_widget = self.add_note(spec.summary)

        notice = QLabel(f"Not implemented yet - planned for development phase {spec.phase}.")
        notice.setObjectName("phaseNotice")
        notice_font = notice.font()
        notice_font.setBold(True)
        notice.setFont(notice_font)
        notice.setWordWrap(True)
        self.body.addWidget(notice)

        if spec.details:
            self.add_note("Planned functionality:")
            for detail in spec.details:
                bullet = QLabel(f"•  {detail}")
                bullet.setWordWrap(True)
                bullet.setIndent(12)
                self.body.addWidget(bullet)

        self.add_note(
            "See development/ROADMAP.md for the deliverables and exit criteria of "
            f"phase {spec.phase}."
        )
