"""Tests for the OMR Flow logo, application icon and footer attribution.

Scope:
    Existence/wiring checks, not pixel-coordinate assertions - matching the
    project's GUI testing policy (`docs/TESTING.md`). Visual placement
    (bottom of the sidebar, no overlap, resize behaviour) was verified
    manually; see the branding integration's completion reports.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import QApplication, QWidget

from omr_scanner.config import AppConfig
from omr_scanner.gui.about_dialog import DEVELOPER_NAME
from omr_scanner.gui.branding import LOGO_ASPECT_RATIO, application_icon, logo_svg_path
from omr_scanner.gui.main_window import DEVELOPER_URL, NAVIGATION_WIDTH, MainWindow

pytestmark = pytest.mark.gui


@pytest.fixture(autouse=True)
def _qapp_ready(qtbot) -> None:
    """Every test here may load a Qt icon/image; make sure a `QApplication` exists first.

    Without this, a test module whose *first* Qt-touching test never requests
    `qtbot` itself can crash the interpreter outright (observed with
    `QIcon`/`QImage` loading from an `.ico` file) rather than raising a normal
    Python exception - see the equivalent note in `gui/icons.py`'s tests.
    """


@pytest.fixture
def window(qtbot, tmp_path: Path) -> MainWindow:
    main_window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
    qtbot.addWidget(main_window)
    return main_window


class TestBrandingAssetLoading:
    """Asset resolution must not depend on the current working directory."""

    def test_logo_svg_path_resolves_to_a_real_file(self):
        assert logo_svg_path().is_file()

    def test_logo_svg_path_resolves_regardless_of_cwd(self, tmp_path: Path):
        previous = Path.cwd()
        os.chdir(tmp_path)
        try:
            assert logo_svg_path().is_file()
        finally:
            os.chdir(previous)

    def test_application_icon_is_not_null(self):
        icon = application_icon()
        assert isinstance(icon, QIcon)
        assert not icon.isNull()

    def test_application_icon_exposes_every_expected_resolution(self):
        sizes = {size.width() for size in application_icon().availableSizes()}
        assert {16, 24, 32, 48, 64, 128, 256}.issubset(sizes)

    def test_application_icon_is_cached(self):
        assert application_icon() is application_icon()


class TestLogoWidget:
    def test_the_main_window_displays_the_logo_as_an_svg_widget(self, window: MainWindow):
        assert isinstance(window.logo_widget, QSvgWidget)

    def test_the_logo_widget_is_a_child_of_the_window(self, window: MainWindow):
        assert window.isAncestorOf(window.logo_widget)

    def test_the_logo_size_is_within_the_recommended_branding_range(self, window: MainWindow):
        # "Approximately 100-160px wide" per the branding brief.
        assert 90 <= window.logo_widget.width() <= 170

    def test_the_logo_aspect_ratio_is_preserved_not_stretched(self, window: MainWindow):
        size = window.logo_widget.size()
        displayed_ratio = size.width() / size.height()
        assert displayed_ratio == pytest.approx(LOGO_ASPECT_RATIO, rel=0.02)

    def test_the_window_icon_is_set(self, window: MainWindow):
        assert not window.windowIcon().isNull()

    def test_the_logo_lives_in_the_sidebar_not_a_separate_header(self, window: MainWindow):
        parent = window.logo_widget.parentWidget()
        assert parent is not None
        assert parent.objectName() == "workflowSidebar"

    def test_the_logo_sits_below_the_navigation_list_in_the_same_sidebar(
        self, window: MainWindow
    ):
        assert window.logo_widget.parentWidget() is window.navigation.parentWidget()
        window.show()
        QApplication.processEvents()
        assert window.logo_widget.y() > window.navigation.y() + window.navigation.height() - 1

    def test_the_sidebar_width_matches_the_navigation_width(self, window: MainWindow):
        window.show()
        QApplication.processEvents()
        sidebar = window.logo_widget.parentWidget()
        assert sidebar is not None
        assert sidebar.width() == NAVIGATION_WIDTH
        assert window.navigation.width() == NAVIGATION_WIDTH

    def test_the_logo_is_horizontally_centred_in_the_sidebar(self, window: MainWindow):
        window.show()
        QApplication.processEvents()
        sidebar = window.logo_widget.parentWidget()
        assert sidebar is not None
        left_gap = window.logo_widget.x()
        right_gap = sidebar.width() - (window.logo_widget.x() + window.logo_widget.width())
        assert left_gap == pytest.approx(right_gap, abs=1)

    def test_no_separate_header_widget_remains(self, window: MainWindow):
        assert window.findChild(QWidget, "appHeader") is None


class TestFooterAttribution:
    def test_the_footer_shows_the_developer_name(self, window: MainWindow):
        assert DEVELOPER_NAME in window.footer_label.text()

    def test_the_footer_link_points_at_the_developer_site(self, window: MainWindow):
        assert f'href="{DEVELOPER_URL}"' in window.footer_label.text()
        assert DEVELOPER_URL == "https://www.sajid.bd"

    def test_clicking_the_link_opens_the_system_browser_not_an_internal_view(
        self, window: MainWindow, monkeypatch: pytest.MonkeyPatch
    ):
        opened: list[str] = []
        monkeypatch.setattr(
            QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString()))
        )
        window._open_developer_site(DEVELOPER_URL)
        assert opened == [DEVELOPER_URL]

    def test_the_footer_does_not_auto_navigate_internally(self, window: MainWindow):
        # `setOpenExternalLinks(False)` - the link is routed through
        # `_open_developer_site`/`QDesktopServices` instead of Qt trying to
        # load the URL as if it were a local document.
        assert window.footer_label.openExternalLinks() is False

    def test_the_footer_is_a_child_of_the_window(self, window: MainWindow):
        assert window.isAncestorOf(window.footer_label)

    def test_the_footer_uses_a_smaller_font_than_the_default(self, window: MainWindow):
        default_size = window.font().pointSizeF()
        assert window.footer_label.font().pointSizeF() < default_size


class TestNoWastedHeaderSpace:
    def test_the_workflow_area_starts_at_the_top_of_the_central_widget(
        self, window: MainWindow
    ):
        # No header row above it any more - the sidebar/stack row is the
        # first thing in the central widget's layout.
        window.show()
        QApplication.processEvents()
        assert window.navigation.y() == 0

    def test_the_stacked_page_starts_at_the_top_of_the_content_pane(self, window: MainWindow):
        window.show()
        QApplication.processEvents()
        assert window.stack.y() == 0


class TestExistingChromeUnaffected:
    """The branding additions must not disturb the pre-existing status bar."""

    def test_the_status_bar_still_shows_the_no_project_message(self, window: MainWindow):
        from omr_scanner.gui.main_window import NO_PROJECT_STATUS

        assert window._project_status.text() == NO_PROJECT_STATUS

    def test_a_transient_status_message_still_works(self, window: MainWindow):
        window.statusBar().showMessage("Testing")
        assert window.statusBar().currentMessage() == "Testing"
