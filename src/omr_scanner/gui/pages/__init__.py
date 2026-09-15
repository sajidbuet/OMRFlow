"""Workflow pages shown in the main window's stacked view.

Purpose:
    One page per stage of the examination processing workflow.

What does NOT belong here:
    * Services or algorithms. A page renders state and emits intent.
    * Cross-page navigation decisions; the main window owns navigation.

Phase status:
    Only :class:`omr_scanner.gui.pages.project_page.ProjectPage` shows real
    functionality. Every other stage is a
    :class:`omr_scanner.gui.pages.placeholder_page.PlaceholderPage` that names
    the phase which will implement it. Placeholders must never simulate
    behaviour that does not exist.
"""

from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES, WorkflowPageSpec
from omr_scanner.gui.pages.placeholder_page import PlaceholderPage
from omr_scanner.gui.pages.project_page import ProjectPage

__all__ = ["WORKFLOW_PAGES", "PlaceholderPage", "ProjectPage", "WorkflowPageSpec"]
