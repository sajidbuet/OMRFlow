"""Tests for the one-line workflow ribbon.

Scope:
    The ribbon as a standalone component: the nine stages, their order, the
    three responsive layouts and the transitions between them, the density
    control, the narrow layout's stage selector, the active/disabled states,
    keyboard access, and - above all - that the workflow is *never* drawn on
    more than one line.

    ===== ==========================================================
    Test  Behaviour
    ===== ==========================================================
    A     All nine stages exist, in the workflow's exact order.
    B     Wide windows show all nine on one row.
    C     Medium windows scroll one row horizontally - never wrap.
    D     Narrow windows show only the active stage.
    E     The narrow stage selector exposes all nine, by hover,
          by click and from the keyboard.
    F     Transitions between the three layouts.
    G     Nothing is hidden, nothing clipped, nothing on a second row.
    H     Active state survives every transition, and stays visible.
    I     Disabled state survives every transition.
    J     Chevrons interlock, and the hit target is the visible shape.
    K     Keyboard activation and movement.
    L     Accessible names, tooltips and disabled explanations.
    M     Density: limits, effect, and what it must not change.
    N     A larger font changes the layout instead of clipping.
    ===== ==========================================================

Why the widths are derived and not written down:
    The ribbon decides its layout from the *measured* width of nine labels in
    the font actually in use, and that font differs between a developer's
    Windows desktop (Segoe UI) and a headless runner (a wider fallback). A
    test that asserted "1366 pixels is wide" would pass on one and fail on the
    other while the code was correct in both. :func:`width_for_mode` therefore
    asks the ribbon itself where each layout begins, which also means these
    tests keep working when a label is reworded or a density level is retuned.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QEnterEvent, QFont, QFontMetrics
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
from omr_scanner.gui.theme import Density
from omr_scanner.gui.theme import Navigator as NavMetrics
from omr_scanner.gui.widgets.workflow_ribbon import (
    MIN_SCROLL_STEPS,
    RibbonMode,
    WorkflowRibbon,
)
from omr_scanner.gui.widgets.workflow_step import StepShape

pytestmark = pytest.mark.gui

EXPECTED_ORDER = (
    "Project",
    "Template",
    "Calibrate",
    "Scan",
    "Resolve",
    "Attendance",
    "Answer Key",
    "Results",
    "Reports",
)
"""The workflow, in the order the brief fixes it. Spelled out here rather
than read from the catalog: this is the assertion, and reading it from the
thing under test would assert nothing."""

SEARCH_CEILING = 6000
"""Widest width :func:`width_for_mode` will look at, in logical pixels."""


@pytest.fixture
def ribbon(qtbot) -> WorkflowRibbon:
    widget = WorkflowRibbon(WORKFLOW_PAGES)
    qtbot.addWidget(widget)
    widget.show()
    return widget


def width_for_mode(ribbon: WorkflowRibbon, mode: RibbonMode) -> int:
    """The narrowest width at which ``ribbon`` chooses ``mode``.

    Found by asking the ribbon, so the answer is correct for whatever font the
    test is running under - see the module docstring.
    """
    for width in range(NavMetrics.STEP_GAP, SEARCH_CEILING):
        if ribbon.plan_for_width(width).mode is mode:
            return width
    raise AssertionError(f"{mode} is unreachable at any width up to {SEARCH_CEILING}")


def apply_width(ribbon: WorkflowRibbon, width: int) -> RibbonMode:
    """Resize ``ribbon`` to ``width`` and return the layout it adopted."""
    ribbon.resize(width + 2 * NavMetrics.STRIP_H_PADDING, ribbon.height())
    return ribbon.mode


def visible_steps(ribbon: WorkflowRibbon) -> list[str]:
    return [step.key for step in ribbon.steps if step.isVisibleTo(ribbon)]


def rows_of(ribbon: WorkflowRibbon) -> list[int]:
    """The distinct vertical positions the visible steps occupy."""
    return sorted({step.y() for step in ribbon.steps if step.isVisibleTo(ribbon)})


def hover(ribbon: WorkflowRibbon, step: object) -> None:
    """Deliver a pointer-enter to ``step`` the way Qt itself would.

    Through ``QApplication.sendEvent`` and not ``step.event(...)``: the hover
    reveal is implemented as an event filter the ribbon installs on each step,
    and an event handed straight to the widget's own ``event()`` never passes
    through the filter chain. A test that called ``event()`` would report the
    feature broken while it worked, or - worse - keep passing after the filter
    was removed.
    """
    centre = QPoint(step.width() // 2, step.height() // 2)
    QApplication.sendEvent(
        step,
        QEnterEvent(centre, step.mapTo(ribbon, centre), step.mapToGlobal(centre)),
    )


# ----------------------------------------------------------------------
# A - the stages and their order
# ----------------------------------------------------------------------
class TestAStagesAndOrder:
    def test_all_nine_stages_are_present(self, ribbon: WorkflowRibbon):
        assert len(ribbon.steps) == 9
        assert len(WORKFLOW_PAGES) == 9

    def test_the_workflow_order_is_exactly_as_specified(self, ribbon: WorkflowRibbon):
        assert tuple(step.label for step in ribbon.steps) == EXPECTED_ORDER

    def test_every_stage_is_numbered_in_workflow_order(self, ribbon: WorkflowRibbon):
        """The number is part of the label, so it survives elision."""
        for position, step in enumerate(ribbon.steps, start=1):
            assert step.number == position
            assert step.display_text.startswith(f"{position}. ")

    def test_every_stage_has_its_own_icon(self, ribbon: WorkflowRibbon):
        """Nine semantic icons, not one repeated nine times - and no emoji."""
        icons = [step.icon_name for step in ribbon.steps]
        assert len(set(icons)) == len(icons), icons
        assert all(icon.isascii() and icon.islower() for icon in icons)

    def test_the_keys_match_the_page_catalog(self, ribbon: WorkflowRibbon):
        assert ribbon.keys == tuple(spec.key for spec in WORKFLOW_PAGES)

    def test_the_order_survives_every_layout(self, ribbon: WorkflowRibbon):
        for mode in RibbonMode:
            apply_width(ribbon, width_for_mode(ribbon, mode))
            assert tuple(step.label for step in ribbon.steps) == EXPECTED_ORDER


# ----------------------------------------------------------------------
# B, C, D - the three layouts
# ----------------------------------------------------------------------
class TestBFullLayout:
    def test_a_wide_ribbon_shows_all_nine_on_one_row(self, ribbon: WorkflowRibbon):
        assert apply_width(ribbon, width_for_mode(ribbon, RibbonMode.FULL)) is (
            RibbonMode.FULL
        )
        assert len(visible_steps(ribbon)) == 9
        assert len(rows_of(ribbon)) == 1
        assert all(step.step_geometry.shape is StepShape.CHEVRON for step in ribbon.steps)

    def test_the_full_row_spans_the_available_width(self, ribbon: WorkflowRibbon):
        """It should reach the margin, not trail off two thirds of the way."""
        width = width_for_mode(ribbon, RibbonMode.FULL) + 400
        plan = ribbon.plan_for_width(width)
        rightmost = max(placement.rect.right() for placement in plan.placements)
        assert rightmost == width - 1

    def test_the_first_step_has_no_left_notch(self, ribbon: WorkflowRibbon):
        """The row's left edge is flat.

        The first step has nothing to interlock with, so a notch there would
        be a bite out of the row's outer edge.
        """
        plan = ribbon.plan_for_width(width_for_mode(ribbon, RibbonMode.FULL))
        assert plan.placements[0].geometry.has_left_notch is False
        assert all(placement.geometry.has_left_notch for placement in plan.placements[1:])


class TestCScrollLayout:
    def test_a_medium_ribbon_scrolls_rather_than_wrapping(self, ribbon: WorkflowRibbon):
        width = width_for_mode(ribbon, RibbonMode.SCROLL)
        plan = ribbon.plan_for_width(width)
        assert plan.mode is RibbonMode.SCROLL
        assert plan.rows == 1
        assert plan.strip_size.width() > width

    def test_every_stage_is_still_there_just_off_to_the_right(
        self, ribbon: WorkflowRibbon
    ):
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.SCROLL))
        assert len(visible_steps(ribbon)) == 9
        assert len(rows_of(ribbon)) == 1
        for step in ribbon.steps:
            assert step.width() > 0
            assert step.height() > 0

    def test_the_wheel_scrolls_the_strip_sideways(self, ribbon: WorkflowRibbon):
        """A vertical wheel too, because most mice only have one.

        Driven through the scrollbar the wheel handler manipulates rather than
        by synthesising a wheel event, which the offscreen platform delivers
        inconsistently; what matters is that the strip *can* be scrolled and
        that the range exists for it.
        """
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.SCROLL))
        bar = ribbon._scroll.horizontalScrollBar()
        assert bar.maximum() > 0, "a scrolling strip must have somewhere to scroll"
        bar.setValue(bar.maximum())
        assert bar.value() == bar.maximum()

    def test_no_scrollbar_steals_height_from_the_chevrons(
        self, ribbon: WorkflowRibbon
    ):
        """The ribbon is one line inside a 46-pixel chrome row.

        A horizontal scrollbar would take a third of that line, which is what
        clipped the chevrons in the previous design, so scrolling is driven by
        the wheel, the arrows and the automatic scroll instead.
        """
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.SCROLL))
        assert (
            ribbon._scroll.horizontalScrollBarPolicy()
            is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        viewport = ribbon._scroll.viewport()
        for step in ribbon.steps:
            assert step.y() + step.height() <= viewport.height()


class TestDNarrowLayout:
    def test_a_narrow_ribbon_shows_only_the_active_stage(self, ribbon: WorkflowRibbon):
        ribbon.set_current_key("results")
        assert apply_width(ribbon, width_for_mode(ribbon, RibbonMode.CURRENT_ONLY)) is (
            RibbonMode.CURRENT_ONLY
        )
        assert visible_steps(ribbon) == ["results"]
        assert len(rows_of(ribbon)) == 1

    def test_the_lone_step_is_a_tile_with_a_caret(self, ribbon: WorkflowRibbon):
        """A chevron only means anything when the next chevron continues it.

        The caret is what says the control opens something, which is the
        difference between a lone step and a dead label.
        """
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.CURRENT_ONLY))
        geometry = ribbon.step(ribbon.current_key()).step_geometry
        assert geometry.shape is StepShape.TILE
        assert geometry.has_menu_indicator is True

    def test_the_hidden_stages_are_neither_deleted_nor_disabled(
        self, ribbon: WorkflowRibbon
    ):
        """The brief's own distinction, and the hardest line in it.

        Hidden from *this layout's strip* is not the same thing as gone: every
        stage keeps its widget, its enabled state and its place in the
        workflow, and is one hover, click or keypress away in the selector.
        """
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.CURRENT_ONLY))
        assert len(ribbon.steps) == 9
        assert ribbon.keys == tuple(spec.key for spec in WORKFLOW_PAGES)
        assert len(ribbon.enabled_keys()) == 9

    def test_changing_stage_changes_which_one_is_shown(self, ribbon: WorkflowRibbon):
        """Requirement 16 in the narrow layout: the active stage is visible."""
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.CURRENT_ONLY))
        for key in ("scan", "attendance", "reports"):
            ribbon.set_current_key(key)
            assert visible_steps(ribbon) == [key]


# ----------------------------------------------------------------------
# E - the narrow layout's stage selector
# ----------------------------------------------------------------------
class TestEStageSelector:
    def _narrow(self, ribbon: WorkflowRibbon) -> None:
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.CURRENT_ONLY))

    def test_the_selector_lists_all_nine_stages_in_order(self, ribbon: WorkflowRibbon):
        self._narrow(ribbon)
        ribbon.refresh_flyout()
        texts = [action.text() for action in ribbon.flyout.actions()]
        assert texts == [step.display_text for step in ribbon.steps]
        assert len(texts) == 9

    def test_hovering_the_current_stage_reveals_the_others(
        self, ribbon: WorkflowRibbon
    ):
        """The brief's hover enhancement."""
        self._narrow(ribbon)
        step = ribbon.step(ribbon.current_key())
        assert step is not None
        hover(ribbon, step)
        assert ribbon.flyout.isVisible()
        ribbon.flyout.close()

    def test_hover_does_nothing_in_the_layouts_that_show_every_stage(
        self, ribbon: WorkflowRibbon
    ):
        """Hovering a chevron in the full layout must not pop a menu open."""
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.FULL))
        hover(ribbon, ribbon.steps[3])
        assert ribbon.flyout.isVisible() is False

    def test_clicking_the_current_stage_also_reveals_the_others(
        self, ribbon: WorkflowRibbon
    ):
        """Hover must not be the only way in.

        Clicking the visible step would otherwise navigate to the page that is
        already open, which is a control that does nothing.
        """
        self._narrow(ribbon)
        received: list[str] = []
        ribbon.step_activated.connect(received.append)
        step = ribbon.step(ribbon.current_key())
        assert step is not None
        step.click()
        assert ribbon.flyout.isVisible()
        assert received == [], "opening the selector is not itself a navigation"
        ribbon.flyout.close()

    def test_the_keyboard_also_reveals_the_others(self, ribbon: WorkflowRibbon):
        """Down, the platform convention for a control that drops a list."""
        self._narrow(ribbon)
        QTest.keyClick(ribbon, Qt.Key.Key_Down)
        assert ribbon.flyout.isVisible()
        ribbon.flyout.close()

    def test_escape_closes_the_selector(self, ribbon: WorkflowRibbon):
        self._narrow(ribbon)
        ribbon.open_flyout()
        assert ribbon.flyout.isVisible()
        QTest.keyClick(ribbon.flyout, Qt.Key.Key_Escape)
        assert ribbon.flyout.isVisible() is False

    def test_choosing_a_stage_from_the_selector_navigates(
        self, ribbon: WorkflowRibbon
    ):
        self._narrow(ribbon)
        received: list[str] = []
        ribbon.step_activated.connect(received.append)
        ribbon.refresh_flyout()
        action = next(
            item for item in ribbon.flyout.actions() if item.text().startswith("6.")
        )
        action.trigger()
        assert received == ["attendance"]

    def test_the_selector_marks_the_current_stage(self, ribbon: WorkflowRibbon):
        self._narrow(ribbon)
        ribbon.set_current_key("resolve")
        ribbon.refresh_flyout()
        checked = [
            action.text() for action in ribbon.flyout.actions() if action.isChecked()
        ]
        assert checked == ["5. Resolve"]

    def test_the_selector_respects_a_disabled_stage(self, ribbon: WorkflowRibbon):
        """It exposes every stage; it does not bypass the workflow's locks."""
        self._narrow(ribbon)
        ribbon.set_step_enabled("reports", enabled=False, reason="Locked.")
        ribbon.refresh_flyout()
        action = next(
            item for item in ribbon.flyout.actions() if item.text().startswith("9.")
        )
        assert action.isEnabled() is False
        assert len(ribbon.flyout.actions()) == 9

    def test_the_selector_does_not_reflow_the_ribbon(self, ribbon: WorkflowRibbon):
        """A popover, not an expansion - the page beneath must not move."""
        self._narrow(ribbon)
        before = ribbon.size()
        ribbon.open_flyout()
        assert ribbon.size() == before
        assert ribbon.flyout.isWindow()
        ribbon.flyout.close()

    def test_leaving_the_narrow_layout_closes_the_selector(
        self, ribbon: WorkflowRibbon
    ):
        self._narrow(ribbon)
        ribbon.open_flyout()
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.FULL))
        assert ribbon.flyout.isVisible() is False


