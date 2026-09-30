"""Verifying an answer key through the *real* confirmation dialog.

Why this file exists:
    Every other Answer Key test replaces ``QMessageBox.question`` with a
    function returning ``QMessageBox.StandardButton.Yes``. The real dialog, in
    PySide6 6.11, returns the clicked button as a plain ``int`` (``16384``):
    equal to ``StandardButton.Yes``, never identical to it. ``verify_key``
    compared by identity, so an operator's real "Yes" was rejected - the
    dialog closed and the key stayed a draft - while every mocked test passed.

    Here nothing about the dialog is replaced. The Verify button is clicked,
    the modal that opens is found, and its own Yes button is clicked.

Scenario (as reported): sets 10, 11 and 12, a 100-question key saved as Set 10
revision 1, reviewer configured, then Verify -> Yes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from omr_scanner.config import AppConfig
from omr_scanner.domain.scoring import AnswerKeyStatus
from omr_scanner.gui.answer_key.page import AnswerKeyPage
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.services import (
    create_project,
    load_template,
    project_sets,
    save_template,
    scoring_store,
    set_active_template,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = pytest.mark.gui

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPOSITORY_ROOT / "Sample-Project" / "1.Template" / "BUET100q.omrt"
REVIEWER = "Robin"
SETS = ("10", "11", "12")
KEY = ("ABCD" * 25)[:100]


def _click_when_modal(
    qtbot: object, button: QMessageBox.StandardButton, seen: list[str]
) -> Callable[[], None]:
    """A callback that waits for the real modal box, then clicks its button."""

    def attempt() -> None:
        box = QApplication.activeModalWidget()
        if not isinstance(box, QMessageBox):
            QTimer.singleShot(20, attempt)
            return
        seen.append(box.windowTitle())
        target = box.button(button)
        assert target is not None, "the confirmation has no such button"
        qtbot.mouseClick(target, Qt.MouseButton.LeftButton)  # type: ignore[attr-defined]

    return attempt


@pytest.fixture
def window(qtbot, tmp_path) -> Iterator[MainWindow]:
    if not TEMPLATE.is_file():
        pytest.skip("committed 100-question sample template not present")
    session = create_project(tmp_path, "Real Verify")
    for code in SETS:
        project_sets.add_set(session.database, code)
    path = save_template(
        load_template(TEMPLATE), session.project.layout.templates_dir / "BUET100q.omrt"
    )
    set_active_template(session, path)
    root = session.root
    session.close()

    win = MainWindow(AppConfig(reviewer_name=REVIEWER), config_path=tmp_path / "config.json")
    qtbot.addWidget(win)
    assert win.open_project_at(root)
    assert win.show_page("answer_key")
    yield win
    win.close()


def _saved_draft(window: MainWindow) -> AnswerKeyPage:
    page = window._answer_key_page()
    assert page.state.plan.question_count == 100
    assert page.select_set("10")
    page.key_edit.setPlainText(KEY)
    assert page.save_key()
    assert page.set_state("10") == "draft"
    assert page.verify_button.isEnabled()
    assert page.action_hint_label.text() == "Ready to verify."
    return page


def test_clicking_the_real_yes_verifies_the_key(qtbot, window: MainWindow):
    page = _saved_draft(window)
    draft = page.current_revision()
    revisions_before = len(scoring_store.list_keys(window.session.database, set_code="10"))
    assert "0 of 3 verified" in page.readiness_label.text()

    seen: list[str] = []
    QTimer.singleShot(0, _click_when_modal(qtbot, QMessageBox.StandardButton.Yes, seen))
    qtbot.mouseClick(page.verify_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: QApplication.activeModalWidget() is None, timeout=5_000)
    assert seen == ["Verify this answer key?"]

    # Persisted: the same revision, now verified, by the configured reviewer.
    database = window.session.database
    stored = scoring_store.verified_key(database, "10")
    assert stored is not None
    assert stored.key_id == draft.key_id and stored.revision == 1
    assert stored.key.status is AnswerKeyStatus.VERIFIED
    assert stored.verified_by == REVIEWER
    assert stored.verified_at is not None
    assert len(scoring_store.list_keys(database, set_code="10")) == revisions_before

    # Shown, immediately.
    assert page.set_state("10") == "verified"
    assert "1 of 3 verified" in page.readiness_label.text()
    assert "Verified" in page._tiles["10"].text()
    assert "Verified" in page.status_label.text()
    assert "revision 1" in page.revision_combo.currentText()
    assert "Verified" in page.revision_combo.currentText()
    assert f"Verified by {REVIEWER}" in page.provenance_label.text()
    assert not page.verify_button.isEnabled()

    # Results sees it without a reopen.
    assert "10 rev 1" in window._results_page().policy_label.text()

    # And it survives closing and reopening the project.
    root = window.session.root
    window.close_project()
    assert window.open_project_at(root)
    page = window._answer_key_page()
    assert page.select_set("10")
    assert page.set_state("10") == "verified"
    assert "1 of 3 verified" in page.readiness_label.text()


def test_clicking_the_real_no_leaves_the_draft(qtbot, window: MainWindow):
    page = _saved_draft(window)
    seen: list[str] = []
    QTimer.singleShot(0, _click_when_modal(qtbot, QMessageBox.StandardButton.No, seen))
    qtbot.mouseClick(page.verify_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: QApplication.activeModalWidget() is None, timeout=5_000)
    assert seen == ["Verify this answer key?"]
    assert page.set_state("10") == "draft"
    assert scoring_store.verified_key(window.session.database, "10") is None


def test_a_failed_verification_is_reported_and_stays_draft(
    qtbot, window: MainWindow, monkeypatch
):
    page = _saved_draft(window)
    from omr_scanner.services.scoring_store import ScoringError

    def refuse(*_a: object, **_k: object) -> None:
        raise ScoringError("disk full", user_message="The project database could not be written.")

    monkeypatch.setattr(scoring_store, "verify_key", refuse)
    warned: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **_k: warned.append(str(a[2])))
    )
    seen: list[str] = []
    QTimer.singleShot(0, _click_when_modal(qtbot, QMessageBox.StandardButton.Yes, seen))
    qtbot.mouseClick(page.verify_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: QApplication.activeModalWidget() is None, timeout=5_000)
    assert warned == ["The project database could not be written."]
    assert page.set_state("10") == "draft"


def test_success_is_not_announced_unless_it_persisted(qtbot, window: MainWindow, monkeypatch):
    page = _saved_draft(window)
    monkeypatch.setattr(scoring_store, "verify_key", lambda *_a, **_k: page.current_revision())
    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "critical", staticmethod(lambda *a, **_k: shown.append(str(a[2])))
    )
    seen: list[str] = []
    QTimer.singleShot(0, _click_when_modal(qtbot, QMessageBox.StandardButton.Yes, seen))
    qtbot.mouseClick(page.verify_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: QApplication.activeModalWidget() is None, timeout=5_000)
    assert shown and "remains a draft" in shown[0]
    assert page.set_state("10") == "draft"
