"""Accessibility qualification for the Qt interface.

What this does and does not claim:
    These are **automated checks against Qt's accessibility model**. They find
    a control with no accessible name, a control nobody can reach with the
    keyboard, a disabled control that does not say why. They cannot tell
    whether a name is *meaningful*, whether a reading order makes sense to
    somebody using a screen reader, or whether a colour pairing is comfortable.
    Passing every check here is **not WCAG conformance** and nothing in this
    module says it is; the manual review lives in
    ``docs/release/ACCESSIBILITY_CHECKLIST.md``.

Findings, not just failures:
    Most of what this module notices is a *finding* with a severity, written to
    the JSON file named by ``OMRFLOW_VALIDATION_A11Y``. The framework's
    accessibility stage reads that file and reports one row per finding. The
    reason for the indirection is that "this combo box has no accessible name"
    and "nothing in the window can be reached by keyboard" are both
    accessibility problems and only one of them should stop a release, and a
    bare ``assert`` cannot express that difference.

    Severities:

Error:
        A control that a screen-reader or keyboard user cannot operate at all.

Warning:
        Something that degrades the experience - a duplicated name, a control
        whose only label is its tooltip.
    INFO
        Worth knowing, not worth acting on alone.

    A handful of things are still plain assertions, because they are structural
    and their failure means the interface is broken rather than imperfect:
    focus must move, a focused stage must activate, and a disabled stage must
    explain itself.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QComboBox,
    QLineEdit,
    QWidget,
)

from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages import WORKFLOW_PAGES

pytestmark = pytest.mark.gui

STAGE_KEYS = tuple(spec.key for spec in WORKFLOW_PAGES)

INTERACTIVE_TYPES = (QAbstractButton, QLineEdit, QComboBox, QAbstractSpinBox)
"""Widget classes a keyboard or screen-reader user has to be able to operate.

Labels and frames are excluded deliberately: a decorative separator with no
accessible name is correct, and demanding one would train people to add noise.
"""

FINDINGS_ENV = "OMRFLOW_VALIDATION_A11Y"


# ------------------------------------------------------------------ recording


@pytest.fixture(scope="session")
def findings_path() -> Path | None:
    raw = os.environ.get(FINDINGS_ENV)
    if not raw:
        return None
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    return path


@pytest.fixture
def record(findings_path: Path | None) -> Iterator[Callable[..., None]]:
    """Append one accessibility finding.

    Written as JSON Lines, one object per finding, appended immediately. A
    crashed suite then still leaves every finding it had already made, which a
    single document written at the end would not.
    """

    def append(severity: str, category: str, message: str, *, where: str = "") -> None:
        if findings_path is None:
            return
        with findings_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "severity": severity,
                        "category": category,
                        "message": message,
                        "where": where,
                    }
                )
                + "\n"
            )

    yield append


# ------------------------------------------------------------------- helpers


def walk(widget: QWidget) -> Iterator[QWidget]:
    """Every widget in the tree rooted at ``widget``, including itself."""
    yield widget
    yield from widget.findChildren(QWidget)


def visible_interactive(root: QWidget) -> list[QWidget]:
    """Interactive, visible, enabled controls under ``root``.

    Hidden controls are skipped: a page that is not on screen has widgets that
    are legitimately unreachable, and flagging them would bury the real
    findings under dozens of false ones.
    """
    return [
        widget
        for widget in walk(root)
        if isinstance(widget, INTERACTIVE_TYPES)
        and widget.isVisible()
        and widget.isEnabled()
        and not is_qt_internal(widget)
    ]


def is_qt_internal(widget: QWidget) -> bool:
    """Whether Qt created this widget rather than the application.

    Qt names its own scaffolding ``qt_*`` - a toolbar's overflow button, a
    scroll area's viewport, a combo box's line edit. The application cannot
    give those accessible names and should not be asked to; reporting them
    would bury the findings that someone can actually act on.
    """
    return widget.objectName().startswith("qt_")


def describe(widget: QWidget) -> str:
    """Enough to find the widget again: type, object name, and its text."""
    text = ""
    if isinstance(widget, QAbstractButton):
        text = widget.text()
    elif isinstance(widget, QLineEdit):
        text = widget.placeholderText()
    name = widget.objectName() or "(no objectName)"
    return f"{type(widget).__name__}[{name}] {text!r}".strip()


def announced_name(widget: QWidget) -> str:
    """The name a screen reader would announce, from any of Qt's sources."""
    for candidate in (
        widget.accessibleName(),
        widget.text() if isinstance(widget, QAbstractButton) else "",
        widget.placeholderText() if isinstance(widget, QLineEdit) else "",
        widget.toolTip(),
    ):
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


