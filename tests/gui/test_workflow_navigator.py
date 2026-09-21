"""Tests for the responsive horizontal workflow navigator.

Scope:
    The navigator as a standalone component: the nine stages, their order,
    the four responsive layouts and the transitions between them, the
    active/disabled states, keyboard access, and that nothing is ever hidden
    or clipped to make a layout fit.

    ===== ==========================================================
    Test  Behaviour
    ===== ==========================================================
    A     All nine stages exist, in the workflow's exact order.
    B     Wide windows use one chevron row.
    C     Medium windows use two rows, split 5 and 4.
    D     Narrow windows use a readable two-column grid.
    E     Extremely narrow windows scroll; nothing is hidden.
    F     Returning to a wide width restores the wide layout.
    G     No stage is ever hidden, and no label ever clipped in the
          chevron and tile layouts.
    H     Active state survives every responsive transition.
    I     Disabled state survives every responsive transition.
    J     Chevrons interlock, and the hit target is the visible shape.
    K     Keyboard activation and arrow-key movement.
    L     Accessible names, tooltips and disabled explanations.
    M     A larger font changes the layout instead of clipping.
    ===== ==========================================================

Why the widths are derived and not written down:
    The navigator decides its layout from the *measured* width of nine labels
    in the font actually in use, and that font differs between a developer's
    Windows desktop (Segoe UI) and a headless runner (a wider fallback). A
    test that asserted "1366 pixels is wide" would pass on one and fail on the
    other while the code was correct in both. :func:`width_for_mode` therefore
    asks the navigator itself where each layout begins, which also means these
    tests keep working when a label is reworded.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest

from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
from omr_scanner.gui.theme import Navigator as NavMetrics
from omr_scanner.gui.widgets.workflow_navigator import (
    COLUMNS_NARROW,
    FIRST_ROW_STEPS,
    NavigatorMode,
    WorkflowNavigator,
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
"""Widest width :func:`width_for_mode` will look at, in logical pixels.

