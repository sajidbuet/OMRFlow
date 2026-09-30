"""Rendered acceptance run: the Results stage's Dashboard tab in the real window.

Scenario (every identifier fictional):
    Three sets (10, 11, 12) of one 100-question examination, 60, 75 and 90
    registered candidates with every fifteenth absent, a different verified
    key per set, and eight deliberately designed questions (see
    ``tests/analytics_fixtures.py``). Scripts are stored recognition results,
    reconciled and scored through the real services; the window is the real
    ``MainWindow`` on the platform's own display.

What it does:
    Opens the project, goes to Results, checks the Results tab still works,
    opens the Dashboard, and for Overall Exam and each set compares the
    figures with the Results tab's own rows (computed independently here),
    scrolls through every panel and screenshots it at 1366x768 and 1100x680,
    and captures a real hover tooltip. Screenshots and a JSON summary of every
    check go to ``test-output/gui/results_dashboard[_<scale>]/``. Exits
    non-zero if any check fails.

Run from the repository root::

    .venv/Scripts/python scripts/acceptance_results_dashboard.py

To look at another display scaling on the same machine, set
``QT_SCALE_FACTOR`` (it multiplies the Windows scaling) and pass a label::

    $env:QT_SCALE_FACTOR = "0.715"; .venv/Scripts/python scripts/acceptance_results_dashboard.py 125
"""

from __future__ import annotations

import json
import statistics
import sys
import uuid
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

LABEL = sys.argv[1] if len(sys.argv) > 1 else ""
OUT = REPOSITORY_ROOT / "test-output" / "gui" / (
    "results_dashboard" + (f"_{LABEL}" if LABEL else "")
)
SIZES = ((1366, 768), (1100, 680))

