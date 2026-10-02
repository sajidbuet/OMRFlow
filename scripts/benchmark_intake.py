"""Measure intake costs on the local disk (0.1.1 revised phase 5).

Builds a project and one watched source folder of ``--files`` small synthetic
JPEGs (counter names), then times, with the real :class:`OsFileSystem` and a
fake clock (so no quiet period is waited for):

* ``list``: one ``os.scandir`` listing of the folder (twice: the second is the
  warm-cache figure; the first is warm too in practice - the files were just
  written - so **no cold-cache figure is claimed**);
* ``reconcile_first`` (inserts every row), ``reconcile_second`` (observes
  every row again), ``reconcile_verify`` (reads, hashes and fully decodes
  every file once), ``reconcile_steady`` (nothing changed - the per-poll cost
  of a quiet folder);
* ``register`` (copy into the project, verify, commit, phase 4 duplicate
  link) in units of ``--unit``;
* ``reconcile_after_register`` (every row terminal) and ``restart`` (recovery
  plus the first reconciliation of a reopened project);
* ``duplicate_link``: phase 4's ``link_exact_duplicates`` + ledger mirror for
  one more unit of the same bytes against the registered session;
* ``hash_decode``: hash-only and hash + full decode throughput on
  ``--large`` realistic page-sized images (A4 at 300 dpi, grey);
* ``latency``: with the **real** clock, quiet ``--quiet`` s, K = 2 and a poll
  every ``--poll`` s, the time from a file's last byte to ``ready`` over
  ``--latency-files`` files.

Usage::

    python scripts/benchmark_intake.py --files 10000 --json out.json
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for entry in (REPOSITORY_ROOT / "src", REPOSITORY_ROOT):
    if str(entry) not in sys.path:  # pragma: no cover - script bootstrap
        sys.path.insert(0, str(entry))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from omr_scanner.domain.intake import Exclusions, StabilityPolicy  # noqa: E402
from omr_scanner.services import batch_store, create_project, scan_sessions  # noqa: E402
from omr_scanner.services import intake as intake_service  # noqa: E402
from omr_scanner.services.image_integrity import check_image_bytes  # noqa: E402
from omr_scanner.services.intake_fs import OsFileSystem  # noqa: E402
from omr_scanner.services.scan_provenance import hash_bytes  # noqa: E402


class Clock:
    """A fake clock for the bulk timings (stability is judged, never waited for)."""

    def __init__(self) -> None:
        from datetime import UTC, datetime

        self.now = datetime(2026, 10, 2, tzinfo=UTC)

    def __call__(self):  # noqa: ANN204 - a clock
        """The current fake time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the fake time forward."""
        from datetime import timedelta

        self.now += timedelta(seconds=seconds)


def timed(label: str, results: dict[str, Any], action: Callable[[], Any]) -> Any:
    """Run `action`, record and print its wall time."""
    started = time.perf_counter()
    value = action()
    results[label] = round(time.perf_counter() - started, 4)
    print(f"{label:28s} {results[label]:9.3f} s", flush=True)
    return value


def small_jpeg(index: int) -> bytes:
    """A small, unique JPEG (the bulk files)."""
    image = np.full((120, 90), 230, np.uint8)
    cv2.putText(image, str(index % 1000), (5, 60), 0, 0.8, 0, 2)
    return cv2.imencode(".jpg", image)[1].tobytes()


def page_jpeg(seed: int) -> bytes:
    """An A4 page at 300 dpi, grey, with marks: a realistic scan's size."""
    rng = np.random.default_rng(seed)
    image = np.full((3508, 2480), 235, np.uint8)
    for _ in range(400):
        y, x = int(rng.integers(0, 3480)), int(rng.integers(0, 2450))
        image[y : y + 25, x : x + 25] = int(rng.integers(0, 90))
    return cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()


def bulk(arguments: argparse.Namespace, results: dict[str, Any]) -> None:
    """Listing, reconciliation, registration, restart and duplicates on one folder."""
    workspace = Path(tempfile.mkdtemp(prefix="omrflow-intake-bench-"))
    folder = workspace / "Scanner A"
    folder.mkdir()
    sample = small_jpeg(0)
    for index in range(1, arguments.files + 1):
        (folder / f"{index:06d}.jpg").write_bytes(small_jpeg(index))
    results["files"] = arguments.files
    results["small_file_bytes"] = len(sample)
    project = create_project(workspace, "bench")
    database = project.database
    session_id = scan_sessions.create_scan_session(database, name="Bench").scan_session_id
    source = intake_service.create_source(
        database, label="A", root_path=str(folder),
        policy=StabilityPolicy(min_observations=2, quiet_seconds=5),
    )
    intake_service.attach_source(database, source.source_id, session_id)
    clock = Clock()
    fs = OsFileSystem()
    service = intake_service.IntakeService(database, project.root, fs=fs, clock=clock)
    from tests.conftest import build_answer_sheet_template

    identity = batch_store.BatchIdentity.of(build_answer_sheet_template())

    def listing() -> object:
        return fs.list_source(str(folder), recursive=False, exclusions=Exclusions())

    timed("list", results, listing)
    timed("list_again", results, listing)
    timed("reconcile_first", results, lambda: service.reconcile(source.source_id))
    clock.advance(1)
    timed("reconcile_second", results, lambda: service.reconcile(source.source_id))
    clock.advance(5)
    report = timed("reconcile_verify", results, lambda: service.reconcile(source.source_id))
    results["verified_ready"] = report.ready
    clock.advance(10)
    timed("reconcile_steady", results, lambda: service.reconcile(source.source_id))

    def register_all() -> int:
        count = 0
        while True:
            items = service.ready_items(
                scan_session_id=session_id, source_id=source.source_id, limit=arguments.unit
            )
            if not items:
                return count
            outcome = service.register(
                scan_session_id=session_id, source_id=source.source_id,
                intake_file_ids=[item.intake_file_id for item in items], identity=identity,
            )
            count += len(outcome.registered)

    results["registered"] = timed("register", results, register_all)
    clock.advance(10)
    timed("reconcile_after_register", results, lambda: service.reconcile(source.source_id))

    def restart() -> None:
        fresh = intake_service.IntakeService(database, project.root, fs=fs, clock=clock)
        fresh.reconcile(source.source_id)

    timed("restart", results, restart)

    copies = workspace / "Scanner B"
    copies.mkdir()
    for index in range(1, arguments.unit + 1):
        (copies / f"copy{index:06d}.jpg").write_bytes(small_jpeg(index))
    second = intake_service.create_source(
        database, label="B", root_path=str(copies),
        policy=StabilityPolicy(min_observations=1, quiet_seconds=0),
    )
    intake_service.attach_source(database, second.source_id, session_id)
    service.reconcile(second.source_id)
    items = service.ready_items(scan_session_id=session_id, source_id=second.source_id)
    outcome = timed(
        "register_duplicate_unit", results,
        lambda: service.register(
            scan_session_id=session_id, source_id=second.source_id,
            intake_file_ids=[item.intake_file_id for item in items], identity=identity,
        ),
    )
    results["duplicates_linked"] = len(outcome.duplicates)
    project.close()
    results["workspace"] = str(workspace)


