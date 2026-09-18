"""Holding one benchmark run together: a dataset, its truth, and its report.

Purpose:
    Everything the *user interface* needs to benchmark a dataset, with no Qt in
    it - so the Scan page's benchmark mode and the command-line tool do the
    same arithmetic, write the same files and compare against the same
    baseline.

Responsibilities:
    * :class:`BenchmarkSession` - open a dataset folder, score a set of
      results, write the report, compare with the previous run.

What does NOT belong here:
    * Recognition or batching. A session is handed results that the one batch
      architecture produced; it never runs a second pipeline of its own.
    * Widgets, threads or progress. Those belong to whoever is driving.

Why the previous run is the baseline:
    A benchmark answers "did this change anything", and the useful comparison
    is nearly always against the last run on the same data. Keeping it in the
    dataset folder means the comparison is there without anybody having to
    remember to save a baseline first - the mistake that makes regression
    testing theoretical.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from omr_scanner.evaluation.benchmark import (
    SUMMARY_FILENAME,
    BenchmarkRunConfig,
    compare_baseline,
    compare_categories,
    evaluate,
    load_categories,
    load_summary,
    write_report,
)
from omr_scanner.evaluation.ground_truth import (
    load_ground_truth_directory,
    load_manifest,
)
from omr_scanner.evaluation.synthetic_dataset import (
    GROUND_TRUTH_DIRNAME,
    IMAGES_DIRNAME,
    MANIFEST_FILENAME,
)
from omr_scanner.services.recognition_models import utc_timestamp

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence
    from typing import Any

    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.evaluation.benchmark import BenchmarkReport, MetricComparison
    from omr_scanner.evaluation.ground_truth import SheetGroundTruth
    from omr_scanner.services.recognition_models import ScanResult

_LOGGER = logging.getLogger(__name__)

REPORT_DIRNAME = "benchmark_report"
"""Where a session writes its report, inside the dataset folder.

Beside the data it scored, on purpose: a report in a temporary folder
somewhere else is a report nobody finds again."""

PREVIOUS_DIRNAME = "previous"
"""The run before this one, kept inside the report folder.

