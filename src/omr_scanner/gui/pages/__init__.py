"""Workflow pages shown in the main window's stacked view.

Purpose:
    One page per stage of the examination processing workflow.

What does NOT belong here:
    * Services or algorithms. A page renders state and emits intent.
    * Cross-page navigation decisions; the main window owns navigation.

Phase status:
    :class:`omr_scanner.gui.pages.project_page.ProjectPage` (Phase 0) and
    :class:`omr_scanner.gui.template_designer.page.TemplateDesignerPage`
    (Phase 2) show real functionality. Every other stage is a
    :class:`omr_scanner.gui.pages.placeholder_page.PlaceholderPage` that names
    the phase which will implement it. Placeholders must never simulate
    behaviour that does not exist.

Note on ``TemplateDesignerPage``:
    Deliberately *not* re-exported here even though it is a page like the
    others. `template_designer.page` imports
    `omr_scanner.gui.pages.base_page` and `.catalog` directly (submodules, not
    this package's `__init__`), and importing any submodule of a package first
    runs that package's `__init__`; re-exporting `TemplateDesignerPage` from
    here would make this `__init__` import `template_designer.page`, which
    imports back into a `gui.pages` that is still mid-import - a circular
    import. `main_window.py` imports it directly from
    `omr_scanner.gui.template_designer.page` instead.
"""

from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES, WorkflowPageSpec
from omr_scanner.gui.pages.placeholder_page import PlaceholderPage
from omr_scanner.gui.pages.project_page import ProjectPage

__all__ = ["WORKFLOW_PAGES", "PlaceholderPage", "ProjectPage", "WorkflowPageSpec"]