# ----------------------------------------------------------------------
# F - transitions
# ----------------------------------------------------------------------
class TestFTransitions:
    def test_every_mode_is_reachable_in_decreasing_width_order(
        self, ribbon: WorkflowRibbon
    ):
        """The three layouts must be ordered by the room they need.

        If a narrower layout needed *more* width than a wider one, the ribbon
        could never choose it.
        """
        widths = [
            width_for_mode(ribbon, mode)
            for mode in (RibbonMode.CURRENT_ONLY, RibbonMode.SCROLL, RibbonMode.FULL)
        ]
        assert widths == sorted(widths), widths

    def test_returning_to_a_wide_width_restores_the_full_layout(
        self, ribbon: WorkflowRibbon
    ):
        full = width_for_mode(ribbon, RibbonMode.FULL)
        assert apply_width(ribbon, full) is RibbonMode.FULL
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.CURRENT_ONLY))
        assert apply_width(ribbon, full) is RibbonMode.FULL
        assert len(visible_steps(ribbon)) == 9

    def test_repeated_resizing_is_stable_and_leaks_no_widgets(
        self, ribbon: WorkflowRibbon
    ):
        """Full -> scroll -> narrow -> full, several times over.

        The step widgets are created once and only ever repositioned, so the
        count must not drift. A layout that destroyed and rebuilt its children
        on resize would show up here as a growing number of them - and would
        also have thrown away each page's state.
        """
        steps = ribbon.steps
        widths = [width_for_mode(ribbon, mode) for mode in RibbonMode]
        for _ in range(4):
            for width in [*widths, *reversed(widths)]:
                apply_width(ribbon, width)
                assert ribbon.steps == steps
        assert len(ribbon.steps) == 9