Generous enough to cover a very large font on a very wide display; a mode
that has not appeared by here does not exist.
"""


@pytest.fixture
def navigator(qtbot) -> WorkflowNavigator:
    widget = WorkflowNavigator(WORKFLOW_PAGES)
    qtbot.addWidget(widget)
    widget.show()
    return widget


def width_for_mode(navigator: WorkflowNavigator, mode: NavigatorMode) -> int:
    """The narrowest width at which ``navigator`` chooses ``mode``.

    Found by asking the navigator, so the answer is correct for whatever font
    the test is running under - see the module docstring.
    """
    for width in range(NavMetrics.STEP_GAP, SEARCH_CEILING):
        if navigator.plan_for_width(width).mode is mode:
            return width
    raise AssertionError(f"{mode} is unreachable at any width up to {SEARCH_CEILING}")


def apply_width(navigator: WorkflowNavigator, width: int) -> NavigatorMode:
    """Resize ``navigator`` to ``width`` and return the layout it adopted."""
    navigator.resize(width + 2 * NavMetrics.BAND_H_PADDING, navigator.height())
    return navigator.mode


def rows_of(navigator: WorkflowNavigator) -> list[int]:
    """The distinct vertical positions the steps occupy, top to bottom."""
    return sorted({step.y() for step in navigator.steps})


# ----------------------------------------------------------------------
# A - the stages and their order
# ----------------------------------------------------------------------
class TestAStagesAndOrder:
    def test_all_nine_stages_are_present(self, navigator: WorkflowNavigator):
        assert len(navigator.steps) == 9
        assert len(WORKFLOW_PAGES) == 9

    def test_the_workflow_order_is_exactly_as_specified(
        self, navigator: WorkflowNavigator
    ):
        assert tuple(step.label for step in navigator.steps) == EXPECTED_ORDER

    def test_every_stage_is_numbered_in_workflow_order(
        self, navigator: WorkflowNavigator
    ):
        """The number is part of the label, so it survives elision."""
        for position, step in enumerate(navigator.steps, start=1):
            assert step.number == position
            assert step.display_text.startswith(f"{position}. ")

    def test_every_stage_has_its_own_icon(self, navigator: WorkflowNavigator):
        """Nine semantic icons, not one repeated nine times."""
        icons = [spec.icon for spec in WORKFLOW_PAGES]
        assert len(set(icons)) == len(icons), icons

    def test_the_keys_match_the_page_catalog(self, navigator: WorkflowNavigator):
        assert navigator.keys == tuple(spec.key for spec in WORKFLOW_PAGES)


# ----------------------------------------------------------------------
# B, C, D, E - the four layouts
# ----------------------------------------------------------------------
class TestBWideLayout:
    def test_a_wide_navigator_uses_one_chevron_row(
        self, navigator: WorkflowNavigator
    ):
        assert apply_width(navigator, width_for_mode(navigator, NavigatorMode.WIDE)) is (
            NavigatorMode.WIDE
        )
        assert len(rows_of(navigator)) == 1
        assert all(
            step.step_geometry.shape is StepShape.CHEVRON for step in navigator.steps
        )

    def test_the_wide_row_spans_the_available_width(
        self, navigator: WorkflowNavigator
    ):
        """It should reach the margin, not trail off two thirds of the way."""
        width = width_for_mode(navigator, NavigatorMode.WIDE) + 400
        plan = navigator.plan_for_width(width)
        rightmost = max(placement.rect.right() for placement in plan.placements)
        assert rightmost == width - 1

    def test_the_first_step_has_no_left_notch(self, navigator: WorkflowNavigator):
        """The row's left edge is flat.

        The first step has nothing to interlock with, so a notch there would
        be a bite out of the row's outer edge.
        """
        plan = navigator.plan_for_width(width_for_mode(navigator, NavigatorMode.WIDE))
        assert plan.placements[0].geometry.has_left_notch is False
        assert all(
            placement.geometry.has_left_notch for placement in plan.placements[1:]
        )


class TestCMediumLayout:
    def test_a_medium_navigator_uses_two_rows(self, navigator: WorkflowNavigator):
        assert apply_width(
            navigator, width_for_mode(navigator, NavigatorMode.MEDIUM)
        ) is NavigatorMode.MEDIUM
        assert len(rows_of(navigator)) == 2

    def test_the_split_is_five_then_four_in_workflow_order(
        self, navigator: WorkflowNavigator
    ):
        plan = navigator.plan_for_width(
            width_for_mode(navigator, NavigatorMode.MEDIUM)
        )
        tops = sorted({placement.rect.top() for placement in plan.placements})
        first = [p.key for p in plan.placements if p.rect.top() == tops[0]]
        second = [p.key for p in plan.placements if p.rect.top() == tops[1]]

        assert len(first) == FIRST_ROW_STEPS
        assert first == [spec.key for spec in WORKFLOW_PAGES[:FIRST_ROW_STEPS]]
        assert second == [spec.key for spec in WORKFLOW_PAGES[FIRST_ROW_STEPS:]]

    def test_each_row_starts_a_new_chevron_run(self, navigator: WorkflowNavigator):
        """The step that begins the second row has no notch either.

        It has no predecessor on its own row, so a notch would point at
        nothing.
        """
        plan = navigator.plan_for_width(
            width_for_mode(navigator, NavigatorMode.MEDIUM)
        )
        tops = sorted({p.rect.top() for p in plan.placements})
        for top in tops:
            row = [p for p in plan.placements if p.rect.top() == top]
            assert row[0].geometry.has_left_notch is False
            assert all(p.geometry.has_left_notch for p in row[1:])

    def test_neither_row_overflows(self, navigator: WorkflowNavigator):
        width = width_for_mode(navigator, NavigatorMode.MEDIUM)
        plan = navigator.plan_for_width(width)
        assert max(p.rect.right() for p in plan.placements) <= width - 1
        assert plan.strip_size.width() <= width


class TestDNarrowLayout:
    def test_a_narrow_navigator_uses_two_columns(self, navigator: WorkflowNavigator):
        assert apply_width(
            navigator, width_for_mode(navigator, NavigatorMode.NARROW)
        ) is NavigatorMode.NARROW
        plan = navigator.plan_for_width(
            width_for_mode(navigator, NavigatorMode.NARROW)
        )
        columns = {p.rect.left() for p in plan.placements}
        assert len(columns) == COLUMNS_NARROW

    def test_the_reading_order_stays_row_major_and_in_workflow_order(
        self, navigator: WorkflowNavigator
    ):
        """1-2 / 3-4 / 5-6 / 7-8 / 9.

        Deliberately *not* balanced into columns: reordering the stages
        vertically to even the columns up would put stage 5 next to stage 1,
        which is a different workflow.
        """
        plan = navigator.plan_for_width(
            width_for_mode(navigator, NavigatorMode.NARROW)
        )
        ordered = sorted(plan.placements, key=lambda p: (p.rect.top(), p.rect.left()))
        assert [p.key for p in ordered] == [spec.key for spec in WORKFLOW_PAGES]

    def test_the_narrow_layout_uses_tiles_rather_than_chevrons(
        self, navigator: WorkflowNavigator
    ):
        """Tiles, because an arrow would point at the column break.

        A chevron only means anything when the next chevron continues it,
        which in a two-column grid it does not.
        """
        plan = navigator.plan_for_width(
            width_for_mode(navigator, NavigatorMode.NARROW)
        )
        assert all(p.geometry.shape is StepShape.TILE for p in plan.placements)
        assert all(p.geometry.arrow_depth == 0 for p in plan.placements)

    def test_numbers_icons_and_labels_all_survive(self, navigator: WorkflowNavigator):
        apply_width(navigator, width_for_mode(navigator, NavigatorMode.NARROW))
        for step in navigator.steps:
            assert step.display_text.startswith(f"{step.number}. ")
            assert step.step_geometry.icon_extent > 0
            assert step.is_label_elided is False


class TestECompactLayout:
    def test_an_extremely_narrow_navigator_scrolls(
        self, navigator: WorkflowNavigator
    ):
        width = width_for_mode(navigator, NavigatorMode.COMPACT)
        plan = navigator.plan_for_width(width)
        assert plan.mode is NavigatorMode.COMPACT
        assert plan.strip_size.width() > width

    def test_no_stage_is_hidden_even_at_the_narrowest(
        self, navigator: WorkflowNavigator
    ):
        """The brief's hardest line: never silently hide a workflow stage.

        Every one of the nine is placed, visible, and has a non-zero size -
        off to the right of the viewport, reachable by scrolling, which is a
        different thing from absent.
        """
        apply_width(navigator, width_for_mode(navigator, NavigatorMode.COMPACT))
        assert len(navigator.plan_for_width(120).placements) == 9
        for step in navigator.steps:
            assert step.isVisibleTo(navigator)
            assert step.width() > 0
            assert step.height() > 0

    def test_a_narrower_width_never_reduces_the_font(
        self, navigator: WorkflowNavigator
    ):
        """Shrinking text to fit is what the layouts exist to avoid."""
        wide = width_for_mode(navigator, NavigatorMode.WIDE)
        apply_width(navigator, wide)
        before = [step.font().pointSizeF() for step in navigator.steps]

        apply_width(navigator, width_for_mode(navigator, NavigatorMode.COMPACT))
        after = [step.font().pointSizeF() for step in navigator.steps]
        assert after == before


# ----------------------------------------------------------------------
# F - transitions
# ----------------------------------------------------------------------
class TestFTransitions:
    def test_every_mode_is_reachable_in_decreasing_width_order(
        self, navigator: WorkflowNavigator
    ):
        """The four layouts must be ordered by the room they need.

        If a narrower layout needed *more* width than a wider one, the
        navigator could never choose it.
        """
        widths = [
            width_for_mode(navigator, mode)
            for mode in (
                NavigatorMode.COMPACT,
                NavigatorMode.NARROW,
                NavigatorMode.MEDIUM,
                NavigatorMode.WIDE,
            )
        ]
        assert widths == sorted(widths), widths

    def test_returning_to_a_wide_width_restores_the_wide_layout(
        self, navigator: WorkflowNavigator
    ):
        wide = width_for_mode(navigator, NavigatorMode.WIDE)
        assert apply_width(navigator, wide) is NavigatorMode.WIDE
        apply_width(navigator, width_for_mode(navigator, NavigatorMode.NARROW))
        assert apply_width(navigator, wide) is NavigatorMode.WIDE
        assert len(rows_of(navigator)) == 1

    def test_repeated_resizing_is_stable_and_leaks_no_widgets(
        self, navigator: WorkflowNavigator
    ):
        """Wide -> medium -> narrow -> wide, several times over.

        The step widgets are created once and only ever repositioned, so the
        count must not drift. A layout that destroyed and rebuilt its children
        on resize would show up here as a growing number of them - and would
        also have thrown away each page's state, which is the defect the brief
        calls out.
        """
        steps = navigator.steps
        widths = [
            width_for_mode(navigator, mode)
            for mode in (
                NavigatorMode.WIDE,
                NavigatorMode.MEDIUM,
                NavigatorMode.NARROW,
                NavigatorMode.COMPACT,
            )
        ]
        for _ in range(4):
            for width in [*widths, *reversed(widths)]:
                apply_width(navigator, width)
                assert navigator.steps == steps
        assert len(navigator.steps) == 9

    @pytest.mark.parametrize(
        "mode",
        [NavigatorMode.WIDE, NavigatorMode.MEDIUM, NavigatorMode.NARROW],
    )
    def test_only_the_compact_layout_can_ever_scroll(
        self, navigator: WorkflowNavigator, mode: NavigatorMode
    ):
        """A scrollbar in a fitting layout clips the chevrons.

        Regression test for a defect found by looking at a real screenshot: a
        resize that passed through a narrower width left the horizontal
        scrollbar showing in the wide layout. The band's height is computed
        without a scrollbar there, so the scrollbar took that height from the
        viewport and cut the bottom off every chevron. Nothing in the geometry
        assertions caught it, because the geometry was right - it was the
        *scroll area* that was wrong.
        """
        from PySide6.QtCore import Qt as QtNs

        apply_width(navigator, width_for_mode(navigator, NavigatorMode.COMPACT))
        apply_width(navigator, width_for_mode(navigator, mode))

        assert navigator.mode is mode
        assert (
            navigator._scroll.horizontalScrollBarPolicy()
            is QtNs.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        assert navigator._scroll.horizontalScrollBar().isVisibleTo(navigator) is False

    def test_the_compact_layout_may_scroll(self, navigator: WorkflowNavigator):
        from PySide6.QtCore import Qt as QtNs

        apply_width(navigator, width_for_mode(navigator, NavigatorMode.COMPACT))
        assert (
            navigator._scroll.horizontalScrollBarPolicy()
            is QtNs.ScrollBarPolicy.ScrollBarAsNeeded
        )

    def test_the_steps_are_never_vertically_clipped(
        self, navigator: WorkflowNavigator
    ):
        """Every step's full height fits inside the band it lives in."""
        for mode in NavigatorMode:
            apply_width(navigator, width_for_mode(navigator, mode))
            viewport = navigator._scroll.viewport()
            for step in navigator.steps:
                assert step.y() + step.height() <= viewport.height(), (
                    f"{mode.value}: {step.key} extends past the viewport"
                )

    def test_the_band_height_follows_the_layout(self, navigator: WorkflowNavigator):
        """Two rows need roughly twice one row; five need more again."""
        apply_width(navigator, width_for_mode(navigator, NavigatorMode.WIDE))
        one_row = navigator.height()
        apply_width(navigator, width_for_mode(navigator, NavigatorMode.MEDIUM))
        two_rows = navigator.height()
        apply_width(navigator, width_for_mode(navigator, NavigatorMode.NARROW))
        five_rows = navigator.height()

        assert two_rows > one_row
        assert five_rows > two_rows


