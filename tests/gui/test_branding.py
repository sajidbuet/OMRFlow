"""Tests for the OMRFlow logo, application icon and footer attribution.

Scope:
    Where the branding lives in the new application shell, that the wordmark
    is never distorted, and that the footer states the build, the licence, the
    credit and the status.

    The shell this file used to describe had a fixed left sidebar with the
    logo at its foot, and then a branded header band above a separate
    navigator band. Both are gone: the wordmark now sits in the single chrome
    row that is also the window's title bar, and the footer is a real status
    band rather than a centred credit line. The asset-loading tests below are
    unchanged, because how an asset is *resolved* did not change - only where
    it is shown.

Policy:
    Existence and relationship checks, not pixel coordinates - see
    `docs/TESTING.md`. Where a coordinate is asserted it is a *relationship*
    ("the pages start directly below the chrome row"), which is the property
    that would actually regress.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import QApplication

from omr_scanner import LICENSE_NAME, __version__
from omr_scanner.config import AppConfig
from omr_scanner.gui.about_dialog import DEVELOPER_NAME
from omr_scanner.gui.branding import LOGO_ASPECT_RATIO, application_icon, logo_svg_path
from omr_scanner.gui.main_window import DEVELOPER_URL, MainWindow
from omr_scanner.gui.theme import Chrome
from omr_scanner.gui.widgets.status_footer import AppStatus

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


class TestLogoInTheChromeRow:
    """The wordmark lives in the chrome row, and is never stretched.

    How the artwork is *rendered* - and the view-box narrowing that stopped it
    being stretched by half again - belongs with the rest of the chrome row,
    in ``test_window_chrome.py``. What is here is where the asset comes from
    and that exactly one of it reaches the shell.
    """

    def test_the_logo_is_an_svg_widget(self, window: MainWindow):
        assert isinstance(window.chrome.logo, QSvgWidget)

    def test_the_logo_is_a_child_of_the_window(self, window: MainWindow):
        assert window.isAncestorOf(window.chrome.logo)

    def test_the_logo_aspect_ratio_is_preserved_not_stretched(self, window: MainWindow):
        """The one branding property that is not a matter of taste.

        A wordmark stretched in either axis is wrong at any size, so the
        displayed ratio is checked against the artwork's own measured ratio
        rather than against a hard-coded width and height.
        """
        size = window.chrome.logo.size()
        assert size.width() / size.height() == pytest.approx(LOGO_ASPECT_RATIO, rel=0.02)

    def test_the_logo_is_rendered_as_a_vector_for_high_dpi(self, window: MainWindow):
        """A `QSvgWidget` re-renders at the device pixel ratio.

        Asserted because the alternative - a `QPixmap` scaled to the row's
        height - looks identical at 100% scaling and visibly soft at 150%,
        which is exactly the kind of regression nobody notices in a test that
        only checks the size.
        """
        assert isinstance(window.chrome.logo, QSvgWidget)
        assert logo_svg_path().suffix == ".svg"

    def test_the_whole_chrome_is_one_compact_row(self, window: MainWindow):
        """It replaces the menu bar *and* the navigator band, not adds to them."""
        assert window.chrome.height() == Chrome.HEIGHT
        assert Chrome.HEIGHT <= 56

    def test_the_window_icon_is_set(self, window: MainWindow):
        assert not window.windowIcon().isNull()

    def test_the_old_sidebar_is_gone_entirely(self, window: MainWindow):
        """Removed, not merely emptied or hidden.

        A hidden sidebar would still occupy its place in the layout, which is
        the "empty spacer where the sidebar used to be" the redesign brief
        rules out.
        """
        from PySide6.QtWidgets import QWidget

        assert window.findChild(QWidget, "workflowSidebar") is None
        assert window.findChild(QWidget, "workflowNavigation") is None
        assert not hasattr(window, "navigation")

    def test_the_logo_is_not_duplicated_across_the_shell(self, window: MainWindow):
        """One wordmark. A second copy would be decoration with no purpose."""
        logos = window.findChildren(QSvgWidget, "appLogo")
        assert len(logos) == 1


class TestFooterAttribution:
    def test_the_footer_shows_the_real_build_version(self, window: MainWindow):
        """Read from package metadata, never typed in.

        `__version__` is the same value `pyproject.toml` declares, so a
        release cannot leave the footer claiming the previous one.
        """
        assert __version__ in window.footer.version_label.text()
        assert "OMRFlow" in window.footer.version_label.text()

    def test_the_footer_states_the_repositorys_actual_licence(self, window: MainWindow):
        text = window.footer.licence_label.text()
        assert LICENSE_NAME in text
        assert "Open Source" in text
        assert LICENSE_NAME == "MIT"

    def test_the_stated_licence_matches_the_license_file(self):
        """The footer must not claim a licence the repository does not carry."""
        licence_file = Path(__file__).resolve().parents[2] / "LICENSE"
        assert licence_file.is_file()
        assert "MIT License" in licence_file.read_text(encoding="utf-8")

    def test_the_footer_shows_the_developer_name(self, window: MainWindow):
        assert DEVELOPER_NAME in window.footer.credit_label.text()

    def test_the_footer_link_points_at_the_developer_site(self, window: MainWindow):
        assert f'href="{DEVELOPER_URL}"' in window.footer.credit_label.text()
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

    def test_the_footer_routes_its_link_through_the_window(self, window: MainWindow):
        """The footer emits; the window is the only place that opens a URL."""
        assert window.footer.credit_label.openExternalLinks() is False
        received: list[str] = []
        window.footer.developer_link_activated.connect(received.append)
        window.footer.developer_link_activated.emit(DEVELOPER_URL)
        assert received == [DEVELOPER_URL]

    def test_the_footer_is_a_child_of_the_window(self, window: MainWindow):
        assert window.isAncestorOf(window.footer)

    def test_the_credit_is_visually_secondary(self, window: MainWindow):
        default_size = window.font().pointSizeF()
        assert window.footer.credit_label.font().pointSizeF() < default_size

    def test_the_status_is_ready_with_no_batch_running(self, window: MainWindow):
        assert window.footer.status is AppStatus.READY
        assert window.footer.status_label.text() == "Ready"

    def test_the_status_is_a_word_and_not_only_a_coloured_dot(self, window: MainWindow):
        """State must never be carried by colour alone.

        The dot is a redundant accent on text that already says the state, so
        an operator who cannot distinguish the colours - or who is reading a
        black-and-white screenshot - still knows what the application is
        doing.
        """
        for status in AppStatus:
            window.footer.set_status(status)
            assert window.footer.status_label.text() == status.value
            assert window.footer.status_label.text().strip() != ""


class TestNoWastedChrome:
    """The shell's bands sit directly on top of one another."""

    def test_the_chrome_row_is_the_first_band_in_the_central_widget(
        self, window: MainWindow
    ):
        window.show()
        QApplication.processEvents()
        assert window.chrome.y() == 0

    def test_the_pages_sit_directly_below_the_chrome_row(self, window: MainWindow):
        """No gap, and nothing between them.

        The brief calls out any blank vertical band above the content as a
        defect, so the relationship is asserted rather than the coordinate -
        and there is now only one band to be below.
        """
        window.show()
        QApplication.processEvents()
        assert window.stack.y() == window.chrome.y() + window.chrome.height()

    def test_the_footer_is_the_last_band_above_the_status_bar(self, window: MainWindow):
        window.show()
        QApplication.processEvents()
        assert window.footer.y() == window.stack.y() + window.stack.height()

    def test_the_pages_get_the_full_window_width(self, window: MainWindow):
        """The width the sidebar used to take now belongs to the content."""
        window.resize(1280, 800)
        window.show()
        QApplication.processEvents()
        central = window.centralWidget()
        assert central is not None
        assert window.stack.width() == central.width()
        assert window.stack.x() == 0


