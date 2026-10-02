"""Golden regression: a one-batch session reproduces the pre-phase-4 values exactly.

ROADMAP Phase C / ACCEPTANCE C9. ``tests/fixtures/golden_one_batch/golden.json``
was captured by :mod:`tests.golden_one_batch` running against ``main`` at
``0e94d67`` (before phase 4: schema 14, batch-keyed downstream). This test runs
the same scenario - same cohort, same configuration - on the current build and
compares every reconciliation row, every stored result and every cell value of
each set's Rollwise, Meritwise and Answer Key sheets. Only the Summary and
Processing Log sheets, which carry generation timestamps, are left out; the
values are compared, not the file bytes (a workbook embeds its own save time).

Regenerate only on purpose, from a pre-phase-4 checkout:
``python tests/golden_one_batch.py --repo <checkout> tests/fixtures/golden_one_batch/golden.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.golden_one_batch import capture

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden_one_batch" / "golden.json"


def test_a_one_batch_session_reproduces_the_pre_phase_4_values(tmp_path: Path) -> None:
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    actual = json.loads(json.dumps(capture(tmp_path), sort_keys=True))
    assert actual["entries"] == expected["entries"]
    assert actual["results"] == expected["results"]
    assert actual["workbooks"].keys() == expected["workbooks"].keys()
    for code, sheets in expected["workbooks"].items():
        assert actual["workbooks"][code] == sheets, code
    # The fixture is not vacuous: it holds every kind of row it was built for.
    statuses = {row["status"] for rows in expected["entries"].values() for row in rows}
    assert {"absent_confirmed", "present_without_script"} <= statuses
    assert sum(len(rows) for rows in expected["results"].values()) == 22