# ----------------------------------------------------------------------
# G - nothing hidden, nothing clipped
# ----------------------------------------------------------------------
class TestGNothingHiddenOrClipped:
    @pytest.mark.parametrize(
        "mode",
        [NavigatorMode.WIDE, NavigatorMode.MEDIUM, NavigatorMode.NARROW],
    )
    def test_no_label_is_clipped_in_the_full_label_layouts(
        self, navigator: WorkflowNavigator, mode: NavigatorMode
    ):
        """These three layouts are chosen precisely because labels fit.

        Only the scrolling last resort is allowed to elide, and there the
        tooltip carries the full name.
        """
        apply_width(navigator, width_for_mode(navigator, mode))
        navigator.repaint()
        for step in navigator.steps:
            assert step.is_label_elided is False, step.display_text

    def test_a_step_is_always_at_least_as_wide_as_its_natural_width(
        self, navigator: WorkflowNavigator
    ):
        for mode in (NavigatorMode.WIDE, NavigatorMode.MEDIUM, NavigatorMode.NARROW):
            plan = navigator.plan_for_width(width_for_mode(navigator, mode))
            for placement in plan.placements:
                step = navigator.step(placement.key)
                assert step is not None
                assert placement.rect.width() >= step.natural_width(placement.geometry)

    def test_an_elided_label_still_has_its_full_name_in_the_tooltip(
        self, navigator: WorkflowNavigator
    ):
        for step in navigator.steps:
            assert step.label in step.toolTip()
            assert str(step.number) in step.toolTip()


