"""Reusable presentation widgets for the application shell.

Purpose:
    Hold the shell's parts - the chrome row, the workflow ribbon, the footer -
    and the presentation primitives the pages share, so that none of them
    lives as styling code inside `MainWindow`.

Layout:
    * :mod:`.app_chrome` - the single chrome row: menu, wordmark, density and
      workflow controls, the ribbon, and the window buttons.
    * :mod:`.window_buttons` - the minimise/maximise/close controls that
      replaced the native title bar's.
    * :mod:`.workflow_ribbon`, :mod:`.workflow_step` - the one-line responsive
      chevron ribbon.
    * :mod:`.status_footer` - the bottom status band.
    * :mod:`.page_header` - the shared heading, used by the Project
      dashboard's card and available to any page that genuinely needs a
      heading of its own. It is no longer put above every stage: the ribbon
      already names the current one, and repeating it cost a row of workspace
      on all nine.
    * :mod:`.card` - card, empty state and clickable action row.
    * :mod:`.buttons` - the primary/secondary/destructive button roles.

What does NOT belong here:
    * Anything that knows about projects, scans, templates or results. These
      widgets are handed text and emit signals; the pages and the main window
      supply the meaning.
"""

from __future__ import annotations

from omr_scanner.gui.widgets.app_chrome import AppChrome
from omr_scanner.gui.widgets.buttons import (
    destructive_button,
    primary_button,
    secondary_button,
    set_button_variant,
)
from omr_scanner.gui.widgets.card import ActionRow, Card, EmptyState
from omr_scanner.gui.widgets.collapsible import CollapsibleSection
from omr_scanner.gui.widgets.page_header import PageHeader
from omr_scanner.gui.widgets.status_chips import StatusChip, StatusChipStrip
from omr_scanner.gui.widgets.status_footer import AppStatus, FooterTier, StatusFooter
from omr_scanner.gui.widgets.window_buttons import WindowButton, WindowButtonKind
from omr_scanner.gui.widgets.workflow_ribbon import (
    LayoutPlan,
    RibbonMode,
    WorkflowRibbon,
)
from omr_scanner.gui.widgets.workflow_step import StepShape, WorkflowStep

__all__ = [
    "ActionRow",
    "AppChrome",
    "AppStatus",
    "Card",
    "CollapsibleSection",
    "EmptyState",
    "FooterTier",
    "LayoutPlan",
    "PageHeader",
    "RibbonMode",
    "StatusChip",
    "StatusChipStrip",
    "StatusFooter",
    "StepShape",
    "WindowButton",
    "WindowButtonKind",
    "WorkflowRibbon",
    "WorkflowStep",
    "destructive_button",
    "primary_button",
    "secondary_button",
    "set_button_variant",
]

