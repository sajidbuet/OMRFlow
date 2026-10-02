"""The duplicate-ID grouping is an office choice in Project Configuration (ACCEPTANCE C4).

Off by default (the identifier alone, unchanged); switching it on needs a named
operator, is stored per project and audited; without a name it is refused and
the checkbox goes back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from PySide6.QtWidgets import QMessageBox

from omr_scanner.gui.project_config_dialog import ProjectConfigDialog
from omr_scanner.services import review_store
from omr_scanner.services.conflict_policy import DuplicateGrouping

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.services import ProjectSession

pytestmark = pytest.mark.gui


def test_the_grouping_is_off_by_default_and_needs_a_named_operator(
    qtbot, project_session: ProjectSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *args, **_k: warnings.append(str(args[1]))
    )
    nameless = ProjectConfigDialog(project_session)
    qtbot.addWidget(nameless)
    assert nameless.duplicate_by_set_checkbox.isChecked() is False
    nameless.duplicate_by_set_checkbox.setChecked(True)
    assert warnings == ["Duplicate check not changed"]
    assert nameless.duplicate_by_set_checkbox.isChecked() is False
    assert review_store.duplicate_grouping(project_session.database) is DuplicateGrouping.IDENTIFIER

    named = ProjectConfigDialog(project_session, operator="Office Head")
    qtbot.addWidget(named)
    named.duplicate_by_set_checkbox.setChecked(True)
    assert review_store.duplicate_grouping(project_session.database) is (
        DuplicateGrouping.SET_AND_IDENTIFIER
    )
    reopened = ProjectConfigDialog(project_session, operator="Office Head")
    qtbot.addWidget(reopened)
    assert reopened.duplicate_by_set_checkbox.isChecked() is True