checks: list[dict[str, object]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record and print one acceptance check."""
    checks.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail else ""))


def main() -> int:  # noqa: D103 - the scenario is the module docstring
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    from tests.analytics_fixtures import OPERATOR, build_dashboard_exam

    from omr_scanner.config import AppConfig
    from omr_scanner.gui.main_window import MainWindow
    from omr_scanner.services import create_project

    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv)
    workspace = REPOSITORY_ROOT / "test-output" / "projects" / uuid.uuid4().hex[:8]
    workspace.mkdir(parents=True)
    session = create_project(workspace, "Results Dashboard Acceptance")
    root = session.root
    exam = build_dashboard_exam(session, workspace / "inputs")
    session.close()
    print(f"Project: {root}")

    def pump(ms: int = 150) -> None:
        end = QTimer()
        end.setSingleShot(True)
        end.start(ms)
        while end.isActive():
            app.processEvents()

    window = MainWindow(AppConfig(reviewer_name=OPERATOR), config_path=workspace / "config.json")
    window.show()
    check("project opens", bool(window.open_project_at(root)))
    pump(300)
    window.show_page("results")
    page = window._results_page()
    assert page is not None
    page.set_batch(exam.batch_id)
    pump(300)
    ratio = window.devicePixelRatioF()
    check("display scaling recorded", True, f"devicePixelRatio={ratio:.2f}")

    # ---- The Results tab, as before ----
    check("Results tab is the default", page.tabs.currentIndex() == 0)
    scored_rows = [item for item in page.state.every_result if item.has_mark]
    check(
        "Results table lists every candidate",
        page.table.rowCount() == sum(len(d.rolls) for d in exam.sets.values()),
        f"{page.table.rowCount()} rows",
    )
    check("Calculate Results enabled", page.score_button.isEnabled())
    for width, height in SIZES:
        window.resize(width, height)
        pump(250)
        window.grab().save(str(OUT / f"00_results_tab_{width}x{height}.png"))

    done: list[int] = []
    page.scored.connect(lambda: done.append(1))
    page.score_batch()
    for _ in range(200):
        if done:
            break
        pump(100)
    check("Recalculating from the Results tab still works", bool(done))
    scored_rows = [item for item in page.state.every_result if item.has_mark]

    # ---- The Dashboard ----
    board = page.dashboard
    computed: list[int] = []
    board.computed.connect(lambda: computed.append(1))
    page.tabs.setCurrentWidget(board)
    for _ in range(200):
        if computed:
            break
        pump(100)
    check("Dashboard computes when opened", bool(computed))
    check("Default scope is Overall Exam", board.scope_combo.currentData() is None)
    offered = [board.scope_combo.itemData(i) for i in range(board.scope_combo.count())]
    check("Every set is offered", offered == [None, "10", "11", "12"], str(offered))

    def expect(scope_key: str | None) -> list[float]:
        return [
            float(item.final_score)
            for item in scored_rows
            if item.final_score is not None and (scope_key is None or item.set_code == scope_key)
        ]

    def shoot_scope(tag: str) -> None:
        bar = board.scroll_area.verticalScrollBar()
        for width, height in SIZES:
            window.resize(width, height)
            pump(300)
            step = max(1, board.scroll_area.viewport().height() - 40)
            position, index = 0, 0
            while True:
                bar.setValue(position)
                pump(120)
                window.grab().save(str(OUT / f"{tag}_{width}x{height}_{index:02d}.png"))
                if position >= bar.maximum():
                    break
                position = min(bar.maximum(), position + step)
                index += 1
            bar.setValue(0)
        window.resize(*SIZES[0])
        pump(200)

    for number, key in enumerate([None, "10", "11", "12"], start=1):
        board.select_scope(key)
        pump(200)
        marks = expect(key)
        label = "overall" if key is None else f"set{key}"
        cards = {name: card.value.text() for name, card in board.kpi_cards.items()}
        check(f"{label}: scripts reconcile with the Results rows",
              cards["count"] == str(len(marks)), f"{cards['count']} vs {len(marks)}")
        check(f"{label}: mean", cards["mean"] == f"{statistics.fmean(marks):.2f}".rstrip("0")
              .rstrip("."), f"{cards['mean']} vs {statistics.fmean(marks):.4f}")
        check(f"{label}: median", float(cards["median"]) == round(statistics.median(marks), 2))
        check(f"{label}: sample SD",
              abs(float(cards["sd"]) - statistics.stdev(marks)) < 0.006,
              f"{cards['sd']} vs {statistics.stdev(marks):.4f}")
        check(f"{label}: min/max",
              (float(cards["min"]), float(cards["max"])) == (min(marks), max(marks)))
        check(f"{label}: histogram counts every script",
              sum(item.count for item in board.histogram.bars) == len(marks))
        if key is not None:
            check(f"{label}: 100 question bars", board.correct_chart.item_count() == 100)
            check(f"{label}: set's own key shown for Q1",
                  f"Correct answer: <b>{exam.sets[key].key[0]}</b>" in (
                      board.option_label.text() if board.select_question(1) else ""))
            flagged = {
                board.flags_table.item(r, 0).text() for r in range(board.flags_table.rowCount())
            }
            wanted = exam.designed["negative_discrimination"]
            check(f"{label}: designed negative item flagged", f"Q{wanted}" in flagged)
            check(f"{label}: high-blank item flagged",
                  f"Q{exam.designed['high_blank']}" in flagged)
        else:
            check("overall: question analysis stays per set",
                  not board.question_panels.isVisible()
                  and "per set" in board.question_note.text())
            check("overall: set comparison shown", board.comparison_box.isVisible())
        shoot_scope(f"{number:02d}_{label}")

    # ---- A real hover tooltip ----
    board.select_scope("11")
    board.select_question(exam.designed["negative_discrimination"])
    window.resize(*SIZES[0])
    pump(250)
    chart = board.discrimination_chart
    board.scroll_area.ensureWidgetVisible(chart, 0, 40)
    pump(200)
    plot = chart.plot_rect()
    slot = plot.width() / chart.item_count()
    target = exam.designed["negative_discrimination"] - 1
    point = QPoint(int(plot.left() + slot * (target + 0.5)), int(plot.center().y()))
    # A real move event: QTest.mouseMove does not reliably produce a
    # button-less move on Windows.
    move = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(point), QPointF(chart.mapToGlobal(point)),
        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(chart, move)
    pump(600)
    shown = next(
        (w for w in app.topLevelWidgets() if w.isVisible() and w.inherits("QTipLabel")), None
    )
    check("hover tooltip appears", shown is not None)
    window.grab().save(str(OUT / "90_hover_discrimination.png"))
    if shown is not None:
        shown.grab().save(str(OUT / "90_hover_tooltip_only.png"))
        text = getattr(shown, "text", lambda: "")()
        check("tooltip names the question and its values",
              f"Question {target + 1}" in text and "Discrimination" in text, text[:120])

    window.close()
    pump(300)
    (OUT / "summary.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
    failed = [item for item in checks if not item["ok"]]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed; screenshots in {OUT}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