# ----------------------------------------------------------------------
# H, I - state survives the transitions
# ----------------------------------------------------------------------
class TestHActiveStateSurvives:
    def test_the_active_stage_is_marked(self, navigator: WorkflowNavigator):
        assert navigator.set_current_key("scan")
        assert navigator.current_key() == "scan"
        checked = [step.key for step in navigator.steps if step.isChecked()]
        assert checked == ["scan"]

    def test_the_active_stage_survives_every_responsive_transition(
        self, navigator: WorkflowNavigator
    ):
        navigator.set_current_key("attendance")
        for mode in NavigatorMode:
            apply_width(navigator, width_for_mode(navigator, mode))
            assert navigator.current_key() == "attendance"
            assert [s.key for s in navigator.steps if s.isChecked()] == ["attendance"]

    def test_the_active_stage_is_identifiable_without_relying_on_colour(
        self, navigator: WorkflowNavigator
    ):
        """It is bolder as well as accented.

        Weight is a non-colour signal, so "which stage am I on" survives a
        monochrome display and colour blindness.
        """
        navigator.set_current_key("resolve")
        active = navigator.step("resolve")
        other = navigator.step("scan")
        assert active is not None and other is not None
        navigator.repaint()
        assert active.isChecked() is True
        assert other.isChecked() is False

    def test_the_geometry_does_not_shift_when_the_active_stage_changes(
        self, navigator: WorkflowNavigator
    ):
        """Navigating must not move the row.

        The active step is bold, and bold is wider - so every step is
        *measured* bold whichever one is actually active.
        """
        apply_width(navigator, width_for_mode(navigator, NavigatorMode.WIDE))
        navigator.set_current_key("project")
        before = [step.geometry() for step in navigator.steps]
        navigator.set_current_key("reports")
        navigator._relayout()
        assert [step.geometry() for step in navigator.steps] == before

    def test_setting_the_current_stage_does_not_emit_a_navigation_request(
        self, navigator: WorkflowNavigator
    ):
        """Otherwise a programmatic page change loops back as a user request."""
        received: list[str] = []
        navigator.step_activated.connect(received.append)
        navigator.set_current_key("results")
        assert received == []

    def test_an_unknown_key_is_refused(self, navigator: WorkflowNavigator):
        assert navigator.set_current_key("not-a-stage") is False