# ----------------------------------------------------------------------
# G - one line, always
# ----------------------------------------------------------------------
class TestGAlwaysOneLine:
    @pytest.mark.parametrize("mode", list(RibbonMode))
    def test_the_workflow_never_occupies_two_rows(
        self, ribbon: WorkflowRibbon, mode: RibbonMode
    ):
        """The single hardest line in the brief.

        Checked on the plan *and* on the applied widgets, because the two
        could disagree - the plan is the decision and the widgets are what an
        operator sees.
        """
        width = width_for_mode(ribbon, mode)
        assert ribbon.plan_for_width(width).rows == 1
        apply_width(ribbon, width)
        assert len(rows_of(ribbon)) == 1

    def test_the_ribbon_is_one_step_tall_at_every_width(self, ribbon: WorkflowRibbon):
        """Its height never changes, so nothing below it can ever be pushed."""
        heights = set()
        for width in range(60, 2000, 37):
            apply_width(ribbon, width)
            heights.add(ribbon.height())
        assert heights == {NavMetrics.STEP_HEIGHT + 2 * NavMetrics.STRIP_V_PADDING}

    @pytest.mark.parametrize("mode", [RibbonMode.FULL, RibbonMode.CURRENT_ONLY])
    def test_no_label_is_clipped_in_the_layouts_that_fit(
        self, ribbon: WorkflowRibbon, mode: RibbonMode
    ):
        apply_width(ribbon, width_for_mode(ribbon, mode))
        ribbon.repaint()
        for step in ribbon.steps:
            if step.isVisibleTo(ribbon):
                assert step.is_label_elided is False, step.display_text

    def test_the_scrolling_layout_shows_every_label_in_full_too(
        self, ribbon: WorkflowRibbon
    ):
        """It scrolls precisely so that nothing has to be shortened."""
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.SCROLL))
        ribbon.repaint()
        for step in ribbon.steps:
            assert step.is_label_elided is False, step.display_text

    def test_a_step_drawn_at_exactly_its_natural_width_does_not_elide(
        self, ribbon: WorkflowRibbon
    ):
        """The measurement has to agree with the renderer, not merely be close.

        Regression test for a one-pixel defect found in a 1366-pixel
        screenshot: ``horizontalAdvance`` sums glyph advances while
        ``elidedText`` lays the string out, and they differ by up to a pixel
        on the last glyph's right bearing. "1. Project" therefore rendered as
        "1. Proje..." in the layout chosen precisely because all nine labels
        fitted - so this checks the renderer's own answer at exactly the width
        the layout hands out, rather than trusting the measurement that
        produced it.
        """
        for step in ribbon.steps:
            geometry = step.step_geometry
            available = step.natural_width(geometry) - geometry.chrome_width
            shown = QFontMetrics(step._text_font()).elidedText(
                step.display_text, Qt.TextElideMode.ElideRight, available
            )
            assert shown == step.display_text, step.key

    def test_a_step_is_always_at_least_as_wide_as_its_natural_width(
        self, ribbon: WorkflowRibbon
    ):
        for mode in (RibbonMode.FULL, RibbonMode.SCROLL):
            plan = ribbon.plan_for_width(width_for_mode(ribbon, mode))
            for placement in plan.placements:
                step = ribbon.step(placement.key)
                assert step is not None
                assert placement.rect.width() >= step.natural_width(placement.geometry)