One generation of history. Enough to answer "did that change help", and not so
much that a dataset folder slowly fills with reports nobody reads."""


@dataclass(frozen=True, slots=True)
class BenchmarkComparison:
    """How this run differs from the one before it.

    Attributes:
        metrics: One entry per headline metric.
        categories: One entry per test-case category that moved.
        baseline_dataset: Which dataset the baseline was measured on, so a
            comparison against a *different* dataset can be spotted and
            disregarded.
    """

    metrics: tuple[MetricComparison, ...] = ()
    categories: tuple[MetricComparison, ...] = ()
    baseline_dataset: str = ""

    @property
    def regressions(self) -> tuple[MetricComparison, ...]:
        """Everything that got worse, headline metrics first."""
        return tuple(
            item
            for item in (*self.metrics, *self.categories)
            if item.verdict == "regressed"
        )

    @property
    def has_baseline(self) -> bool:
        """Whether there was a previous run to compare against at all."""
        return bool(self.metrics)


@dataclass(frozen=True, slots=True)
class BenchmarkSession:
    """One dataset, opened and ready to be scored.

    Attributes:
        dataset_dir: The dataset folder.
        images_dir: Where its scans are.
        truths: Ground truth, keyed by scan stem.
        name: The dataset's name from its manifest, or its folder name.
        generator: The manifest's generator record - seed, profile, DPI - kept
            so the run configuration can record what it scored.
        template_path: The template the manifest says the dataset was made
            from, when it recorded one.
    """

    dataset_dir: Path
    images_dir: Path
    truths: dict[str, SheetGroundTruth]
    name: str = ""
    generator: dict[str, Any] = field(default_factory=dict)
    template_path: str = ""

    @property
    def report_dir(self) -> Path:
        """Where this session's report is written."""
        return self.dataset_dir / REPORT_DIRNAME

    @property
    def sheet_count(self) -> int:
        """How many labelled sheets this dataset contains."""
        return len(self.truths)

    @classmethod
    def open(cls, dataset_dir: Path) -> BenchmarkSession:
        """Open a dataset folder for benchmarking.

        Args:
            dataset_dir: A folder containing ``images/`` and ``ground_truth/``.
                A folder that *is* the images folder, with ``ground_truth/``
                beside it, is accepted too - that is what a user who navigated
                one level too deep will select.

        Returns:
            The session.

        Raises:
            FileNotFoundError: There is no ground truth to score against.
                Deliberately fatal: benchmarking without labels would silently
                measure nothing.
        """
        root = dataset_dir
        if (root / GROUND_TRUTH_DIRNAME).is_dir() is False and root.name == IMAGES_DIRNAME:
            root = root.parent

        truth_dir = root / GROUND_TRUTH_DIRNAME
        if not truth_dir.is_dir():
            raise FileNotFoundError(
                f"No '{GROUND_TRUTH_DIRNAME}' folder in {root}; "
                "a benchmark needs a labelled dataset."
            )
        truths = load_ground_truth_directory(truth_dir)
        if not truths:
            raise FileNotFoundError(f"No ground-truth files in {truth_dir}")

        images = root / IMAGES_DIRNAME
        name = root.name
        generator: dict[str, Any] = {}
        template_path = ""
        manifest_path = root / MANIFEST_FILENAME
        if manifest_path.is_file():
            try:
                manifest = load_manifest(manifest_path)
            except (OSError, ValueError):
                # A damaged manifest costs the dataset its name, not its
                # usefulness: the labels are what the benchmark actually needs.
                _LOGGER.warning("Could not read the manifest at %s", manifest_path)
            else:
                name = manifest.name or name
                generator = dict(manifest.generator)
                template_path = manifest.template

        return cls(
            dataset_dir=root,
            images_dir=images if images.is_dir() else root,
            truths=truths,
            name=name,
            generator=generator,
            template_path=template_path,
        )

    def score(
        self,
        results: Sequence[ScanResult],
        template: OmrTemplate | None = None,
        *,
        worker_count: int = 0,
        started_at: str = "",
        write: bool = True,
    ) -> tuple[BenchmarkReport, BenchmarkComparison]:
        """Score a set of results against this dataset and write the report.

        Args:
            results: What the engine produced, in any order.
            template: The template it read with, recorded in the run
                configuration.
            worker_count: How many processes the batch used.
            started_at: When the run started, ISO-8601 UTC.
            write: Write the report files. ``False`` scores without touching
                the disk, which is what a test wants.

        Returns:
            The report, and how it compares with the previous run - an empty
            comparison when there was not one.
        """
        baseline = self._load_baseline()
        report = evaluate(
            results,
            self.truths,
            dataset=self.name,
            dataset_path=str(self.dataset_dir),
        ).with_config(
            BenchmarkRunConfig(
                dataset=self.name,
                dataset_path=str(self.dataset_dir),
                template=template.name if template is not None else "",
                template_path=self.template_path,
                engine_name=results[0].engine_name if results else "",
                engine_version=results[0].engine_version if results else "",
                worker_count=worker_count,
                settings=(
                    template.recognition.model_dump(mode="json")
                    if template is not None
                    else {}
                ),
                generator=self.generator,
                started_at=started_at or utc_timestamp(),
            )
        )

        comparison = BenchmarkComparison()
        if baseline is not None:
            summary, categories = baseline
            comparison = BenchmarkComparison(
                metrics=compare_baseline(summary, report.summary),
                categories=tuple(
                    item
                    for item in compare_categories(categories, report.categories)
                    if item.verdict != "unchanged"
                ),
                baseline_dataset=summary.dataset,
            )

        if write:
            self._rotate_previous()
            write_report(report, self.report_dir)
            _LOGGER.info(
                "Benchmark scored %d scan(s) of '%s'; report in %s",
                report.summary.scans,
                self.name,
                self.report_dir,
            )
        return report, comparison

    def _load_baseline(self) -> tuple[Any, Any] | None:
        """Read the previous run's report, when there is one."""
        path = self.report_dir / SUMMARY_FILENAME
        if not path.is_file():
            return None
        try:
            return load_summary(path), load_categories(path)
        except (OSError, ValueError):
            _LOGGER.warning("Could not read the previous report at %s", path)
            return None

    def _rotate_previous(self) -> None:
        """Move the last report into ``previous/`` before writing a new one.

        Copied rather than deleted so that a comparison can still be made by
        hand afterwards, and only one generation is kept so the folder does not
        grow without bound.
        """
        current = self.report_dir / SUMMARY_FILENAME
        if not current.is_file():
            return
        previous = self.report_dir / PREVIOUS_DIRNAME
        previous.mkdir(parents=True, exist_ok=True)
        for path in self.report_dir.glob("*.*"):
            if path.is_file():
                shutil.copy2(path, previous / path.name)


__all__ = [
    "PREVIOUS_DIRNAME",
    "REPORT_DIRNAME",
    "BenchmarkComparison",
    "BenchmarkSession",
]