class TestIDisabledStateSurvives:
    def test_a_stage_can_be_disabled_with_a_reason(
        self, navigator: WorkflowNavigator
    ):
        navigator.set_step_enabled("reports", enabled=False, reason="Not yet.")
        step = navigator.step("reports")
        assert step is not None
        assert step.isEnabled() is False
        assert step.disabled_reason == "Not yet."
        assert "Not yet." in step.toolTip()

    def test_the_disabled_state_survives_every_responsive_transition(
        self, navigator: WorkflowNavigator
    ):
        navigator.set_step_enabled("answer_key", enabled=False, reason="Locked.")
        for mode in NavigatorMode:
            apply_width(navigator, width_for_mode(navigator, mode))
            step = navigator.step("answer_key")
            assert step is not None
            assert step.isEnabled() is False
            assert step.disabled_reason == "Locked."

    def test_a_disabled_stage_is_still_readable(self, navigator: WorkflowNavigator):
        """A locked stage is subdued, not illegible.

        An operator should be able to read the whole workflow ahead of
        themselves.
        """
        navigator.set_step_enabled("results", enabled=False, reason="Locked.")
        step = navigator.step("results")
        assert step is not None
        assert step.display_text == "8. Results"
        assert step.isVisibleTo(navigator)

    def test_re_enabling_clears_the_reason(self, navigator: WorkflowNavigator):
        navigator.set_step_enabled("reports", enabled=False, reason="Not yet.")
        navigator.set_step_enabled("reports", enabled=True)
        step = navigator.step("reports")
        assert step is not None
        assert step.isEnabled() is True
        assert step.disabled_reason == ""

    def test_enabled_keys_reports_what_can_be_opened(
        self, navigator: WorkflowNavigator
    ):
        navigator.set_step_enabled("scan", enabled=False, reason="Locked.")
        assert "scan" not in navigator.enabled_keys()
        assert len(navigator.enabled_keys()) == 8