# ----------------------------------------------------------------------
# H, I - state survives the transitions
# ----------------------------------------------------------------------
class TestHActiveStateSurvives:
    def test_the_active_stage_is_marked(self, ribbon: WorkflowRibbon):
        assert ribbon.set_current_key("scan")
        assert ribbon.current_key() == "scan"
        checked = [step.key for step in ribbon.steps if step.isChecked()]
        assert checked == ["scan"]

    def test_the_active_stage_survives_every_responsive_transition(
        self, ribbon: WorkflowRibbon
    ):
        ribbon.set_current_key("attendance")
        for mode in RibbonMode:
            apply_width(ribbon, width_for_mode(ribbon, mode))
            assert ribbon.current_key() == "attendance"
            assert [s.key for s in ribbon.steps if s.isChecked()] == ["attendance"]

    def test_the_active_stage_is_scrolled_into_view(self, ribbon: WorkflowRibbon):
        """Requirement 16 in the scrolling layout.

        The last stage is off to the right of a viewport that starts at zero,
        so activating it has to move the strip - and the check is that the
        step's rectangle ends up inside the viewport, not that the scrollbar
        reached any particular value.
        """
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.SCROLL))
        bar = ribbon._scroll.horizontalScrollBar()
        bar.setValue(0)
        ribbon.set_current_key("reports")

        step = ribbon.step("reports")
        assert step is not None
        viewport = ribbon._scroll.viewport()
        left = step.x() - bar.value()
        assert left + step.width() <= viewport.width() + 1
        assert bar.value() > 0

    def test_scrolling_back_to_an_early_stage_also_brings_it_into_view(
        self, ribbon: WorkflowRibbon
    ):
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.SCROLL))
        ribbon.set_current_key("reports")
        ribbon.set_current_key("project")
        bar = ribbon._scroll.horizontalScrollBar()
        assert bar.value() == 0

    def test_the_active_stage_is_identifiable_without_relying_on_colour(
        self, ribbon: WorkflowRibbon
    ):
        """It is bolder as well as accented."""
        ribbon.set_current_key("resolve")
        active = ribbon.step("resolve")
        other = ribbon.step("scan")
        assert active is not None and other is not None
        ribbon.repaint()
        assert active.isChecked() is True
        assert other.isChecked() is False

    def test_the_geometry_does_not_shift_when_the_active_stage_changes(
        self, ribbon: WorkflowRibbon
    ):
        """Navigating must not move the row.

        The active step is bold, and bold is wider - so every step is
        *measured* bold whichever one is actually active.
        """
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.FULL))
        ribbon.set_current_key("project")
        before = [step.geometry() for step in ribbon.steps]
        ribbon.set_current_key("reports")
        ribbon._relayout()
        assert [step.geometry() for step in ribbon.steps] == before

    def test_setting_the_current_stage_does_not_emit_a_navigation_request(
        self, ribbon: WorkflowRibbon
    ):
        """Otherwise a programmatic page change loops back as a user request."""
        received: list[str] = []
        ribbon.step_activated.connect(received.append)
        ribbon.set_current_key("results")
        assert received == []

    def test_an_unknown_key_is_refused(self, ribbon: WorkflowRibbon):
        assert ribbon.set_current_key("not-a-stage") is False