class TestExistingChromeUnaffected:
    """The redesign must not disturb the pre-existing status bar."""

    def test_the_status_bar_no_longer_duplicates_the_project_indicator(
        self, window: MainWindow
    ):
        """The footer states the open project; the status bar used to as well.

        Its permanent widget read ``"<folder name>  (<absolute path>)"``, two
        rows below a footer that now names the *examination* - so it was both
        a duplicate and the long filesystem path the footer's own brief asks
        not to put permanently on screen. The bar keeps its transient
        messages, which is the next test.
        """
        from PySide6.QtWidgets import QLabel

        assert not hasattr(window, "_project_status")
        permanent = [
            label.text()
            for label in window.statusBar().findChildren(QLabel)
            if label.text()
        ]
        assert "No project open" not in permanent

    def test_the_footer_is_where_the_open_project_is_stated(
        self, window: MainWindow, tmp_path: Path
    ):
        """What replaced it, and by a title rather than a path."""
        assert window.create_project_at(tmp_path, "Status Bar Project")
        assert window.footer.project_title == "Status Bar Project"
        assert str(tmp_path) not in window.footer.project_label.full_text

    def test_a_transient_status_message_still_works(self, window: MainWindow):
        window.statusBar().showMessage("Testing")
        assert window.statusBar().currentMessage() == "Testing"

