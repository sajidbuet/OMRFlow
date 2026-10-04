"""The session sheet list is an SQL-paged model, bounded at 100,000 rows (revised phase 8).

A metadata-only population (``tests/snapshot_population.py``: rows, no images,
no recognition) of 100,000 sheets in one session. The list must hold one page
- never the session - and every filter, sort and page turn must be a bounded
SQL read, made in the list's worker thread (never on the GUI thread).
Timings are recorded (printed with ``-s``) and checked against generous
ceilings; this is a scalability test of the list, not the phase 9 recognition
qualification.
"""

from __future__ import annotations

import time
import tracemalloc

import pytest
from PySide6.QtCore import Qt, QThread
from tests.snapshot_population import build

from omr_scanner.gui.scan.session_table_model import SessionSheetList
from omr_scanner.services import create_project, session_sheets
from omr_scanner.services.session_sheets import PAGE_SIZE, QualityFilter, StatusFilter

pytestmark = pytest.mark.gui

ROWS = 100_000
CEILING_MS = 2_000.0
"""Per list operation at 100,000 rows, end to end (request, worker read, table
shown). Generous on purpose - the numbers recorded in the handoff are what
matter; this catches a list that walks the session (tens of seconds here)."""


@pytest.fixture(scope="module")
def population(tmp_path_factory):
    folder = tmp_path_factory.mktemp("paging")
    project = create_project(folder, "Paging scale")
    try:
        built = build(project.database, sheets=ROWS)
        yield project, built
    finally:
        project.close()


@pytest.fixture
def widget(qtbot):
    made = SessionSheetList()
    qtbot.addWidget(made)
    yield made
    made.shutdown()


def loaded(qtbot, widget: SessionSheetList, action) -> float:
    """Run ``action`` and wait until the list shows the page it asked for."""
    started = time.perf_counter()
    with qtbot.waitSignal(widget.page_loaded, timeout=60_000):
        action()
    return (time.perf_counter() - started) * 1000.0


def test_a_100k_session_list_holds_one_page_and_every_operation_is_bounded(
    qtbot, population, widget
):
    project, built = population
    tracemalloc.start()
    try:
        first = loaded(
            qtbot, widget, lambda: widget.set_session(project.database, built.scan_session_id)
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert widget.total == ROWS
    assert widget.model.rowCount() == PAGE_SIZE  # one page, never the session
    assert widget.page_label.text() == f"1-{PAGE_SIZE} of {ROWS:,}"
    # The reads happen in the list's own thread.
    assert widget._thread is not None and widget._thread is not QThread.currentThread()

    next_page = loaded(qtbot, widget, widget.next_page)
    assert widget.model.rowCount() == PAGE_SIZE
    assert widget.page_label.text().startswith(f"{PAGE_SIZE + 1:,}-")

    failed = loaded(
        qtbot,
        widget,
        lambda: widget.status_combo.setCurrentIndex(
            widget.status_combo.findData(StatusFilter.FAILED)
        ),
    )
    assert 0 < widget.total < ROWS
    assert all(row.status == "failed" for row in widget.model.rows)
    assert widget.model.rowCount() <= PAGE_SIZE

    suggested = loaded(
        qtbot,
        widget,
        lambda: widget.quality_combo.setCurrentIndex(
            widget.quality_combo.findData(QualityFilter.SUGGESTED)
        ),
    )
    assert all(row.suggestion_outstanding for row in widget.model.rows)

    loaded(qtbot, widget, lambda: widget.status_combo.setCurrentIndex(0))
    loaded(qtbot, widget, lambda: widget.quality_combo.setCurrentIndex(0))
    sort = loaded(qtbot, widget, lambda: widget.model.sort(1, Qt.SortOrder.AscendingOrder))
    ids = [row.student_id for row in widget.model.rows]
    assert ids == sorted(ids)

    widget.search_edit.setText("0099999")
    search = loaded(qtbot, widget, widget.search_edit.editingFinished.emit)
    assert widget.total >= 1
    assert all(
        "0099999" in row.original_name or "0099999" in row.student_id
        for row in widget.model.rows
    )

    print(
        f"\n100k session list (end to end, worker thread): first page {first:.0f} ms "
        f"(GUI-side peak traced allocation {peak / 2**20:.1f} MiB), next page "
        f"{next_page:.0f} ms, status filter {failed:.0f} ms, quality filter "
        f"{suggested:.0f} ms, sort {sort:.0f} ms, search {search:.0f} ms"
    )
    for name, value in (
        ("first page", first), ("next page", next_page), ("status filter", failed),
        ("quality filter", suggested), ("sort", sort), ("search", search),
    ):
        assert value < CEILING_MS, (name, value)
    # Bounded memory: a page of 200 detached rows, not 100,000.
    assert peak < 64 * 2**20


def test_selection_survives_a_refresh_by_sheet_id(qtbot, population, widget):
    project, built = population
    loaded(qtbot, widget, lambda: widget.set_session(project.database, built.scan_session_id))
    chosen = widget.model.rows[5].scan_id
    assert widget.select_scan(chosen)
    seen: list[object] = []
    widget.sheet_selected.connect(seen.append)
    loaded(qtbot, widget, widget.refresh)
    assert widget.selected_scan_id() == chosen
    assert seen == []  # a refresh is not an operator's selection


def test_requests_while_reading_coalesce_into_one_more(qtbot, population, widget, monkeypatch):
    project, built = population
    loaded(qtbot, widget, lambda: widget.set_session(project.database, built.scan_session_id))
    calls: list[int] = []
    real = session_sheets.list_sheets

    def counted(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(session_sheets, "list_sheets", counted)
    for _ in range(10):
        widget.refresh()
    deadline = time.monotonic() + 60
    while widget.busy and time.monotonic() < deadline:
        qtbot.wait(20)
    qtbot.wait(100)
    # One running read, then exactly one owed - never ten.
    assert 1 <= len(calls) <= 2