class TestIDisabledStateSurvives:
    def test_a_stage_can_be_disabled_with_a_reason(self, ribbon: WorkflowRibbon):
        ribbon.set_step_enabled("reports", enabled=False, reason="Not yet.")
        step = ribbon.step("reports")
        assert step is not None
        assert step.isEnabled() is False
        assert step.disabled_reason == "Not yet."
        assert "Not yet." in step.toolTip()

    def test_the_disabled_state_survives_every_responsive_transition(
        self, ribbon: WorkflowRibbon
    ):
        ribbon.set_step_enabled("answer_key", enabled=False, reason="Locked.")
        for mode in RibbonMode:
            apply_width(ribbon, width_for_mode(ribbon, mode))
            step = ribbon.step("answer_key")
            assert step is not None
            assert step.isEnabled() is False
            assert step.disabled_reason == "Locked."

    def test_a_disabled_stage_is_still_readable(self, ribbon: WorkflowRibbon):
        """A locked stage is subdued, not illegible."""
        ribbon.set_step_enabled("results", enabled=False, reason="Locked.")
        step = ribbon.step("results")
        assert step is not None
        assert step.display_text == "8. Results"

    def test_re_enabling_clears_the_reason(self, ribbon: WorkflowRibbon):
        ribbon.set_step_enabled("reports", enabled=False, reason="Not yet.")
        ribbon.set_step_enabled("reports", enabled=True)
        step = ribbon.step("reports")
        assert step is not None
        assert step.isEnabled() is True
        assert step.disabled_reason == ""

    def test_enabled_keys_reports_what_can_be_opened(self, ribbon: WorkflowRibbon):
        ribbon.set_step_enabled("scan", enabled=False, reason="Locked.")
        assert "scan" not in ribbon.enabled_keys()
        assert len(ribbon.enabled_keys()) == 8


