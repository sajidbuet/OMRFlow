"""The calibration page's layout: what gets the height, and what folds away.

What these lock down:
    The page's subject is the registered scan, and it was not getting the
    room. A ten-line status paragraph and an always-open results table sat
    permanently under the preview; with realistic content the preview held
    about 57% of the page (547 px at 1920x1080) and the paragraph alone took
    140 px of it.

    The fix is hierarchy, not smaller type: the paragraph became a one-row
    chip strip, the table moved into a drawer that starts shut, and the split
    is decided by stretch factors rather than fixed pixel heights so the
    preview grows with the window.

    ===== ==========================================================
    Test  Behaviour
    ===== ==========================================================
    A     The preview dominates, and gains the drawer's space when shut.
    B     Status is one compact strip, with words as well as colour.
    C     What folds away, and what must never fold away with it.
    D     Fit follows the viewport; a chosen zoom does not.
    ===== ==========================================================

Why heights are asserted as fractions:
    An absolute pixel assertion would encode this machine's fonts and chrome.
    The claim worth pinning is "the preview is the dominant component", which
    is a ratio.
"""

from __future__ import annotations

import pytest

from omr_scanner.gui.calibration.page import (
    DRAWER_STRETCH,
    PREVIEW_STRETCH,
    CalibrationPage,
    _status_chips,
)
from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
from omr_scanner.gui.widgets import CollapsibleSection

pytestmark = pytest.mark.gui

SPEC = next(spec for spec in WORKFLOW_PAGES if spec.key == "calibration")


@pytest.fixture
def page(qtbot) -> CalibrationPage:
    widget = CalibrationPage(SPEC)
    qtbot.addWidget(widget)
    widget.resize(1600, 900)
    widget.show()
    qtbot.waitExposed(widget)
    return widget


def _fraction(page: CalibrationPage) -> float:
    return page.preview.height() / max(1, page.height())


# ----------------------------------------------------------------------
# A - the preview dominates
# ----------------------------------------------------------------------
class TestAThePreviewIsTheDominantArea:
    def test_it_takes_most_of_the_page_with_the_drawer_shut(self, page, qtbot):
        """The default state, and the one that was 57% before."""
        page.results_drawer.set_expanded(False)
        qtbot.wait(10)
        assert _fraction(page) > 0.70

    def test_it_still_dominates_with_the_drawer_open(self, page, qtbot):
        page.results_drawer.set_expanded(True)
        qtbot.wait(10)
        assert _fraction(page) > 0.50

    def test_opening_the_drawer_costs_the_preview_height(self, page, qtbot):
        page.results_drawer.set_expanded(False)
        qtbot.wait(10)
        shut = page.preview.height()
        page.results_drawer.set_expanded(True)
        qtbot.wait(10)
        assert page.preview.height() < shut

    def test_closing_the_drawer_gives_the_height_back(self, page, qtbot):
        page.results_drawer.set_expanded(True)
        qtbot.wait(10)
        open_height = page.preview.height()
        page.results_drawer.set_expanded(False)
        qtbot.wait(10)
        assert page.preview.height() > open_height

    def test_a_taller_window_makes_a_taller_preview(self, page, qtbot):
        """Stretch factors, not a fixed height - the defect being avoided.

        Measured against the height the page was actually given, not the
        height it asked for: on a real desktop the window manager clamps a
        window to the screen, so at a high display scaling ``resize(1600,
        1000)`` can land well short of 1000 and a test comparing against the
        requested figure fails for a reason that has nothing to do with the
        layout.
        """
        page.resize(1600, 700)
        qtbot.wait(10)
        short_page = page.height()
        short = page.preview.height()
        page.resize(1600, 1000)
        qtbot.wait(10)
        gained = page.height() - short_page
        if gained < 250:
            pytest.skip(f"the window manager granted only {gained} px of the 300 asked for")
        # Nearly all of the extra height reaches the preview, which is what
        # having the largest stretch factor means.
        assert page.preview.height() - short > gained * 0.6

    def test_the_preview_is_weighted_above_the_drawer(self):
        assert PREVIEW_STRETCH > DRAWER_STRETCH

    def test_the_preview_has_a_floor_but_not_a_ceiling(self, page):
        assert page.preview.minimumHeight() > 0
        assert page.preview.maximumHeight() > 10_000, "a ceiling would cap growth"


