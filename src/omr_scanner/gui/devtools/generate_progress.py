"""Watching a dataset being generated, and deciding what to do with it.

Purpose:
    Show one progress readout while sheets are written, and one summary
    afterwards that offers the two things a person actually wants next: look at
    the folder, or benchmark it.

Responsibilities:
    * :class:`GenerationProgressDialog` - bar, counts, elapsed, ETA, rate,
      cancel.
    * :class:`GenerationSummaryDialog` - what was produced, and what to do now.

What does NOT belong here:
    * Generating, benchmarking or reading a dataset. The summary emits a
      choice; the caller acts on it.

Why this looks like the Scan page's progress panel:
    Because it is the same estimator and the same formatting helpers. Two
    progress readouts in one application that disagree about what "remaining"
    means teach a user to trust neither.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.services import format_count, format_duration, format_rate

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from omr_scanner.evaluation.ground_truth import DatasetManifest
    from omr_scanner.gui.devtools.generate_worker import DatasetWorker

_LOGGER = logging.getLogger(__name__)

REFRESH_MS = 200
"""How often the readout is repainted. Five times a second - the same rate the
Scan page uses, and for the same reason: fast enough to look live, slow enough
that a thousand sheets do not cause a thousand repaints."""


class GenerationProgressDialog(QDialog):
    """Show a generation run and let it be cancelled.

    Args:
        worker: The run to watch. Already started, or started by the caller
            immediately afterwards.
        total: How many sheets were asked for.
        parent: Optional Qt parent.

    The dialog never generates anything itself: it polls the worker's snapshot
    on a timer, exactly as the Scan page polls a batch.
    """

    def __init__(
        self, worker: DatasetWorker, total: int, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("generationProgressDialog")
        self.setWindowTitle("Generating Synthetic Dataset")
        self.setModal(True)
        self.setMinimumWidth(420)

        self._worker = worker
        self._total = total
        self._cancelled = False

        layout = QVBoxLayout(self)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("generationProgressBar")
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0.0%")
        layout.addWidget(self.progress_bar)

        self.counts_label = QLabel(f"0 / {format_count(total)} sheets")
        self.counts_label.setObjectName("generationCountsLabel")
        layout.addWidget(self.counts_label)

        self.timing_label = QLabel("Elapsed --:-- · Remaining Calculating...")
        self.timing_label.setObjectName("generationTimingLabel")
        layout.addWidget(self.timing_label)

        self.rate_label = QLabel("Speed --")
        self.rate_label.setObjectName("generationRateLabel")
        layout.addWidget(self.rate_label)

        self.current_label = QLabel("Preparing...")
        self.current_label.setObjectName("generationCurrentLabel")
        layout.addWidget(self.current_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setObjectName("generationCancelButton")
        buttons.rejected.connect(self.cancel)
        layout.addWidget(buttons)

        worker.sheet_done.connect(self._on_sheet)
        worker.finished_dataset.connect(self._on_finished)
        worker.failed.connect(self._on_failed)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    @property
    def cancelled(self) -> bool:
        """Whether the user asked this run to stop."""
        return self._cancelled

    def cancel(self) -> None:
        """Stop after the sheet being drawn, and say so."""
        if self._cancelled:
            return
        self._cancelled = True
        self._worker.cancel()
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancelling...")
        self.current_label.setText("Cancelling after the current sheet...")

    def _on_sheet(self, name: str) -> None:
        """Remember the latest file. Drawing happens on the timer."""
        self._latest = name

    def _refresh(self) -> None:
        """Repaint the readout from the worker's snapshot."""
        snapshot = self._worker.progress_snapshot()
        completed = snapshot.completed
        if self.progress_bar.maximum() != max(snapshot.total, 1):
            self.progress_bar.setRange(0, max(snapshot.total, 1))
        self.progress_bar.setValue(completed)
        self.progress_bar.setFormat(f"{snapshot.percent:.1f}%")
        self.counts_label.setText(
            f"{format_count(completed)} / {format_count(snapshot.total)} sheets"
        )

        if self._cancelled:
            remaining = "Cancelling..."
        elif not snapshot.has_eta:
            remaining = "Calculating..."
        else:
            remaining = f"~{format_duration(snapshot.eta_seconds)}"
        self.timing_label.setText(
            f"Elapsed {format_duration(snapshot.elapsed_seconds)} · Remaining {remaining}"
        )
        self.rate_label.setText(f"Speed {format_rate(snapshot.rate)}")
        latest = getattr(self, "_latest", "")
        if latest and not self._cancelled:
            self.current_label.setText(f"Writing {latest}")

    def _on_finished(self, manifest: DatasetManifest) -> None:
        """Close once the run ends; the summary dialog takes over."""
        self._timer.stop()
        self._refresh()
        _LOGGER.info("Generation finished: %d sheet(s)", len(manifest.entries))
        self.accept()

    def _on_failed(self, message: str) -> None:
        """Show the failure in place rather than closing on it silently."""
        self._timer.stop()
        self.current_label.setText(f"Generation failed: {message}")
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Close")


