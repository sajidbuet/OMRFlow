"""Long scanner and file names beside Resolve's evidence stay readable (revised phase 8, pre-merge).

The provenance corner (scanner · batch · original file · arrival) must never
widen the page or squeeze the evidence tabs, whatever the operator named the
scanner or the file. It shows what fits - eliding the middle, so the scanner's
name, the file's ending and the arrival time survive - and its tooltip has
every fact whole. The content-addressed project copy is never the file named.
"""

from __future__ import annotations

import re

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication
from tests.engine_rig import EngineRig, readable_sheets
from tests.integration.test_session_finish import BIG

from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.review.display_names import SHEET_NAME_LIMIT, middle_ellipsis
from omr_scanner.gui.review.page import ProvenanceLabel, ResolvePage, fit_provenance

pytestmark = pytest.mark.gui

STATION = "Station - Electrical Machines Laboratory North Wing"
LONG_FILE = "2026-10-05_Final_Examination_EEE_415_Section_A_Student_1000001_rescan_02.png"
SHEETS = readable_sheets(20)


@pytest.fixture
def rig(project_session) -> EngineRig:
    made = EngineRig(project_session)
    made.source(STATION)  # labelled "Scanner Station - ..."
    # Two sheets sharing a Student ID: a duplicate conflict on the long-named file.
    made.write(STATION, [(LONG_FILE, SHEETS[17]), ("short.png", SHEETS[18])])
    made.new_engine(unit_policy=BIG)
    made.make_ready()
    made.run()
    return made


def shown_page(qtbot, rig: EngineRig, width: int, height: int) -> ResolvePage:
    page = ResolvePage(next(item for item in WORKFLOW_PAGES if item.key == "resolve"))
    qtbot.addWidget(page)
    page.on_project_changed(rig.project)
    page.set_reviewer("Operator")
    page.resize(width, height)
    page.show()
    assert page.load_session(rig.session_id, rig.template)
    QApplication.processEvents()
    return page


def select_long_file(page: ResolvePage) -> None:
    for row, conflict in enumerate(page.state.conflicts):
        if conflict.scan_name == LONG_FILE:
            page.queue_table.selectRow(row)
            QApplication.processEvents()
            return
    raise AssertionError([item.scan_name for item in page.state.conflicts])


@pytest.mark.parametrize("size", [(1366, 768), (1100, 680)])
def test_long_provenance_is_elided_never_widening_the_page(qtbot, rig, size):
    page = shown_page(qtbot, rig, *size)
    minimum_before = page.minimumSizeHint().width()
    tabs_before = page.view_tabs.minimumSizeHint().width()
    select_long_file(page)
    label = page.provenance_context_label
    assert isinstance(label, ProvenanceLabel)
    assert label.isVisibleTo(page)
    # The whole value is held; the original name, never the project copy's.
    assert f"Scanner {STATION}" in label.full_text
    assert LONG_FILE in label.full_text
    assert not re.search(r"[0-9a-f]{32,}\.png", label.full_text)
    # No horizontal expansion caused by the text: neither the page's nor the
    # tab widget's minimum moved, and the label needs no width at all.
    assert page.minimumSizeHint().width() == minimum_before
    assert page.view_tabs.minimumSizeHint().width() == tabs_before
    assert label.minimumSizeHint().width() == 0
    # It fits beside the tabs (the tabs keep their own width) ...
    bar = page.view_tabs.tabBar()
    assert label.width() <= page.view_tabs.width() - bar.sizeHint().width()
    assert bar.width() >= bar.sizeHint().width()
    # ... showing what fits: this value is longer than the room at either
    # size, so it is shortened - the file's ending and its arrival kept.
    assert label.is_elided
    assert "…" in label.text()
    assert "rescan_02.png" in label.text()
    assert label.text().endswith("arrived " + label.full_text[-5:])
    assert label.fontMetrics().horizontalAdvance(label.text()) <= label.width()
    # The tooltip has every fact whole, one per line.
    tip = label.toolTip()
    assert f"Scanner: Scanner {STATION}" in tip
    assert f"File: {LONG_FILE}" in tip
    assert "Arrived:" in tip
    assert "Stored in the project as:" in tip


