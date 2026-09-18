"""GUI validation of large-batch progress, ETA display and cancellation.

Scope:
    What a person watching a ten-thousand-sheet batch sees, and what the window
    must keep doing while they watch it.

    ===== ==========================================================
    Test  Behaviour
    ===== ==========================================================
    A     The progress panel exists and renders a snapshot correctly.
    B     A real batch advances it, and finishes at exactly 100%.
    C     Updates are throttled - the widgets are not touched once
          per completed sheet.
    D     Cancellation is immediate in the interface and honest in
          the final state.
    E     Ten thousand rows create no per-job widgets and render in
          negligible time.
    ===== ==========================================================

Two kinds of test here, deliberately:
    The rendering tests feed constructed
    :class:`~omr_scanner.services.batch_progress.ProgressSnapshot` values
    straight into the panel. They are exact, instant, and can describe a
    six-thousand-sheet batch without processing one. The batch tests then run
    real recognition over a handful of sheets to prove the wiring between the
    worker thread's counters and those labels is real.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from PySide6.QtWidgets import QLabel, QProgressBar
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.scan.page import PROGRESS_REFRESH_MS, ScanEntry, ScanPage
from omr_scanner.services import save_template
from omr_scanner.services.batch_progress import (
    BatchState,
    JobStatus,
    ProgressSnapshot,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from omr_scanner.domain.template import OmrTemplate

pytestmark = pytest.mark.gui

BATCH_TIMEOUT_MS = 120_000


def sheet_marks(roll: str = "120317", *, answer: str = "B") -> dict:
    return {
        "roll_number": dict(enumerate(roll)),
        "set_code": {0: "A"},
        "questions_0": dict.fromkeys(range(10), answer),
        "questions_1": dict.fromkeys(range(10), answer),
    }


@pytest.fixture(scope="module")
def template() -> OmrTemplate:
    return build_answer_sheet_template()


@pytest.fixture
def template_path(tmp_path: Path, template: OmrTemplate) -> Path:
    path = tmp_path / "templates" / "synthetic.omrt"
    path.parent.mkdir(parents=True, exist_ok=True)
    return save_template(template, path)


@pytest.fixture
def write_sheet(tmp_path: Path, template: OmrTemplate) -> Callable[..., Path]:
    import cv2

    scans = tmp_path / "scans"
    scans.mkdir(parents=True, exist_ok=True)

    def write(name: str, roll: str = "120317") -> Path:
        path = scans / name
        cv2.imwrite(str(path), render_marked_sheet(template, sheet_marks(roll)))
        return path

    return write


@pytest.fixture
def page(qtbot) -> ScanPage:
    spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
    scan_page = ScanPage(spec)
    qtbot.addWidget(scan_page)
    return scan_page


@pytest.fixture
def loaded(page: ScanPage, template_path: Path, write_sheet) -> ScanPage:
    """A page with a template and twelve readable sheets."""
    assert page.load_template_from(template_path) is True
    page.add_scan_paths(
        [write_sheet(f"scan{index:03d}.png", roll=f"12031{index % 10}") for index in range(12)]
    )
    return page


def _collect(worker: object, estimates: list[float]) -> None:
    """Record the estimate a worker is currently offering, when it offers one."""
    eta = worker.progress_snapshot().eta_seconds
    if eta is not None:
        estimates.append(eta)


def snapshot(**kwargs: object) -> ProgressSnapshot:
    """A processing snapshot with the fields a test is not about filled in."""
    defaults: dict = {
        "state": BatchState.PROCESSING,
        "total": 10_000,
        "successful": 6_301,
        "warnings": 28,
        "failed": 13,
        "elapsed_seconds": 1_122.0,
        "rate": 5.65,
        "eta_seconds": 647.0,
        "workers": 12,
    }
    defaults.update(kwargs)
    return ProgressSnapshot(**defaults)


# ----------------------------------------------------------------------
# Test A - the panel
# ----------------------------------------------------------------------
class TestAProgressPanel:
    def test_the_panel_exposes_stable_object_names(self, page: ScanPage):
        for name in (
            "progressBar",
            "progressLabel",
            "progressCountsLabel",
            "progressTimingLabel",
            "progressRateLabel",
            "progressOutcomeLabel",
        ):
            widget = page.findChild(QProgressBar if name == "progressBar" else QLabel, name)
            assert widget is not None, name

    def test_it_renders_a_mid_batch_snapshot(self, page: ScanPage):
        page._render_progress(snapshot())

        assert page.progress_label.text() == "Processing OMR scans..."
        assert page.progress_counts_label.text() == "6,342 / 10,000 processed"
        assert "63.4%" in page.progress_bar.format()
        assert page.progress_bar.value() == 6_342
        assert page.progress_bar.maximum() == 10_000

    def test_it_shows_elapsed_and_an_estimated_remaining_time(self, page: ScanPage):
        page._render_progress(snapshot())
        text = page.progress_timing_label.text()
        assert "Elapsed 00:18:42" in text
        # A tilde, because it is an estimate and is never presented as more.
        assert "Remaining ~00:10:47" in text

    def test_it_shows_throughput_and_the_worker_count(self, page: ScanPage):
        page._render_progress(snapshot())
        text = page.progress_rate_label.text()
        assert "Speed 5.7 scans/sec" in text
        assert "12 workers" in text

    def test_it_shows_the_outcome_tally_in_words(self, page: ScanPage):
        page._render_progress(snapshot())
        text = page.progress_outcome_label.text()
        # Words and numbers, not colour alone.
        assert "Successful 6,301" in text
        assert "Review 28" in text
        assert "Failed 13" in text

    def test_during_warm_up_it_says_it_is_calculating(self, page: ScanPage):
        page._render_progress(snapshot(successful=4, warnings=0, failed=0, eta_seconds=None))
        assert "Calculating..." in page.progress_timing_label.text()
        # And nothing that could be read as a real estimate.
        assert "~" not in page.progress_timing_label.text()

    def test_an_estimated_finishing_time_appears_with_the_estimate(self, page: ScanPage):
        import time

        page._render_progress(snapshot(finish_wall_clock=time.time() + 600))
        assert "Finish ~" in page.progress_rate_label.text()

    def test_it_never_calls_a_nearly_finished_batch_finished(self, page: ScanPage):
        page._render_progress(snapshot(successful=9_999, warnings=0, failed=0))
        # 9,999 of 10,000 rounds to "100.0%" as a percentage, which is exactly
        # why the bar is driven by the counts: it is one short, and says so.
        assert page.progress_bar.value() == 9_999
        assert page.progress_bar.maximum() == 10_000
        assert page.progress_bar.value() != page.progress_bar.maximum()
        assert "9,999 / 10,000" in page.progress_counts_label.text()

    def test_cancelling_replaces_the_estimate(self, page: ScanPage):
        page._render_progress(snapshot(state=BatchState.CANCELLING))
        assert "Cancelling..." in page.progress_timing_label.text()
        assert "~00:" not in page.progress_timing_label.text()

    def test_preparing_is_shown_before_the_first_sheet(self, page: ScanPage):
        page._show_preparing(10_000)
        assert page.progress_label.text() == "Preparing batch..."
        assert page.progress_counts_label.text() == "0 / 10,000 processed"
        # Never "Remaining 00:00:00", which would read as "nearly done".
        assert "Calculating..." in page.progress_timing_label.text()


# ----------------------------------------------------------------------
# Test B - a real batch
# ----------------------------------------------------------------------
class TestBRealBatch:
    def test_progress_advances_monotonically_to_exactly_one_hundred_percent(
        self, qtbot, loaded: ScanPage
    ):
        seen: list[int] = []
        loaded.progress_bar.valueChanged.connect(seen.append)

        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS) as blocker:
            assert loaded.process_all() is True
        report = blocker.args[0]

        assert seen == sorted(seen)
        assert loaded.progress_bar.value() == loaded.progress_bar.maximum() == 12
        assert loaded.progress_bar.format() == "100.0%"
        assert report.total == 12

    def test_the_counts_reconcile_with_what_was_processed(self, qtbot, loaded: ScanPage):
        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS) as blocker:
            assert loaded.process_all() is True
        report = blocker.args[0]

        assert report.complete_count + report.review_count + report.failed_count == report.total
        text = loaded.progress_outcome_label.text()
        assert f"Successful {report.complete_count}" in text
        assert f"Review {report.review_count}" in text
        assert f"Failed {report.failed_count}" in text

    def test_a_failed_scan_still_advances_the_bar(self, qtbot, loaded: ScanPage, tmp_path: Path):
        corrupt = tmp_path / "scans" / "broken.png"
        corrupt.write_bytes(b"not an image")
        loaded.add_scan_paths([corrupt])

        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS) as blocker:
            assert loaded.process_all() is True
        report = blocker.args[0]

        # Thirteen sheets, one unreadable, and the bar still reaches the end -
        # the failure this rule exists for is a batch that stalls at 97%.
        assert report.total == 13
        assert report.failed_count == 1
        assert loaded.progress_bar.value() == loaded.progress_bar.maximum() == 13
        assert "Failed 1" in loaded.progress_outcome_label.text()

    def test_the_completion_summary_reports_duration_and_average_speed(
        self, qtbot, loaded: ScanPage
    ):
        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert loaded.process_all() is True

        assert loaded.progress_label.text() == "Batch processing complete."
        assert "12 / 12 processed" in loaded.progress_counts_label.text()
        assert "Completed in 00:00:" in loaded.progress_timing_label.text()
        assert "Remaining 00:00:00" in loaded.progress_timing_label.text()
        assert "Average" in loaded.progress_rate_label.text()

    def test_an_estimate_appears_once_the_run_has_warmed_up(
        self, qtbot, loaded: ScanPage, template: OmrTemplate
    ):
        """The worker thread's tracker really does produce an ETA mid-run.

        Driven through a real :class:`BatchWorker` with a short warm-up, so
        this exercises the actual wiring - completions counted off the GUI
        thread, snapshots read from it - rather than the estimator alone,
        which its own unit tests already cover.
        """
        from omr_scanner.gui.scan.worker import BatchWorker
        from omr_scanner.services import BatchOptions
        from omr_scanner.services.batch_progress import BatchProgressTracker

        tracker = BatchProgressTracker(warmup_jobs=3, warmup_seconds=0.2)
        worker = BatchWorker(
            [entry.path for entry in loaded.state.entries],
            template,
            BatchOptions(with_preview=False),
            workers=1,
            tracker=tracker,
        )
        estimates: list[float] = []
        events: list[None] = []

        def observe(_update: object) -> None:
            events.append(None)
            _collect(worker, estimates)

        worker.progress.connect(observe)

        with qtbot.waitSignal(worker.finished_report, timeout=BATCH_TIMEOUT_MS):
            worker.start()
        worker.wait(BATCH_TIMEOUT_MS)

        assert estimates, "no estimate was ever offered during the run"
        assert any(value > 0.0 for value in estimates), "every estimate was zero"
        assert all(value >= 0.0 for value in estimates)
        # Not from the beginning: the earliest events fall inside the warm-up
        # and offer nothing, which is what "Calculating..." is for.
        assert len(estimates) < len(events)
        # And the run ends with nothing left to wait for.
        assert tracker.snapshot().eta_seconds == 0.0

    def test_elapsed_time_is_reported_and_non_zero(self, qtbot, loaded: ScanPage):
        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert loaded.process_all() is True
        snapshot_after = loaded._last_snapshot
        assert snapshot_after.elapsed_seconds > 0.0

    def test_the_window_keeps_repainting_while_the_batch_runs(self, qtbot, loaded: ScanPage):
        # Progress signals reaching the GUI thread *are* the event loop still
        # turning: a frozen window would deliver none of them.
        ticks: list[int] = []
        loaded.progress_bar.valueChanged.connect(ticks.append)

        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert loaded.process_all() is True
            qtbot.wait(50)

        assert ticks
        assert loaded.cancel_button.isEnabled() is False  # the run is over

    def test_no_dialog_is_raised_for_a_failed_scan(
        self, qtbot, loaded: ScanPage, tmp_path: Path, monkeypatch
    ):
        """A thousand-sheet batch with fifty bad files must not be fifty modals."""
        shown: list[str] = []
        for name in ("warning", "information", "critical"):
            monkeypatch.setattr(
                f"omr_scanner.gui.scan.page.QMessageBox.{name}",
                staticmethod(lambda *args, **_kwargs: shown.append(str(args[1:2]))),
            )

        scans = tmp_path / "scans"
        for index in range(5):
            (scans / f"bad{index}.png").write_bytes(b"not an image")
        loaded.add_scan_paths([scans / f"bad{index}.png" for index in range(5)])

        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS) as blocker:
            assert loaded.process_all() is True

        assert blocker.args[0].failed_count == 5
        assert shown == []

    def test_an_empty_list_does_not_start_a_batch(self, page: ScanPage, template_path: Path):
        assert page.load_template_from(template_path) is True
        assert page.process_all() is False
        # Nothing to divide by, and nothing claimed about it.
        assert page.progress_label.text() == ""

    def test_a_single_scan_batch_completes_cleanly(
        self, qtbot, page: ScanPage, template_path: Path, write_sheet
    ):
        assert page.load_template_from(template_path) is True
        page.add_scan_paths([write_sheet("only.png")])

        with qtbot.waitSignal(page.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert page.process_all() is True

        assert page.progress_bar.value() == page.progress_bar.maximum() == 1
        assert "1 / 1 processed" in page.progress_counts_label.text()


# ----------------------------------------------------------------------
# Test C - throttling
# ----------------------------------------------------------------------
class TestCThrottling:
    def test_the_refresh_interval_is_a_few_times_a_second(self):
        # Fast enough to look live, slow enough that a machine finishing fifty
        # sheets a second does not repaint fifty times.
        assert 100 <= PROGRESS_REFRESH_MS <= 250

    def test_a_completion_event_does_not_repaint_by_itself(self, page: ScanPage):
        from omr_scanner.services import BatchProgress, BatchStage

        before = page.progress_counts_label.text()
        for index in range(50):
            page._on_progress(
                BatchProgress(
                    index,
                    50,
                    Path("scan.png"),
                    BatchStage.RECOGNISED,
                    completed_count=index + 1,
                    outcome="complete",
                )
            )
        # Fifty completions, zero repaints: the widgets are written by the
        # refresh timer, not by the event.
        assert page.progress_counts_label.text() == before

    def test_a_finished_scan_marks_its_row_without_redrawing_it(
        self, page: ScanPage, template_path: Path, write_sheet
    ):
        from omr_scanner.services import ProcessedScan, RecognitionOutcome, RegistrationStatus
        from omr_scanner.services.recognition_models import ScanResult

        assert page.load_template_from(template_path) is True
        path = write_sheet("a.png")
        page.add_scan_paths([path])

        processed = ProcessedScan(
            result=ScanResult(
                source_path=path,
                outcome=RecognitionOutcome.COMPLETE,
                registration=RegistrationStatus.REGISTERED,
            )
        )
        page._on_scan_done(processed)

        assert page._dirty_rows == {0}
        assert page.scan_table.item(0, 3).text() == "Pending"  # not yet drawn
        page._flush_dirty_rows()
        assert page.scan_table.item(0, 3).text() == "Complete"
        assert page._dirty_rows == set()

    def test_rows_are_found_by_index_not_by_searching(self, loaded: ScanPage):
        # O(1) per completion. The linear search this replaces made a
        # ten-thousand-sheet batch quadratic in the GUI thread.
        assert len(loaded._row_by_path) == len(loaded.state.entries)
        for row, entry in enumerate(loaded.state.entries):
            assert loaded._row_by_path[entry.path] == row


# ----------------------------------------------------------------------
# Test D - cancellation
# ----------------------------------------------------------------------
class TestDCancellation:
    def test_cancel_is_offered_only_while_a_run_is_going(self, loaded: ScanPage):
        assert loaded.cancel_button.isEnabled() is False

    def test_cancelling_disables_the_button_and_says_so_immediately(
        self, qtbot, loaded: ScanPage
    ):
        assert loaded.process_all() is True
        assert loaded.cancel_button.isEnabled() is True

        loaded.cancel_processing()

        # Immediately, before any worker has noticed: a live button invites a
        # second click that has nothing to do.
        assert loaded.cancel_button.isEnabled() is False
        assert loaded.cancel_button.text() == "Cancelling..."
        assert loaded.progress_label.text() == "Cancelling batch processing..."

        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS):
            pass

    def test_a_cancelled_batch_reports_what_it_did_and_did_not_do(
        self, qtbot, loaded: ScanPage
    ):
        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS) as blocker:
            assert loaded.process_all() is True
            loaded.cancel_processing()
        report = blocker.args[0]

        assert report.cancelled is True
        assert loaded.progress_label.text() == "Batch cancelled."
        counts = loaded.progress_counts_label.text()
        assert "processed" in counts
        assert "not processed" in counts
        assert "Stopped after" in loaded.progress_timing_label.text()

    def test_the_results_already_produced_are_kept(self, qtbot, loaded: ScanPage):
        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS) as blocker:
            assert loaded.process_all() is True
            loaded.cancel_processing()
        report = blocker.args[0]

        processed = [entry for entry in loaded.state.entries if entry.processed is not None]
        assert len(processed) == report.total

    def test_the_controls_come_back_after_a_cancellation(self, qtbot, loaded: ScanPage):
        with qtbot.waitSignal(loaded.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert loaded.process_all() is True
            loaded.cancel_processing()

        assert loaded.process_all_button.isEnabled() is True
        assert loaded.cancel_button.isEnabled() is False
        assert loaded.cancel_button.text() == "Cancel Processing"


# ----------------------------------------------------------------------
# Test E - ten thousand sheets
# ----------------------------------------------------------------------
class TestETenThousandScans:
    def test_the_interface_creates_no_widget_per_scan(self, page: ScanPage, tmp_path: Path):
        page.state.entries.extend(
            ScanEntry(path=tmp_path / f"scan{index:05d}.png") for index in range(10_000)
        )
        page._rebuild_scan_table()

        assert page.scan_table.rowCount() == 10_000
        # One bar for the batch, not one per sheet - the difference between an
        # interface that scales and one that stops responding at a thousand.
        assert len(page.findChildren(QProgressBar)) == 1

    def test_rendering_a_ten_thousand_sheet_snapshot_is_instant(self, page: ScanPage):
        import time

        started = time.perf_counter()
        for completed in range(0, 10_000, 100):
            page._render_progress(
                snapshot(successful=completed, warnings=0, failed=0, eta_seconds=12.0)
            )
        elapsed = time.perf_counter() - started

        # A hundred repaints of the whole panel; at five a second that is
        # twenty seconds of a real run, and it must not cost measurable time.
        assert elapsed < 1.0
        assert page.progress_counts_label.text() == "9,900 / 10,000 processed"

    def test_the_row_index_stays_proportional_not_quadratic(
        self, page: ScanPage, tmp_path: Path
    ):
        import time

        page.state.entries.extend(
            ScanEntry(path=tmp_path / f"scan{index:05d}.png") for index in range(10_000)
        )
        page._rebuild_scan_table()

        started = time.perf_counter()
        for entry in page.state.entries:
            assert page._row_by_path[entry.path] is not None
        assert time.perf_counter() - started < 0.5

    def test_a_ten_thousand_job_tracker_drives_the_panel_to_exactly_full(
        self, page: ScanPage
    ):
        from omr_scanner.services.batch_progress import BatchProgressTracker

        tracker = BatchProgressTracker()
        tracker.start(10_000, workers=8)
        for index in range(10_000):
            tracker.record(JobStatus.FAILED if index % 500 == 0 else JobStatus.SUCCESS)
        tracker.finish()

        page._render_progress(tracker.snapshot())
        assert page.progress_bar.value() == 10_000
        assert page.progress_bar.maximum() == 10_000
        assert page.progress_bar.format() == "100.0%"
        assert "10,000 / 10,000 processed" in page.progress_counts_label.text()
        assert "Failed 20" in page.progress_outcome_label.text()
