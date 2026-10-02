"""An exact duplicate image is never read: the real Scan stage (0.1.1 phase 4).

A sheet and a byte-identical copy under another name, then the same bytes again
in a later run of the same scan session. The real recognition engine runs; the
database and the run's own report are the oracle: the copies have no result,
are marked as duplicates of the first sheet, and the run read one sheet only.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import cv2
import pytest
from sqlalchemy import select
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.database.models import BatchScan
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.scan.page import ScanPage
from omr_scanner.services import save_template, session_population

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from omr_scanner.services import ProjectSession

pytestmark = pytest.mark.gui

TIMEOUT_MS = 120_000


def _marks(roll: str) -> dict:
    return {
        "roll_number": dict(enumerate(roll)),
        "set_code": {0: "A"},
        "questions_0": dict.fromkeys(range(10), "B"),
        "questions_1": dict.fromkeys(range(10), "C"),
    }


def rows(database) -> dict[str, tuple[str, bool]]:
    with database.session() as session:
        return {
            str(name): (str(status), bool(result))
            for name, status, result in session.execute(
                select(BatchScan.filename, BatchScan.status, BatchScan.result_json)
            ).all()
        }


def test_a_byte_identical_copy_is_linked_and_never_read(
    qtbot, project_session: ProjectSession, tmp_path: Path
) -> None:
    template = build_answer_sheet_template()
    path = project_session.project.layout.templates_dir / "sheet.omrt"
    path.parent.mkdir(parents=True, exist_ok=True)
    template_file = save_template(template, path)
    first_folder = tmp_path / "desk_a"
    first_folder.mkdir()
    original = first_folder / "a.png"
    cv2.imwrite(str(original), render_marked_sheet(template, _marks("170504")))
    copy = first_folder / "a_copy.png"
    shutil.copyfile(original, copy)

    spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
    page = ScanPage(spec)
    qtbot.addWidget(page)
    page.on_project_changed(project_session)
    assert page.load_template_from(template_file) is True
    page.add_scan_paths([original, copy])
    with qtbot.waitSignal(page.batch_finished, timeout=TIMEOUT_MS) as finished:
        assert page.process_all() is True
    report = finished.args[0]
    assert len(report.processed) == 1  # one sheet recognised, not two
    found = rows(project_session.database)
    assert found["a.png"][1] is True  # read, with a stored result
    assert found["a_copy.png"] == ("duplicate", False)
    labels = [page._scan_model.data(page._scan_model.index(row, 3)) for row in range(2)]
    assert labels[1] == "Duplicate of a.png (not read)"

    # A later run of the same session, from another folder: same bytes again.
    page.shutdown_batch()
    page.on_project_changed(None)
    page.on_project_changed(project_session)
    assert page.load_template_from(template_file) is True
    second_folder = tmp_path / "desk_b"
    second_folder.mkdir()
    late = second_folder / "late.png"
    shutil.copyfile(original, late)
    page.clear_scans()
    page.add_scan_paths([late])
    with qtbot.waitSignal(page.batch_finished, timeout=TIMEOUT_MS) as finished:
        assert page.process_all() is True
    assert len(finished.args[0].processed) == 0
    assert rows(project_session.database)["late.png"] == ("duplicate", False)
    population = session_population.population(project_session.database, page.state.batch_id)
    assert len(population.effective) == 1
    page.shutdown_batch()
    page.on_project_changed(None)
