"""Measuring how well recognition actually reads sheets.

Purpose:
    Answer "is the recognition engine any good, and did this change make it
    better or worse?" with numbers, against data whose correct answers are
    known - synthetic today, a real validated corpus when one exists.

Modules:
    * ``ground_truth.py``     - what a sheet *should* read, and the manifest
      that describes a dataset of them. One schema for synthetic and real data
      alike, so the same evaluator serves both.
    * ``synthetic_dataset.py`` - generate a reproducible dataset of sheets with
      controlled defects, from a real ``.omrt`` template, with ground truth
      written beside every image.
    * ``benchmark.py``        - compare results against ground truth, classify
      every disagreement, and write a machine-readable report; plus the
      baseline comparison that says whether a change improved things.

What does NOT belong here:
    * Qt, and anything a user runs. This is a development and QA layer: the
      tools in :mod:`omr_scanner.tools` drive it, and the tests use it.
    * Recognition itself. This package *judges* the engine; it must never
      become a second implementation of it, or a benchmark would be measuring
      itself.
    * Changing production defaults. A threshold sweep here reports what it
      found; a human decides whether synthetic evidence justifies changing what
      the application ships with. Synthetic images are not real scans.

Why it is a separate layer:
    Everything below ``services`` is engine; this sits *above* it, consuming
    the same public :class:`~omr_scanner.services.recognition_models.ScanResult`
    that Phase 4 will consume. If a benchmark ever needs a private detail of
    recognition to do its job, that detail belongs on the result instead - and
    that pressure is exactly why the boundary is worth having.
"""

from omr_scanner.evaluation.benchmark import (
    BenchmarkReport,
    BenchmarkSummary,
    ErrorCategory,
    ErrorRecord,
    MetricComparison,
    compare_baseline,
    compare_result,
    evaluate,
    write_report,
)
from omr_scanner.evaluation.ground_truth import (
    GROUND_TRUTH_SCHEMA_VERSION,
    DatasetManifest,
    SheetGroundTruth,
    load_ground_truth,
    load_manifest,
    save_ground_truth,
    save_manifest,
)

__all__ = [
    "GROUND_TRUTH_SCHEMA_VERSION",
    "BenchmarkReport",
    "BenchmarkSummary",
    "DatasetManifest",
    "ErrorCategory",
    "ErrorRecord",
    "MetricComparison",
    "SheetGroundTruth",
    "compare_baseline",
    "compare_result",
    "evaluate",
    "load_ground_truth",
    "load_manifest",
    "save_ground_truth",
    "save_manifest",
    "write_report",
]