class GenerationSummaryDialog(QDialog):
    """What was generated, and the two useful things to do with it.

    Signals:
        benchmark_requested: the user chose to benchmark this dataset now.

    Args:
        manifest: The dataset that was written.
        output_dir: Where it went.
        parent: Optional Qt parent.
    """

    benchmark_requested = Signal()

    def __init__(
        self,
        manifest: DatasetManifest,
        output_dir: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("generationSummaryDialog")
        self.setWindowTitle("Synthetic Dataset Generated")
        self.setModal(True)
        self.setMinimumWidth(460)

        self._output_dir = output_dir
        generator = manifest.generator
        cancelled = bool(generator.get("cancelled"))
        pixels = generator.get("page_pixels") or [0, 0]

        layout = QVBoxLayout(self)

        headline = QLabel(
            f"<b>{format_count(len(manifest.entries))} sheet(s)"
            f"{' (cancelled early)' if cancelled else ''}</b>"
        )
        headline.setObjectName("generationSummaryHeadline")
        layout.addWidget(headline)

        details = QLabel(
            f"Folder: {output_dir}<br>"
            f"Template: {generator.get('template_name', '-')}<br>"
            f"Profile: {generator.get('profile', '-')} · Seed: {generator.get('seed', '-')}<br>"
            f"Images: {pixels[0]} x {pixels[1]} px at {generator.get('dpi', '-')} dpi, "
            f"{str(generator.get('image_format', '')).upper()}"
        )
        details.setObjectName("generationSummaryDetails")
        details.setWordWrap(True)
        details.setTextInteractionFlags(details.textInteractionFlags())
        layout.addWidget(details)

        reproduce = QLabel(
            "Regenerate this exact dataset with the same template, profile, count "
            "and seed. The manifest records all four."
        )
        reproduce.setWordWrap(True)
        layout.addWidget(reproduce)

        caveat = QLabel(
            "Synthetic data measures regression consistency and controlled edge cases, "
            "not real-world accuracy."
        )
        caveat.setObjectName("generationSummaryCaveat")
        caveat.setWordWrap(True)
        layout.addWidget(caveat)

        row = QWidget()
        buttons = QHBoxLayout(row)
        buttons.setContentsMargins(0, 0, 0, 0)

        self.open_button = QPushButton("Open Folder")
        self.open_button.setObjectName("openDatasetFolderButton")
        self.open_button.clicked.connect(self._open_folder)
        buttons.addWidget(self.open_button)

        self.benchmark_button = QPushButton("Run Recognition Benchmark")
        self.benchmark_button.setObjectName("runBenchmarkButton")
        self.benchmark_button.clicked.connect(self._request_benchmark)
        buttons.addWidget(self.benchmark_button)

        buttons.addStretch(1)
        self.close_button = QPushButton("Close")
        self.close_button.setObjectName("closeSummaryButton")
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.close_button)
        layout.addWidget(row)

    def _open_folder(self) -> None:
        """Show the dataset in the system file manager."""
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._output_dir)))

    def _request_benchmark(self) -> None:
        """Ask the caller to benchmark this dataset, and close."""
        self.benchmark_requested.emit()
        self.accept()


__all__ = ["REFRESH_MS", "GenerationProgressDialog", "GenerationSummaryDialog"]