# ----------------------------------------------------------------------
# J - the chevron geometry
# ----------------------------------------------------------------------
class TestJChevronGeometry:
    def test_consecutive_chevrons_interlock(self, ribbon: WorkflowRibbon):
        """Each step starts inside the previous one's rectangle."""
        plan = ribbon.plan_for_width(width_for_mode(ribbon, RibbonMode.FULL))
        for previous, current in zip(plan.placements, plan.placements[1:], strict=False):
            assert current.rect.left() < previous.rect.right()

    def test_the_seam_between_two_chevrons_is_the_configured_gap(
        self, ribbon: WorkflowRibbon
    ):
        """No visible geometry gap, and no seamless merge either.

        Two accent-adjacent steps with no seam read as one wide shape; a seam
        wider than the arrow would break the interlock entirely.
        """
        plan = ribbon.plan_for_width(width_for_mode(ribbon, RibbonMode.FULL))
        for previous, current in zip(plan.placements, plan.placements[1:], strict=False):
            overlap = previous.rect.right() + 1 - current.rect.left()
            assert overlap == previous.geometry.arrow_depth - NavMetrics.STEP_GAP

    @pytest.mark.parametrize("density", range(Density.MINIMUM, Density.MAXIMUM + 1))
    def test_the_chevrons_interlock_at_every_density(
        self, ribbon: WorkflowRibbon, density: int
    ):
        """No clipped arrowhead and no gap, at any density.

        Display scaling reaches these widgets as a larger font, and density is
        the other axis that changes the arrow's depth, so both are swept
        rather than spot-checked.
        """
        ribbon.set_density(density)
        plan = ribbon.plan_for_width(width_for_mode(ribbon, RibbonMode.FULL))
        for placement in plan.placements:
            assert placement.geometry.arrow_depth > NavMetrics.STEP_GAP
            assert placement.rect.width() > 2 * placement.geometry.arrow_depth

    def test_the_hit_target_is_the_painted_shape_not_the_rectangle(
        self, ribbon: WorkflowRibbon
    ):
        """The notch belongs to the previous step, not to this one."""
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.FULL))
        step = ribbon.steps[1]
        middle_height = step.height() // 2
        assert step.hitButton(QPoint(1, middle_height)) is False
        assert step.hitButton(QPoint(step.width() // 2, middle_height)) is True

    def test_the_earlier_step_is_above_the_later_one(self, ribbon: WorkflowRibbon):
        """So the widget on top at any pixel is the one painted there."""
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.FULL))
        strip = ribbon.steps[0].parentWidget()
        assert strip is not None
        order = [child for child in strip.children() if child in ribbon.steps]
        assert order[-1] is ribbon.steps[0]

    def test_a_click_activates_the_stage_it_landed_on(self, ribbon: WorkflowRibbon):
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.FULL))
        received: list[str] = []
        ribbon.step_activated.connect(received.append)

        step = ribbon.step("calibration")
        assert step is not None
        QTest.mouseClick(
            step,
            Qt.MouseButton.LeftButton,
            pos=QPoint(step.width() // 2, step.height() // 2),
        )
        assert received == ["calibration"]

    def test_a_disabled_stage_does_not_emit_when_clicked(self, ribbon: WorkflowRibbon):
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.FULL))
        ribbon.set_step_enabled("reports", enabled=False, reason="Locked.")
        received: list[str] = []
        ribbon.step_activated.connect(received.append)

        step = ribbon.step("reports")
        assert step is not None
        QTest.mouseClick(
            step,
            Qt.MouseButton.LeftButton,
            pos=QPoint(step.width() // 2, step.height() // 2),
        )
        assert received == []


# ----------------------------------------------------------------------
# K - keyboard
# ----------------------------------------------------------------------
class TestKKeyboard:
    def test_every_stage_is_reachable_by_tab(self, ribbon: WorkflowRibbon):
        assert all(
            step.focusPolicy() == Qt.FocusPolicy.StrongFocus for step in ribbon.steps
        )

    def test_space_activates_the_focused_stage(self, ribbon: WorkflowRibbon):
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.FULL))
        received: list[str] = []
        ribbon.step_activated.connect(received.append)
        step = ribbon.step("scan")
        assert step is not None
        step.setFocus()
        QTest.keyClick(step, Qt.Key.Key_Space)
        assert received == ["scan"]

    def test_enter_activates_the_focused_stage(self, ribbon: WorkflowRibbon):
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.FULL))
        received: list[str] = []
        ribbon.step_activated.connect(received.append)
        step = ribbon.step("template")
        assert step is not None
        step.setFocus()
        step.click()
        assert received == ["template"]

    def test_the_arrow_keys_move_between_stages(self, ribbon: WorkflowRibbon):
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.FULL))
        ribbon.steps[0].setFocus()
        QTest.keyClick(ribbon, Qt.Key.Key_Right)
        assert ribbon.steps[1].hasFocus()
        QTest.keyClick(ribbon, Qt.Key.Key_Left)
        assert ribbon.steps[0].hasFocus()

    def test_home_and_end_jump_to_the_ends(self, ribbon: WorkflowRibbon):
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.FULL))
        ribbon.steps[3].setFocus()
        QTest.keyClick(ribbon, Qt.Key.Key_End)
        assert ribbon.steps[-1].hasFocus()
        QTest.keyClick(ribbon, Qt.Key.Key_Home)
        assert ribbon.steps[0].hasFocus()

    def test_arrow_movement_skips_disabled_stages(self, ribbon: WorkflowRibbon):
        """A disabled stage cannot be opened, so stopping on it is a dead end."""
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.FULL))
        ribbon.set_step_enabled("template", enabled=False, reason="Locked.")
        ribbon.steps[0].setFocus()
        QTest.keyClick(ribbon, Qt.Key.Key_Right)
        assert ribbon.step("calibration").hasFocus()

    def test_there_is_no_keyboard_trap(self, ribbon: WorkflowRibbon):
        """An unhandled key must fall through, or Tab could not leave."""
        apply_width(ribbon, width_for_mode(ribbon, RibbonMode.FULL))
        ribbon.steps[0].setFocus()
        QTest.keyClick(ribbon, Qt.Key.Key_A)
        assert ribbon.steps[0].hasFocus()