# ----------------------------------------------------------------------
# J - the chevron geometry
# ----------------------------------------------------------------------
class TestJChevronGeometry:
    def test_consecutive_chevrons_interlock(self, navigator: WorkflowNavigator):
        """Each step starts inside the previous one's rectangle.

        That overlap is what makes the row read as one connected process
        rather than nine separate tiles - and it is the reason the hit test
        below cannot be the rectangle.
        """
        plan = navigator.plan_for_width(width_for_mode(navigator, NavigatorMode.WIDE))
        for previous, current in zip(plan.placements, plan.placements[1:], strict=False):
            if current.rect.top() != previous.rect.top():
                continue
            assert current.rect.left() < previous.rect.right()

    def test_the_seam_between_two_chevrons_is_the_configured_gap(
        self, navigator: WorkflowNavigator
    ):
        """The seam is small and deliberately not zero.

        Two accent-adjacent steps with no seam read as one wide shape.
        """
        plan = navigator.plan_for_width(width_for_mode(navigator, NavigatorMode.WIDE))
        for previous, current in zip(plan.placements, plan.placements[1:], strict=False):
            if current.rect.top() != previous.rect.top():
                continue
            overlap = previous.rect.right() + 1 - current.rect.left()
            assert overlap == previous.geometry.arrow_depth - NavMetrics.STEP_GAP

    def test_the_hit_target_is_the_painted_shape_not_the_rectangle(
        self, navigator: WorkflowNavigator
    ):
        """The notch belongs to the previous step, not to this one.

        Without this, clicking the visible arrowhead of step 4 would land in
        step 5's rectangle and - because a rejected press is *ignored* rather
        than passed to the sibling beneath - do nothing at all.
        """
        apply_width(navigator, width_for_mode(navigator, NavigatorMode.WIDE))
        step = navigator.steps[1]
        middle_height = step.height() // 2

        # Just inside the left edge, level with the notch's apex: outside the
        # painted chevron even though it is inside the widget's rectangle.
        assert step.hitButton(QPoint(1, middle_height)) is False
        # Comfortably inside the body.
        assert step.hitButton(QPoint(step.width() // 2, middle_height)) is True

    def test_the_earlier_step_is_above_the_later_one(
        self, navigator: WorkflowNavigator
    ):
        """So the widget on top at any pixel is the one painted there."""
        apply_width(navigator, width_for_mode(navigator, NavigatorMode.WIDE))
        strip = navigator.steps[0].parentWidget()
        assert strip is not None
        order = [child for child in strip.children() if child in navigator.steps]
        # Qt paints children in list order, so the last is on top; the steps
        # were raised in reverse, leaving step 1 last.
        assert order[-1] is navigator.steps[0]

    def test_a_click_activates_the_stage_it_landed_on(
        self, navigator: WorkflowNavigator
    ):
        apply_width(navigator, width_for_mode(navigator, NavigatorMode.WIDE))
        received: list[str] = []
        navigator.step_activated.connect(received.append)

        step = navigator.step("calibration")
        assert step is not None
        QTest.mouseClick(
            step,
            Qt.MouseButton.LeftButton,
            pos=QPoint(step.width() // 2, step.height() // 2),
        )
        assert received == ["calibration"]

    def test_a_disabled_stage_does_not_emit_when_clicked(
        self, navigator: WorkflowNavigator
    ):
        apply_width(navigator, width_for_mode(navigator, NavigatorMode.WIDE))
        navigator.set_step_enabled("reports", enabled=False, reason="Locked.")
        received: list[str] = []
        navigator.step_activated.connect(received.append)

        step = navigator.step("reports")
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
    def test_every_stage_is_reachable_by_tab(self, navigator: WorkflowNavigator):
        assert all(
            step.focusPolicy() == Qt.FocusPolicy.StrongFocus for step in navigator.steps
        )

    def test_space_activates_the_focused_stage(self, navigator: WorkflowNavigator):
        received: list[str] = []
        navigator.step_activated.connect(received.append)
        step = navigator.step("scan")
        assert step is not None
        step.setFocus()
        QTest.keyClick(step, Qt.Key.Key_Space)
        assert received == ["scan"]

    def test_enter_activates_the_focused_stage(self, navigator: WorkflowNavigator):
        received: list[str] = []
        navigator.step_activated.connect(received.append)
        step = navigator.step("template")
        assert step is not None
        step.setFocus()
        step.click()
        assert received == ["template"]

    def test_the_arrow_keys_move_between_stages(self, navigator: WorkflowNavigator):
        """Both axes are accepted.

        The same nine stages are a row in one layout, two rows in another and
        a grid in a third, and an operator should not have to know which.
        """
        navigator.steps[0].setFocus()
        QTest.keyClick(navigator, Qt.Key.Key_Right)
        assert navigator.steps[1].hasFocus()
        QTest.keyClick(navigator, Qt.Key.Key_Left)
        assert navigator.steps[0].hasFocus()
        QTest.keyClick(navigator, Qt.Key.Key_Down)
        assert navigator.steps[1].hasFocus()

    def test_home_and_end_jump_to_the_ends(self, navigator: WorkflowNavigator):
        navigator.steps[3].setFocus()
        QTest.keyClick(navigator, Qt.Key.Key_End)
        assert navigator.steps[-1].hasFocus()
        QTest.keyClick(navigator, Qt.Key.Key_Home)
        assert navigator.steps[0].hasFocus()

    def test_arrow_movement_skips_disabled_stages(
        self, navigator: WorkflowNavigator
    ):
        """A disabled stage cannot be opened, so stopping on it is a dead end."""
        navigator.set_step_enabled("template", enabled=False, reason="Locked.")
        navigator.steps[0].setFocus()
        QTest.keyClick(navigator, Qt.Key.Key_Right)
        assert navigator.step("calibration").hasFocus()

    def test_there_is_no_keyboard_trap(self, navigator: WorkflowNavigator):
        """An unhandled key must fall through, or Tab could not leave."""
        navigator.steps[0].setFocus()
        QTest.keyClick(navigator, Qt.Key.Key_A)
        assert navigator.steps[0].hasFocus()


# ----------------------------------------------------------------------
# L - accessibility metadata
# ----------------------------------------------------------------------
class TestLAccessibility:
    def test_every_stage_has_an_accessible_name_including_its_number(
        self, navigator: WorkflowNavigator
    ):
        for step in navigator.steps:
            assert step.accessibleName() == step.display_text

    def test_every_stage_describes_itself(self, navigator: WorkflowNavigator):
        for step, spec in zip(navigator.steps, WORKFLOW_PAGES, strict=True):
            assert spec.summary in step.accessibleDescription()

    def test_a_disabled_stage_explains_itself_to_a_screen_reader(
        self, navigator: WorkflowNavigator
    ):
        navigator.set_step_enabled("reports", enabled=False, reason="Locked here.")
        step = navigator.step("reports")
        assert step is not None
        assert "Locked here." in step.accessibleDescription()

    def test_every_stage_has_a_status_tip(self, navigator: WorkflowNavigator):
        for step in navigator.steps:
            assert step.statusTip()


# ----------------------------------------------------------------------
# M - text scaling
# ----------------------------------------------------------------------
class TestMTextScaling:
    def test_a_larger_font_changes_the_layout_rather_than_clipping(
        self, navigator: WorkflowNavigator
    ):
        """The heart of the responsive requirement.

        A Windows text-scaling change reaches a widget as a font change. The
        steps re-measure, the width that used to fit nine chevrons no longer
        does, and the navigator moves to a layout that fits - it does not
        shrink the text back down or clip it.
        """
        wide = width_for_mode(navigator, NavigatorMode.WIDE)
        apply_width(navigator, wide)
        assert navigator.mode is NavigatorMode.WIDE

        large = QFont(navigator.font())
        large.setPointSizeF(navigator.font().pointSizeF() * 2.0)
        navigator.setFont(large)
        for step in navigator.steps:
            step.setFont(large)
        navigator._relayout()

        assert navigator.mode is not NavigatorMode.WIDE
        navigator.repaint()
        if navigator.mode is not NavigatorMode.COMPACT:
            assert all(step.is_label_elided is False for step in navigator.steps)

    def test_a_larger_font_makes_each_step_want_more_room(
        self, navigator: WorkflowNavigator
    ):
        step = navigator.steps[0]
        before = step.natural_width()
        large = QFont(step.font())
        large.setPointSizeF(step.font().pointSizeF() * 1.5)
        step.setFont(large)
        assert step.natural_width() > before
