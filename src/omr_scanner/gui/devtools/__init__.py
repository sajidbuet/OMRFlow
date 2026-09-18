"""Developer and testing tools, as dialogs rather than command lines.

Purpose:
    Put the two things a developer needs while working on recognition - a
    labelled dataset to test against, and a score for what the engine did with
    it - inside the application, so that neither requires remembering a
    command.

Responsibilities:
    * :mod:`~omr_scanner.gui.devtools.generate_dialog` - what to generate.
    * :mod:`~omr_scanner.gui.devtools.generate_worker` - generating it without
      freezing the window.
    * :mod:`~omr_scanner.gui.devtools.generate_progress` - watching it happen,
      and what to do next.
    * :mod:`~omr_scanner.gui.devtools.benchmark_dialog` - reading the score.

What does NOT belong here:
    * Generating or scoring anything. Both live in
      :mod:`omr_scanner.evaluation`, and these dialogs are a front end to them -
      so that the command-line tools and the menu items cannot drift apart.
    * A second scanning screen. Benchmarking runs in the existing Scan page,
      in benchmark mode, on the one batch architecture the application has.

Who this is for:
    Developers and testers. These tools are visible in a normal build on
    purpose - a testing tool nobody can reach is a testing tool nobody uses -
    but everything they produce is labelled synthetic, and none of it is
    evidence about real-world accuracy.
"""

from __future__ import annotations

from omr_scanner.gui.devtools.benchmark_dialog import BenchmarkResultsDialog
from omr_scanner.gui.devtools.generate_dialog import (
    GenerateDatasetDialog,
    GenerationRequest,
)
from omr_scanner.gui.devtools.generate_progress import (
    GenerationProgressDialog,
    GenerationSummaryDialog,
)
from omr_scanner.gui.devtools.generate_worker import DatasetWorker

__all__ = [
    "BenchmarkResultsDialog",
    "DatasetWorker",
    "GenerateDatasetDialog",
    "GenerationProgressDialog",
    "GenerationRequest",
    "GenerationSummaryDialog",
]
