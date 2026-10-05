"""Resolve's provenance line stays readable and never widens the page (revised phase 8).

Scope:
    The line beside the evidence tabs - scanner, batch, the file name the
    sheet arrived with, arrival - with deliberately long values, inside the
    real main window at 1366x768 and 1100x680 and at 100 / 150 / 200 %
    interface zoom; and the order in which it shortens.

What is asserted are layout invariants, measured against the fonts actually
in use - not pixel values:

* the line sits beside the tabs, inside the tab widget, and never squeezes a
  tab: the tab bar keeps its own size hint whenever the window has room;
* a long line does not change the page's or the window's minimum width;
* the batch and arrival time are always shown whole; the scanner name gives
  way before the file name, and the file name keeps its start and extension;
* the tooltip has every value complete; the content-addressed stored copy's
  name is never the name shown.

The application is configured (stylesheet, font) exactly as at start-up, so
the fonts measured are the same whether this module runs alone or after the
rest of ``tests/gui``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from PySide6.QtWidgets import QApplication

from omr_scanner.config import AppConfig
from omr_scanner.gui.application import configure_application
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.review import page as resolve_module
from omr_scanner.gui.review.provenance_label import SEPARATOR, ProvenanceLabel
from omr_scanner.gui.ui_scale import UiScaleManager
from omr_scanner.services import create_project
from omr_scanner.services.session_sheets import SheetProvenance

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from omr_scanner.gui.review.page import ResolvePage

pytestmark = pytest.mark.gui

SOURCE = "Scanner Station - Electrical Machines Laboratory North Wing"
ORIGINAL = "2026-10-05_Final_Examination_EEE_415_Section_A_Student_1000001_rescan_02.png"
STORED = "3f9a0c1e5b7d2a4c6e8f0a1b3c5d7e9f1a2b4c6d8e0f2a4b6c8d0e2f4a6b8c0d.png"
ARRIVED = datetime(2026, 10, 5, 4, 42, tzinfo=UTC)


def long_provenance(scan_id: int = 1) -> SheetProvenance:
    return SheetProvenance(
        scan_id=scan_id,
        source_label=SOURCE,
        batch_label=f"Batch 2 · {SOURCE}",
        batch_id="0123456789abcdef",
        original_name=ORIGINAL,
        stored_name=STORED,
        arrived_at=ARRIVED,
        read_at=ARRIVED,
    )


def short_provenance(scan_id: int = 1) -> SheetProvenance:
    return SheetProvenance(
        scan_id=scan_id,
        source_label="Scanner A",
        batch_label="Batch 2 · Scanner A",
        batch_id="0123456789abcdef",
        original_name="a.png",
        stored_name="a.png",
        arrived_at=ARRIVED,
        read_at=ARRIVED,
    )


def settle() -> None:
    for _ in range(4):
        QApplication.processEvents()


@pytest.fixture
def manager(qtbot) -> UiScaleManager:
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    configure_application(app)
    found = UiScaleManager.find()
    assert found is not None and found.percent == 100
    return found


@pytest.fixture
def window(qtbot, tmp_path: Path, manager, monkeypatch) -> Iterator[MainWindow]:
    project = create_project(tmp_path / "workspace", "Provenance Layout")
    root = project.root
    project.close()
    win = MainWindow(config=AppConfig(reviewer_name="Operator"), config_path=tmp_path / "c.json")
    qtbot.addWidget(win)
    assert win.open_project_at(root)
    assert win.show_page("resolve")
    yield win
    win.close()


def resolve_page(window: MainWindow) -> ResolvePage:
    page = window._resolve_page()
    assert page is not None
    return page


def show(page: ResolvePage, monkeypatch, found: SheetProvenance) -> ProvenanceLabel:
    monkeypatch.setattr(
        resolve_module.session_sheets, "sheet_provenance", lambda _db, _scan_id: found
    )
    page._show_provenance(found.scan_id)
    settle()
    label = page.provenance_context_label
    assert isinstance(label, ProvenanceLabel)
    return label


def visible_names(label: ProvenanceLabel) -> list[str]:
    return label.text().split(SEPARATOR)


class TestLongProvenanceLayout:
    @pytest.mark.parametrize("size", [(1366, 768), (1100, 680)])
    @pytest.mark.parametrize("percent", [100, 150, 200])
    def test_it_fits_beside_the_tabs_without_widening_anything(
        self, qtbot, window, manager, monkeypatch, size, percent
    ):
        page = resolve_page(window)
        manager.set_percent(percent)
        window.resize(*size)
        window.show()
        qtbot.waitExposed(window)
        show(page, monkeypatch, short_provenance())
        window_floor = window.minimumSizeHint().width()
        page_floor = page.minimumSizeHint().width()

        label = show(page, monkeypatch, long_provenance())
        tabs, bar = page.view_tabs, page.view_tabs.tabBar()

        # Nothing got wider for it.
        assert window.minimumSizeHint().width() == window_floor
        assert page.minimumSizeHint().width() == page_floor
        assert label.minimumSizeHint().width() == 0
        # Inside the tab widget, to the right of the last tab.
        assert label.isVisible()
        assert label.geometry().right() <= tabs.width()
        assert label.geometry().left() >= bar.geometry().right()
        # The tabs keep the room they ask for, and the evidence is usable.
        assert bar.width() >= min(bar.sizeHint().width(), tabs.width())
        assert tabs.currentWidget().width() > 0
        # The text shown is what fits.
        metrics = label.fontMetrics()
        assert metrics.horizontalAdvance(label.text()) <= label.contentsRect().width()

        # The full values are in the tooltip, one per line.
        tip = label.toolTip()
        assert f"Source: {SOURCE}" in tip
        assert f"Original file: {ORIGINAL}" in tip
        assert f"Stored as: {STORED}" in tip
        # The stored copy's name is never the name shown.
        assert STORED not in label.text() and STORED[:12] not in label.text()

    def test_at_1366_at_100_percent_the_file_name_stays_recognisable(
        self, qtbot, window, monkeypatch
    ):
        page = resolve_page(window)
        window.resize(1366, 768)
        window.show()
        qtbot.waitExposed(window)
        label = show(page, monkeypatch, long_provenance())
        text = label.text()
        assert label.contentsRect().width() > 0
        assert "Batch 2" in text and "arrived" in text
        # The start and the extension of the arrival name are still there.
        shown_name = next(part for part in visible_names(label) if part.endswith(".png"))
        assert shown_name.startswith("2026")

    def test_a_short_line_is_shown_whole(self, qtbot, window, monkeypatch):
        page = resolve_page(window)
        window.resize(1366, 768)
        window.show()
        qtbot.waitExposed(window)
        label = show(page, monkeypatch, short_provenance())
        assert label.text() == label.full_text
        assert label.text().startswith("Scanner A · Batch 2 · a.png · arrived ")

    def test_no_sheet_hides_the_line(self, qtbot, window, monkeypatch):
        page = resolve_page(window)
        show(page, monkeypatch, long_provenance())
        page._show_provenance(None)
        label = page.provenance_context_label
        assert label.isHidden() and label.text() == "" and label.toolTip() == ""
        assert page._provenance_names == ("", "")


class TestShorteningOrder:
    """:meth:`ProvenanceLabel.fitted_text` - what gives way first."""

    @pytest.fixture
    def label(self, qtbot, manager) -> ProvenanceLabel:
        widget = ProvenanceLabel()
        qtbot.addWidget(widget)
        widget.set_provenance(long_provenance())
        return widget

    def width_of(self, label: ProvenanceLabel, text: str) -> int:
        return label.fontMetrics().horizontalAdvance(text)

    def test_room_for_everything_shows_everything(self, label):
        full = label.full_text
        assert full == SEPARATOR.join(
            [SOURCE, "Batch 2", ORIGINAL, f"arrived {ARRIVED.astimezone():%H:%M}"]
        )
        assert label.fitted_text(self.width_of(label, full)) == full

    def test_the_scanner_name_gives_way_first(self, label):
        without_source = SEPARATOR.join(label.full_text.split(SEPARATOR)[1:])
        width = self.width_of(label, without_source) + self.width_of(label, " · Scanner Stat…")
        source, batch, name, arrival = label.fitted_text(width).split(SEPARATOR)
        assert source.endswith("…") and SOURCE.startswith(source[:-1])
        assert (batch, name) == ("Batch 2", ORIGINAL)
        assert arrival.startswith("arrived ")

    def test_then_the_file_name_from_the_middle(self, label):
        width = self.width_of(
            label, "Scanner… · Batch 2 · 2026-10-05_Fin…ent_1000001_rescan_02.png · arrived 00:00"
        )
        source, batch, name, arrival = label.fitted_text(width).split(SEPARATOR)
        assert source.endswith("…")
        assert batch == "Batch 2" and arrival.startswith("arrived ")
        assert "…" in name and name.startswith("2026") and name.endswith(".png")
        assert self.width_of(label, label.fitted_text(width)) <= width

    def test_too_narrow_for_both_the_file_stays(self, label):
        width = self.width_of(label, "Batch 2 · 2026…_02.png · arrived 00:00") + 4
        parts = label.fitted_text(width).split(SEPARATOR)
        assert parts[0] == "Batch 2" and parts[-1].startswith("arrived ")
        assert parts[1].endswith(".png")
        assert SOURCE[:6] not in label.fitted_text(width)

    def test_whatever_the_width_the_line_fits(self, label):
        for width in range(0, self.width_of(label, label.full_text) + 20, 7):
            assert self.width_of(label, label.fitted_text(width)) <= max(width, 0) or (
                label.fitted_text(width) in ("", "…")
            )
