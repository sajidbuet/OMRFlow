"""Progress events and time estimates for the quality gate.

An opt-in pytest plugin for ``pytest-ruff-mypy.ps1``::

    python -m pytest -p tools.gate_progress \
        --gate-progress-file=PATH [--gate-durations-file=PATH]

Without ``--gate-progress-file`` it does nothing. It never writes to the
console, so pytest's own output - and the gate's ``pytest.log`` - stay
exactly as they are.

The progress file gets one JSON object per line, flushed as it is written:

* ``{"event": "collected", "total", "known", "expected_s"}`` - once, after
  deselection; ``known`` is how many of the tests have a recorded duration.
* ``{"event": "start", "nodeid"}`` - before each test's setup.
* ``{"event": "finish", "nodeid", "outcome", "duration", "done", "failed",
  "elapsed", "remaining_s", "basis"}`` - after each test's teardown.
* ``{"event": "finished", "exitstatus", "elapsed"}`` - at session end.

``remaining_s`` is an estimate, not a measurement. With a durations file
from an earlier run (``basis`` ``"previous-run"``) it is the remaining
tests' recorded durations, scaled by how fast this run is going against
those same recordings; a test with no recording counts as the median
recorded test. Without one (``basis`` ``"average"``) it is this run's mean
time per test times the tests left. Test durations here differ by orders of
magnitude (crash and GUI tests run first and take most of the time), so the
first kind is far better than a count-based guess.

The durations file is rewritten at session end, merged with what it already
held, so a run that is killed loses only its own new timings. Neither file
is read by anything but the gate.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import IO, Any

import pytest

_DURATIONS_VERSION = 1


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the gate's two file options."""
    group = parser.getgroup("gate-progress", "quality-gate progress events")
    group.addoption(
        "--gate-progress-file",
        default=None,
        help="Write one JSON progress event per line to this file.",
    )
    group.addoption(
        "--gate-durations-file",
        default=None,
        help="Per-test durations: read to estimate the time left, rewritten at the end.",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Enable the reporter only when the gate asked for it."""
    progress_file = config.getoption("--gate-progress-file")
    if not progress_file:
        return
    durations_file = config.getoption("--gate-durations-file")
    config.pluginmanager.register(
        GateProgress(Path(progress_file), Path(durations_file) if durations_file else None),
        "gate-progress-reporter",
    )


def _read_durations(path: Path | None) -> dict[str, float]:
    """Return the recorded per-test durations, or none if unreadable."""
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != _DURATIONS_VERSION:
        return {}
    durations = data.get("durations")
    if not isinstance(durations, dict):
        return {}
    return {
        str(nodeid): float(seconds)
        for nodeid, seconds in durations.items()
        if isinstance(seconds, int | float) and seconds >= 0
    }


class GateProgress:
    """Writes the progress events and keeps the durations file.

    Args:
        progress_path: The JSON-lines file the gate polls.
        durations_path: Recorded durations, read now and rewritten at the end;
            ``None`` estimates from this run alone and records nothing.
    """

    def __init__(self, progress_path: Path, durations_path: Path | None) -> None:
        self._durations_path = durations_path
        self._history = _read_durations(durations_path)
        self._measured: dict[str, float] = {}
        self._expected: dict[str, float] = {}
        self._known = 0
        self._total = 0
        self._done = 0
        self._failed = 0
        self._expected_done = 0.0
        self._expected_total = 0.0
        self._session_start = time.monotonic()
        self._first_start: float | None = None
        self._test_start: dict[str, float] = {}
        self._test_failed: dict[str, bool] = {}
        self._test_skipped: dict[str, bool] = {}
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        self._out: IO[str] = progress_path.open("w", encoding="utf-8", newline="\n")

    def _emit(self, **event: Any) -> None:
        self._out.write(json.dumps(event) + "\n")
        self._out.flush()

    def _elapsed(self) -> float:
        return round(time.monotonic() - self._session_start, 3)

    @pytest.hookimpl(trylast=True)
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        """Fix the expected duration of every selected test."""
        recorded = [
            self._history[item.nodeid] for item in session.items if item.nodeid in self._history
        ]
        self._known = len(recorded)
        fallback = statistics.median(recorded) if recorded else 0.0
        self._expected = {
            item.nodeid: self._history.get(item.nodeid, fallback) for item in session.items
        }
        self._total = len(session.items)
        self._expected_total = sum(self._expected.values())
        self._emit(
            event="collected",
            total=self._total,
            known=self._known,
            expected_s=round(self._expected_total, 1) if self._known else None,
        )

    def pytest_runtest_logstart(self, nodeid: str) -> None:
        """Report the test that is about to run."""
        now = time.monotonic()
        if self._first_start is None:
            self._first_start = now
        self._test_start[nodeid] = now
        self._test_failed[nodeid] = False
        self._test_skipped[nodeid] = False
        self._emit(event="start", nodeid=nodeid)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Fold one phase's outcome into the test's outcome."""
        if report.failed:
            self._test_failed[report.nodeid] = True
        elif report.skipped:
            self._test_skipped[report.nodeid] = True

    def pytest_runtest_logfinish(self, nodeid: str) -> None:
        """Report the finished test and the time estimated to be left."""
        now = time.monotonic()
        duration = now - self._test_start.pop(nodeid, now)
        self._measured[nodeid] = duration
        failed = self._test_failed.pop(nodeid, False)
        skipped = self._test_skipped.pop(nodeid, False)
        outcome = "failed" if failed else "skipped" if skipped else "passed"
        self._done += 1
        self._failed += int(failed)
        self._expected_done += self._expected.get(nodeid, 0.0)
        self._emit(
            event="finish",
            nodeid=nodeid,
            outcome=outcome,
            duration=round(duration, 3),
            done=self._done,
            failed=self._failed,
            elapsed=self._elapsed(),
            remaining_s=self._remaining(now),
            basis="previous-run" if self._known else "average",
        )

    def _remaining(self, now: float) -> float | None:
        if self._first_start is None or self._done == 0:
            return None
        running = now - self._first_start
        left = max(0, self._total - self._done)
        if self._known and self._expected_done > 0:
            pace = running / self._expected_done
            seconds = max(0.0, self._expected_total - self._expected_done) * pace
        else:
            seconds = running / self._done * left
        return round(seconds, 1)

    def pytest_sessionfinish(self, exitstatus: int) -> None:
        """Close the event stream and record this run's durations."""
        self._emit(event="finished", exitstatus=int(exitstatus), elapsed=self._elapsed())
        self._out.close()
        if self._durations_path is None or not self._measured:
            return
        merged = {**self._history, **self._measured}
        payload = {
            "version": _DURATIONS_VERSION,
            "durations": {nodeid: round(seconds, 3) for nodeid, seconds in merged.items()},
        }
        temporary = self._durations_path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=0), encoding="utf-8")
            temporary.replace(self._durations_path)
        except OSError:
            # A missing timing record only weakens the next estimate.
            temporary.unlink(missing_ok=True)