# ------------------------------------------------------------- named controls


class TestAccessibleNames:
    """Every interactive control should announce something."""

    def test_the_main_window_controls_are_named(self, shown_window: MainWindow, record):
        unnamed = [
            describe(widget)
            for widget in visible_interactive(shown_window)
            if not announced_name(widget)
        ]
        for item in unnamed:
            record("ERROR", "missing-accessible-name", item, where="main window")
        # Structural: the shell's own chrome must be usable even if a stage
        # inside it is not yet complete.
        assert not unnamed, "main-window controls with no accessible name:\n  " + "\n  ".join(
            unnamed
        )

    @pytest.mark.parametrize("key", STAGE_KEYS)
    def test_each_stage_names_its_controls(
        self, shown_window: MainWindow, workspace: Path, key: str, qtbot, record
    ):
        """Recorded, not asserted.

        An unnamed control on one stage is a real defect and is reported as an
        ERROR finding, but it does not by itself stop an Alpha release - the
        project's own position is that a full accessibility audit is a Beta
        requirement. The finding is in the report either way.
        """
        shown_window.create_project_at(workspace, "Accessibility Project")
        assert shown_window.show_page(key)
        qtbot.wait(80)
        page = shown_window.stack.currentWidget()
        assert page is not None

        for widget in visible_interactive(page):
            if not announced_name(widget):
                record(
                    "ERROR",
                    "missing-accessible-name",
                    describe(widget),
                    where=f"{key} stage",
                )
            elif not widget.accessibleName().strip():
                # It announces something, but only because Qt fell back to its
                # visible text or tooltip. Workable; not deliberate.
                record(
                    "INFO",
                    "implicit-accessible-name",
                    f"{describe(widget)} announces {announced_name(widget)!r} "
                    "via its text or tooltip rather than an accessibleName",
                    where=f"{key} stage",
                )

    def test_the_workflow_steps_are_named(self, shown_window: MainWindow):
        """Each navigator step must announce which stage it is."""
        for step in shown_window.navigator.steps:
            assert announced_name(step) or step.title, (
                f"workflow step {step.key!r} announces nothing"
            )

    def test_accessible_names_within_the_navigator_are_distinct(
        self, shown_window: MainWindow, workspace: Path, qtbot, record
    ):
        """Two steps announcing the same thing cannot be told apart."""
        shown_window.create_project_at(workspace, "Distinct Names Project")
        qtbot.wait(60)
        labels = [announced_name(step) or step.title for step in shown_window.navigator.steps]
        duplicates = sorted({label for label in labels if labels.count(label) > 1})
        for label in duplicates:
            record("WARNING", "duplicate-accessible-name", label, where="navigator")
        assert not duplicates, f"workflow steps sharing a name: {duplicates}"


# --------------------------------------------------------- keyboard reachable