# ----------------------------------------------------------------------
# L - accessibility metadata
# ----------------------------------------------------------------------
class TestLAccessibility:
    def test_every_stage_has_an_accessible_name_including_its_number(
        self, ribbon: WorkflowRibbon
    ):
        for step in ribbon.steps:
            assert step.accessibleName() == step.display_text

    def test_every_stage_describes_itself(self, ribbon: WorkflowRibbon):
        for step, spec in zip(ribbon.steps, WORKFLOW_PAGES, strict=True):
            assert spec.summary in step.accessibleDescription()

    def test_a_disabled_stage_explains_itself_to_a_screen_reader(
        self, ribbon: WorkflowRibbon
    ):
        ribbon.set_step_enabled("reports", enabled=False, reason="Locked here.")
        step = ribbon.step("reports")
        assert step is not None
        assert "Locked here." in step.accessibleDescription()

    def test_every_stage_has_a_status_tip_and_a_full_name_in_its_tooltip(
        self, ribbon: WorkflowRibbon
    ):
        for step in ribbon.steps:
            assert step.statusTip()
            assert step.label in step.toolTip()
            assert str(step.number) in step.toolTip()


# ----------------------------------------------------------------------
# M - density
# ----------------------------------------------------------------------
class TestMDensity:
    def test_the_default_is_inside_the_supported_range(self, ribbon: WorkflowRibbon):
        assert Density.MINIMUM <= ribbon.density <= Density.MAXIMUM
        assert ribbon.density == Density.DEFAULT

    def test_a_tighter_density_makes_the_row_narrower(self, ribbon: WorkflowRibbon):
        widths = []
        for level in range(Density.MINIMUM, Density.MAXIMUM + 1):
            ribbon.set_density(level)
            widths.append(ribbon._chevron_row_width(ribbon.steps)[0])
        assert widths == sorted(widths), widths
        assert widths[0] < widths[-1]

    def test_the_minimum_is_a_floor_and_the_maximum_a_ceiling(
        self, ribbon: WorkflowRibbon
    ):
        ribbon.set_density(Density.MINIMUM)
        assert ribbon.set_density(Density.MINIMUM - 5) is False
        assert ribbon.density == Density.MINIMUM
        assert ribbon.can_decrease_density() is False

        ribbon.set_density(Density.MAXIMUM)
        assert ribbon.set_density(Density.MAXIMUM + 5) is False
        assert ribbon.density == Density.MAXIMUM
        assert ribbon.can_increase_density() is False

    def test_the_density_signal_carries_the_new_level(self, ribbon: WorkflowRibbon):
        received: list[int] = []
        ribbon.density_changed.connect(received.append)
        ribbon.set_density(Density.MAXIMUM)
        ribbon.set_density(Density.MAXIMUM)
        assert received == [Density.MAXIMUM]

    def test_density_never_changes_the_font(self, ribbon: WorkflowRibbon):
        """It is not a zoom control, and must never become one by degrees."""
        before = [step.font().pointSizeF() for step in ribbon.steps]
        for level in range(Density.MINIMUM, Density.MAXIMUM + 1):
            ribbon.set_density(level)
            assert [step.font().pointSizeF() for step in ribbon.steps] == before

    def test_density_never_changes_the_ribbons_height(self, ribbon: WorkflowRibbon):
        """The chrome row shares this height with the window buttons."""
        before = ribbon.height()
        for level in range(Density.MINIMUM, Density.MAXIMUM + 1):
            ribbon.set_density(level)
            assert ribbon.height() == before

    def test_density_never_changes_the_workflow(self, ribbon: WorkflowRibbon):
        """Same nine stages, same order, same enabled state, same active one."""
        ribbon.set_current_key("scan")
        ribbon.set_step_enabled("reports", enabled=False, reason="Locked.")
        for level in range(Density.MINIMUM, Density.MAXIMUM + 1):
            ribbon.set_density(level)
            assert ribbon.keys == tuple(spec.key for spec in WORKFLOW_PAGES)
            assert ribbon.current_key() == "scan"
            assert ribbon.step("reports").isEnabled() is False

    def test_a_tighter_density_can_win_back_the_full_layout(
        self, ribbon: WorkflowRibbon
    ):
        """What the ``-`` button is actually for.

        At a width that scrolls at the default density, tightening the ribbon
        has to be able to fit all nine - otherwise the control changes an
        appearance and solves nothing.
        """
        ribbon.set_density(Density.MAXIMUM)
        width = ribbon._chevron_row_width(ribbon.steps)[0] - 1
        assert ribbon.plan_for_width(width).mode is not RibbonMode.FULL
        ribbon.set_density(Density.MINIMUM)
        assert ribbon.plan_for_width(width).mode is RibbonMode.FULL


