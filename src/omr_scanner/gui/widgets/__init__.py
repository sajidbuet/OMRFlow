"""Reusable presentation widgets for the application shell.

Purpose:
    Hold the shell's parts - header, workflow navigator, footer - and the
    presentation primitives the pages share, so that none of them lives as
    styling code inside `MainWindow`.

Layout:
    * :mod:`.app_header` - the compact branded header and its menu button.
    * :mod:`.workflow_navigator`, :mod:`.workflow_step` - the responsive
      chevron navigator.
    * :mod:`.status_footer` - the bottom status band.
    * :mod:`.page_header` - the heading every stage shares.
    * :mod:`.card` - card, empty state and clickable action row.
    * :mod:`.buttons` - the primary/secondary/destructive button roles.

What does NOT belong here:
    * Anything that knows about projects, scans, templates or results. These
      widgets are handed text and emit signals; the pages and the main window
      supply the meaning.
"""

from __future__ import annotations

from omr_scanner.gui.widgets.app_header import AppHeader
from omr_scanner.gui.widgets.buttons import (
    destructive_button,
    primary_button,
    secondary_button,
    set_button_variant,
)
from omr_scanner.gui.widgets.card import ActionRow, Card, EmptyState
from omr_scanner.gui.widgets.page_header import HeroTagline, PageHeader
from omr_scanner.gui.widgets.status_footer import AppStatus, FooterTier, StatusFooter
from omr_scanner.gui.widgets.workflow_navigator import (
    LayoutPlan,
    NavigatorMode,
    WorkflowNavigator,
)
from omr_scanner.gui.widgets.workflow_step import StepShape, StepSize, WorkflowStep

__all__ = [
    "ActionRow",
    "AppHeader",
    "AppStatus",
    "Card",
    "EmptyState",
    "FooterTier",
    "HeroTagline",
    "LayoutPlan",
    "NavigatorMode",
    "PageHeader",
    "StatusFooter",
    "StepShape",
    "StepSize",
    "WorkflowNavigator",
    "WorkflowStep",
    "destructive_button",
    "primary_button",
    "secondary_button",
    "set_button_variant",
]