class TestKeyboardAccess:
    """Everything interactive can be reached and operated without a mouse."""

    def test_interactive_controls_accept_focus(self, shown_window: MainWindow, record):
        unfocusable = [
            describe(widget)
            for widget in visible_interactive(shown_window)
            if widget.focusPolicy() == Qt.FocusPolicy.NoFocus
        ]
        for item in unfocusable:
            record("ERROR", "keyboard-unreachable", item, where="main window")
        assert not unfocusable, (
            "visible controls that the keyboard cannot reach:\n  " + "\n  ".join(unfocusable)
        )

    def test_the_workflow_steps_are_keyboard_reachable(self, shown_window: MainWindow):
        """The steps carry the focus, not the band that holds them.

        The navigator container is deliberately ``NoFocus``: Tab should land on
        a stage, not on the strip around it. What matters is that each step
        itself accepts focus.
        """
        unreachable = [
            step.key
            for step in shown_window.navigator.steps
            if step.focusPolicy() == Qt.FocusPolicy.NoFocus
        ]
        assert not unreachable, f"workflow steps the keyboard cannot reach: {unreachable}"

    def test_tab_moves_focus_through_the_window(self, shown_window: MainWindow, qtbot):
        """Tab must actually move somewhere - trapped focus is unusable."""
        shown_window.navigator.steps[0].setFocus()
        qtbot.wait(40)
        seen: list[str] = []
        for _ in range(12):
            focused = shown_window.focusWidget()
            seen.append(describe(focused) if focused is not None else "(none)")
            qtbot.keyClick(shown_window, Qt.Key.Key_Tab)
            qtbot.wait(20)
        assert len(set(seen)) > 1, f"focus never moved; it stayed on {seen[0]}"

    def test_a_focused_step_can_be_activated_by_keyboard(
        self, shown_window: MainWindow, workspace: Path, qtbot
    ):
        shown_window.create_project_at(workspace, "Keyboard Access Project")
        navigator = shown_window.navigator
        target = next(step for step in navigator.steps if step.key == STAGE_KEYS[1])
        target.setFocus()
        qtbot.wait(30)
        assert target.hasFocus()
        qtbot.keyClick(target, Qt.Key.Key_Space)
        qtbot.wait(80)
        assert navigator.current_key() == STAGE_KEYS[1]


# --------------------------------------------------------------- focus, state


class TestFocusAndState:
    """Focus is visible, and disabled controls say why."""

    def test_the_focused_step_reports_focus(self, shown_window: MainWindow, qtbot):
        step = shown_window.navigator.steps[0]
        step.setFocus()
        qtbot.wait(40)
        assert step.hasFocus(), "the step did not take focus, so no ring can be drawn"

    def test_disabled_stages_explain_themselves(self, shown_window: MainWindow, record):
        """A greyed-out control with no explanation is the commonest complaint."""
        shown_window.close_project()
        silent = [
            step.key
            for step in shown_window.navigator.steps
            if not step.isEnabled() and not (step.toolTip() or "").strip()
        ]
        for key in silent:
            record("WARNING", "unexplained-disabled-control", key, where="navigator")
        assert not silent, f"disabled stages with no stated reason: {silent}"

    def test_enabled_state_follows_whether_a_project_is_open(
        self, shown_window: MainWindow, workspace: Path, qtbot
    ):
        shown_window.close_project()
        qtbot.wait(40)
        without = set(shown_window.navigator.enabled_keys())

        shown_window.create_project_at(workspace, "Enablement Project")
        qtbot.wait(80)
        with_project = set(shown_window.navigator.enabled_keys())

        assert with_project >= without, "opening a project disabled a stage"


# ------------------------------------------------------------------ tooltips


class TestTooltips:
    """A control whose purpose is not obvious from its label."""

    def test_icon_only_controls_are_labelled(self, shown_window: MainWindow, record):
        """An icon with no text must carry a tooltip or an accessible name."""
        silent: list[str] = []
        for widget in visible_interactive(shown_window):
            if not isinstance(widget, QAbstractButton):
                continue
            if widget.text().strip() or widget.icon().isNull():
                continue
            if not (widget.toolTip().strip() or widget.accessibleName().strip()):
                silent.append(describe(widget))
            elif not widget.toolTip().strip():
                record(
                    "INFO",
                    "icon-only-without-tooltip",
                    f"{describe(widget)} has an accessible name but no tooltip",
                    where="main window",
                )
        for item in silent:
            record("ERROR", "unlabelled-icon-control", item, where="main window")
        assert not silent, "icon-only controls with neither tooltip nor name:\n  " + "\n  ".join(
            silent
        )