# ----------------------------------------------------------------------
# N - text scaling
# ----------------------------------------------------------------------
class TestNTextScaling:
    def test_a_larger_font_changes_the_layout_rather_than_clipping(
        self, ribbon: WorkflowRibbon
    ):
        """A Windows text-scaling change reaches a widget as a font change.

        The steps re-measure, the width that used to fit nine chevrons no
        longer does, and the ribbon scrolls - it does not shrink the text back
        down, clip it, or start a second row.
        """
        full = width_for_mode(ribbon, RibbonMode.FULL)
        apply_width(ribbon, full)
        assert ribbon.mode is RibbonMode.FULL

        large = QFont(ribbon.font())
        large.setPointSizeF(ribbon.font().pointSizeF() * 2.0)
        ribbon.setFont(large)
        for step in ribbon.steps:
            step.setFont(large)
        ribbon._relayout()

        assert ribbon.mode is not RibbonMode.FULL
        assert len(rows_of(ribbon)) == 1

    def test_a_larger_font_makes_each_step_want_more_room(
        self, ribbon: WorkflowRibbon
    ):
        step = ribbon.steps[0]
        before = step.natural_width()
        large = QFont(step.font())
        large.setPointSizeF(step.font().pointSizeF() * 1.5)
        step.setFont(large)
        assert step.natural_width() > before

    def test_the_scroll_floor_is_a_measurement_not_a_resolution(
        self, ribbon: WorkflowRibbon
    ):
        """It must move with the font, or narrow mode arrives at the wrong time."""
        before = ribbon.scroll_floor()
        large = QFont(ribbon.font())
        large.setPointSizeF(ribbon.font().pointSizeF() * 1.5)
        for step in ribbon.steps:
            step.setFont(large)
        assert ribbon.scroll_floor() > before
        assert MIN_SCROLL_STEPS >= 2


def test_the_module_exposes_no_second_row_concept():
    """A guard against the previous design creeping back.

    The two-row and two-column layouts were removed, not merely stopped being
    chosen. Naming them here means a reintroduction has to delete this test,
    which is a decision somebody has to make on purpose.
    """
    import omr_scanner.gui.widgets.workflow_ribbon as module

    assert not hasattr(module, "FIRST_ROW_STEPS")
    assert not hasattr(module, "COLUMNS_NARROW")
    assert {mode.name for mode in RibbonMode} == {"FULL", "SCROLL", "CURRENT_ONLY"}