def hash_decode(arguments: argparse.Namespace, results: dict[str, Any]) -> None:
    """Hash and hash + full-decode throughput on page-sized images."""
    pages = [page_jpeg(seed) for seed in range(arguments.large)]
    total = sum(len(page) for page in pages)
    results["large_files"] = len(pages)
    results["large_mean_bytes"] = round(total / len(pages))
    started = time.perf_counter()
    for page in pages:
        hash_bytes(page)
    hashing = time.perf_counter() - started
    started = time.perf_counter()
    for page in pages:
        hash_bytes(page)
        assert check_image_bytes(page).ok
    both = time.perf_counter() - started
    results["hash_mb_per_s"] = round(total / hashing / 1e6, 1)
    results["hash_decode_ms_per_page"] = round(both / len(pages) * 1000, 1)
    print(f"hash {results['hash_mb_per_s']} MB/s; hash+decode "
          f"{results['hash_decode_ms_per_page']} ms/page ({results['large_mean_bytes']} B)")


def latency(arguments: argparse.Namespace, results: dict[str, Any]) -> None:
    """Last byte written to ready, with the real clock."""
    workspace = Path(tempfile.mkdtemp(prefix="omrflow-intake-latency-"))
    folder = workspace / "scanner"
    folder.mkdir()
    project = create_project(workspace, "latency")
    database = project.database
    session_id = scan_sessions.create_scan_session(database, name="L").scan_session_id
    source = intake_service.create_source(
        database, label="L", root_path=str(folder),
        policy=StabilityPolicy(min_observations=2, quiet_seconds=arguments.quiet),
    )
    intake_service.attach_source(database, source.source_id, session_id)
    service = intake_service.IntakeService(database, project.root)
    finished: dict[str, float] = {}

    def writer() -> None:
        for index in range(arguments.latency_files):
            data = small_jpeg(index)
            path = folder / f"{index:06d}.jpg"
            with path.open("wb") as handle:
                for offset in range(0, len(data), 1024):
                    handle.write(data[offset : offset + 1024])
                    handle.flush()
                    time.sleep(0.01)
            finished[path.name] = time.monotonic()
            time.sleep(0.15)

    thread = threading.Thread(target=writer)
    thread.start()
    ready: dict[str, float] = {}
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and (
        thread.is_alive() or len(ready) < arguments.latency_files
    ):
        service.reconcile(source.source_id)
        now = time.monotonic()
        for item in service.ready_items(scan_session_id=session_id):
            ready.setdefault(item.relative_path, now)
        time.sleep(arguments.poll)
    thread.join()
    delays = sorted(ready[name] - finished[name] for name in ready if name in finished)
    results["latency_files"] = len(delays)
    results["latency_quiet_s"] = arguments.quiet
    results["latency_poll_s"] = arguments.poll
    results["latency_s"] = {
        "min": round(delays[0], 3),
        "median": round(statistics.median(delays), 3),
        "p95": round(delays[int(0.95 * (len(delays) - 1))], 3),
        "max": round(delays[-1], 3),
    }
    print("latency", results["latency_s"])
    project.close()


def main() -> int:
    """Run the measurements and print (and optionally save) the figures."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--unit", type=int, default=500)
    parser.add_argument("--large", type=int, default=20)
    parser.add_argument("--quiet", type=float, default=2.0)
    parser.add_argument("--poll", type=float, default=0.5)
    parser.add_argument("--latency-files", type=int, default=30)
    parser.add_argument("--skip-latency", action="store_true")
    parser.add_argument("--json", type=Path)
    arguments = parser.parse_args()
    results: dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "processor": platform.processor(),
        "opencv": cv2.__version__,
    }
    bulk(arguments, results)
    hash_decode(arguments, results)
    if not arguments.skip_latency:
        latency(arguments, results)
    if arguments.json:
        arguments.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