def test_the_file_and_its_arrival_give_way_last(qapp):
    metrics = QFontMetrics(QApplication.font())
    parts = (f"Scanner {STATION}", "Batch 3", LONG_FILE, "arrived 15:01")
    full = " · ".join(parts)
    width = metrics.horizontalAdvance
    assert fit_provenance(parts, metrics, width(full)) == full
    # A little short: only the scanner's name is shortened; the file is whole.
    shown = fit_provenance(parts, metrics, width(full) - width(STATION) // 2)
    assert shown.startswith("Scanner") and "…" in shown.split(" · ")[0]
    assert LONG_FILE in shown and "Batch 3" in shown and shown.endswith("arrived 15:01")
    # Shorter: the batch goes to the tooltip before the file is touched.
    room = width(f"Scanner · {LONG_FILE} · arrived 15:01")
    shown = fit_provenance(parts, metrics, room)
    assert LONG_FILE in shown and "Batch 3" not in shown
    # Tight: the file itself is elided in the middle, its ending kept.
    shown = fit_provenance(parts, metrics, width(LONG_FILE) // 2)
    assert shown.endswith("_02.png · arrived 15:01")
    assert shown.startswith("2026-10-05_")  # both ends of the file name survive
    assert "…" in shown and width(shown) <= width(LONG_FILE) // 2


def test_a_long_scanner_name_does_not_crowd_the_batch_filter(qtbot, rig):
    page = shown_page(qtbot, rig, 1366, 768)
    source = page.source_filter
    assert source.isVisibleTo(page)
    index = source.findText(f"Scanner {STATION}")
    assert index > 0
    assert source.itemData(index, Qt.ItemDataRole.ToolTipRole) == f"Scanner {STATION}"
    # The source filter is not sized by its long item, so the batch filter
    # beside it keeps room for its own text.
    width = source.fontMetrics().horizontalAdvance(f"Scanner {STATION}")
    assert source.minimumSizeHint().width() < width
    assert source.sizeHint().width() < width
    batch = page.batch_filter
    assert batch.width() >= batch.fontMetrics().horizontalAdvance(batch.currentText())


def test_panel_names_are_bounded_whatever_their_length():
    assert middle_ellipsis("short.png") == "short.png"
    shown = middle_ellipsis(LONG_FILE)
    assert len(shown) == SHEET_NAME_LIMIT
    assert shown.startswith("2026-10-05") and shown.endswith("rescan_02.png") and "…" in shown
    # Length-independent: a name ten times longer takes exactly the same room.
    assert len(middle_ellipsis(LONG_FILE * 10)) == SHEET_NAME_LIMIT


def test_the_decision_panel_shows_the_long_name_bounded(qtbot, rig):
    page = shown_page(qtbot, rig, 1366, 768)
    select_long_file(page)
    text = page.machine_summary_label.text()
    assert middle_ellipsis(LONG_FILE) in text
    assert LONG_FILE not in text  # whole only in the provenance tooltip
    assert LONG_FILE in page.provenance_context_label.toolTip()


def test_a_narrow_window_elides_more_and_a_wide_one_shows_it_whole(qtbot, rig):
    page = shown_page(qtbot, rig, 1366, 768)
    select_long_file(page)
    label = page.provenance_context_label
    page.resize(900, 680)
    QApplication.processEvents()
    narrow = len(label.text())
    page.resize(3000, 900)
    QApplication.processEvents()
    assert len(label.text()) >= narrow
    if label.width() >= label.fontMetrics().horizontalAdvance(label.full_text) + 8:
        assert not label.is_elided