# ----------------------------------------------------------------------
# B - the status strip
# ----------------------------------------------------------------------
class TestBStatusIsCompact:
    def test_the_verbose_panel_is_no_longer_under_the_preview(self, page):
        """It still exists and is still filled - it lives in the drawer now.

        The information was reorganised, not deleted.
        """
        assert page.calibration_summary_panel is not None
        assert page.details_section.content is page.calibration_summary_panel
        assert isinstance(page.details_section, CollapsibleSection)

    def test_the_strip_starts_empty(self, page):
        assert page.status_chip_strip.chip_texts() == []

    def test_every_chip_says_something_without_its_colour(self, page):
        """Colour alone is not a status anybody can read (WCAG 1.4.1)."""
        page.status_chip_strip.set_chips(
            [("ok", "Registration 4/4"), ("warn", "31 review"), ("fail", "Failed")]
        )
        texts = page.status_chip_strip.chip_texts()
        assert texts == ["Registration 4/4", "31 review", "Failed"]
        assert all(text.strip() for text in texts)

    def test_setting_chips_replaces_rather_than_appends(self, page):
        page.status_chip_strip.set_chips([("ok", "one"), ("ok", "two")])
        page.status_chip_strip.set_chips([("ok", "three")])
        assert page.status_chip_strip.chip_texts() == ["three"]

    def test_the_strip_wraps_instead_of_forcing_the_page_wider(self, page, qtbot):
        """Chip count depends on what the engine found; the page width does not."""
        page.status_chip_strip.set_chips([("neutral", f"chip {n}") for n in range(20)])
        qtbot.wait(10)
        layout = page.status_chip_strip.layout()
        narrow = layout.heightForWidth(200)
        wide = layout.heightForWidth(2000)
        assert narrow > wide, "a narrow strip should need more rows"

    def test_a_failed_registration_reads_as_failed(self):
        """Semantics unchanged: the chips restate the engine, never reinterpret it."""

        class _Report:
            registered = False
            markers_detected = 0
            orientation_ok = False
            answers_single = answers_blank = answers_multiple = 0
            answers_needing_review = 0

        class _Result:
            identifier = None
            set_code = None

        chips = _status_chips(_Result(), _Report())
        assert chips[0] == ("fail", "Registration failed")


# ----------------------------------------------------------------------
# C - what folds, and what must not fold with it
# ----------------------------------------------------------------------
class TestCCollapsibleSections:
    @pytest.mark.parametrize(
        "section",
        ["results_drawer", "threshold_section", "field_diagnostics_section",
         "details_section"],
    )
    def test_it_is_collapsible_and_starts_shut(self, page, section: str):
        widget = getattr(page, section)
        assert isinstance(widget, CollapsibleSection)
        assert widget.is_expanded is False

    @pytest.mark.parametrize(
        "section",
        ["results_drawer", "threshold_section", "field_diagnostics_section"],
    )
    def test_its_header_is_reachable_from_the_keyboard(self, page, section: str):
        from PySide6.QtCore import Qt

        header = getattr(page, section).header
        assert header.focusPolicy() != Qt.FocusPolicy.NoFocus
        assert header.accessibleName()

    def test_folding_the_thresholds_does_not_hide_save_or_export(self, page, qtbot):
        """The defect this test exists for.

        Save and Export Report used to live inside the threshold group, which
        was harmless while it was always open and hid both buttons the moment
        it folded. Neither is a threshold control.
        """
        page.threshold_section.set_expanded(False)
        qtbot.wait(10)
        assert page.save_button.isVisibleTo(page)
        assert page.export_report_button.isVisibleTo(page)

    def test_the_threshold_controls_survive_folding(self, page, qtbot):
        """Folding hides; it must never reset a working value."""
        page.threshold_section.set_expanded(True)
        qtbot.wait(10)
        before = page.state.working_settings
        page.threshold_section.set_expanded(False)
        page.threshold_section.set_expanded(True)
        qtbot.wait(10)
        assert page.state.working_settings == before

    def test_the_sidebar_scrolls_rather_than_pushing_controls_off(self, page):
        """The sidebar scrolls rather than pushing controls off the bottom.

        Expanding the thresholds on a short window must not make Export
        unreachable.
        """
        from PySide6.QtWidgets import QScrollArea

        scrollers = page.findChildren(QScrollArea, "calibrationControlScroll")
        assert scrollers, "the control column should be scrollable"
        assert scrollers[0].widgetResizable() is True

    def test_the_field_diagnostics_list_scrolls(self, page):
        from PySide6.QtWidgets import QScrollArea

        assert page.findChildren(QScrollArea, "fieldDiagnosticsScroll")


# ----------------------------------------------------------------------
# D - fit behaviour
# ----------------------------------------------------------------------
class TestDFitFollowsTheViewport:
    def test_fit_is_the_starting_mode(self, page):
        assert page._zoom_is_fit is True

    def test_zooming_stops_the_automatic_refit(self, page):
        """A zoom the user chose is theirs.

        Collapsing the drawer afterwards must not silently snap back to fit.
        """
        page._manual_zoom("in")
        assert page._zoom_is_fit is False
        page._actual_size_preview()
        assert page._zoom_is_fit is False

    def test_pressing_fit_restores_the_mode(self, page):
        page._manual_zoom("in")
        page._fit_preview()
        assert page._zoom_is_fit is True

    def test_toggling_the_drawer_refits_only_when_fitting(self, page, qtbot):
        calls: list[int] = []
        page.preview.fit_to_window = lambda: calls.append(1)  # type: ignore[method-assign]

        page.results_drawer.set_expanded(True)
        qtbot.wait(10)
        assert calls, "a fitted page should be re-fitted for the new viewport"

        page._zoom_is_fit = False
        calls.clear()
        page.results_drawer.set_expanded(False)
        qtbot.wait(10)
        assert calls == [], "a manually chosen zoom must be left alone"
