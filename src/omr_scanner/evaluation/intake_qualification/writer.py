"""One simulated scanner: a separate writer process (revised phase 9).

Run by the supervisor as its own operating-system process - one per source -
never imported by OMRFlow. It knows nothing about OMRFlow: it copies image
bytes from the campaign's pool into its scanner folder on a schedule, writing
each file the way its plan says (in one go, in steps, header first, held open
while growing, with a pause longer than the stability quiet period, or under
a temporary ``.part`` name renamed into place). The only thing OMRFlow can see
is the folder.

Every file's ``write_started`` is logged before its first byte and its
``write_completed`` - with the final size and SHA-256 - after its last byte and
close (or rename), to the writer's own append-only, fsynced log. That log is
the external evidence ``no_incomplete_file_processed`` compares submission
times against.

Usage (by the supervisor)::

    python -m omr_scanner.evaluation.intake_qualification.writer SCHEDULE.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

HEADER_BYTES = 64
"""A PNG signature plus most of its IHDR chunk: a file that exists, has the
right suffix, and is not yet an image."""


def _chunks(data: bytes, count: int) -> list[bytes]:
    count = max(1, count)
    size = max(1, -(-len(data) // count))
    return [data[index : index + size] for index in range(0, len(data), size)] or [b""]


def write_file(target: Path, data: bytes, item: dict[str, Any], stop: Any) -> dict[str, Any]:
    """Write one file by its pattern; return timing facts."""
    pattern = item["pattern"]
    pauses = [float(value) for value in item.get("pauses", ())]
    hold_after = float(item.get("hold_after", 0.0))
    started = time.time()
    if pattern == "atomic":
        with target.open("wb") as handle:
            handle.write(data)
    elif pattern in ("stepped", "long_pause"):
        parts = _chunks(data, len(pauses) + 1)
        for index, part in enumerate(parts):
            with target.open("ab" if index else "wb") as handle:
                handle.write(part)
            if index < len(pauses):
                stop.sleep(pauses[index])
    elif pattern == "header_first":
        with target.open("wb") as handle:
            handle.write(data[:HEADER_BYTES])
        stop.sleep(pauses[0] if pauses else 0.5)
        with target.open("ab") as handle:
            handle.write(data[HEADER_BYTES:])
    elif pattern == "held_open":
        parts = _chunks(data, len(pauses) + 1)
        with target.open("wb") as handle:
            for index, part in enumerate(parts):
                handle.write(part)
                handle.flush()
                if index < len(pauses):
                    stop.sleep(pauses[index])
            stop.sleep(hold_after)
    elif pattern == "rename":
        temporary = target.with_name(target.name + ".part")
        parts = _chunks(data, len(pauses) + 1)
        for index, part in enumerate(parts):
            with temporary.open("ab" if index else "wb") as handle:
                handle.write(part)
            if index < len(pauses):
                stop.sleep(pauses[index])
        temporary.replace(target)
    else:
        raise ValueError(f"unknown write pattern {pattern!r}")
    return {"write_started_at": started, "write_completed_at": time.time()}


class _Stop:
    """Sleeps that end early when the supervisor asks the writer to stop."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    @property
    def requested(self) -> bool:
        return self.path is not None and self.path.exists()

    def sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.requested:
                return
            time.sleep(min(0.05, remaining))


def run(schedule_path: Path) -> int:
    """Write the schedule's files; return the exit code."""
    from omr_scanner.evaluation.intake_qualification.evidence import EvidenceLog

    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    log = EvidenceLog(Path(schedule["log"]), campaign_id=schedule["campaign"],
                      role=f"writer:{schedule['source']}")
    stop = _Stop(Path(schedule["stop_file"]) if schedule.get("stop_file") else None)
    target_dir = Path(schedule["target"])
    target_dir.mkdir(parents=True, exist_ok=True)
    log.write("writer_started", source=schedule["source"], files=len(schedule["arrivals"]),
              target=str(target_dir))
    start_file = Path(schedule["start_file"])
    while not start_file.exists():
        if stop.requested:
            log.write("writer_stopped", reason="stop requested before start")
            return 3
        time.sleep(0.05)
    origin = None
    while origin is None:
        try:
            origin = float(start_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):  # renamed into place by the supervisor right now
            time.sleep(0.02)
    written = 0
    for item in schedule["arrivals"]:
        wait = origin + float(item["at"]) - time.time()
        if wait > 0:
            stop.sleep(wait)
        if stop.requested:
            log.write("writer_stopped", reason="stop requested", written=written)
            return 3
        data = Path(item["pool"]).read_bytes()
        target = target_dir / item["name"]
        log.write("write_started", seq=item["seq"], source=schedule["source"], name=item["name"],
                  path=str(target), pattern=item["pattern"], content=item["content"])
        timing = write_file(target, data, item, stop)
        digest = hashlib.sha256(data).hexdigest()
        if digest != item["sha256"]:
            log.write("writer_error", seq=item["seq"], error="pool bytes changed")
            return 2
        log.write("write_completed", seq=item["seq"], source=schedule["source"],
                  name=item["name"], path=str(target), pattern=item["pattern"],
                  content=item["content"], size=len(data), sha256=digest,
                  created_at=timing["write_started_at"], **timing)
        written += 1
    log.write("writer_finished", source=schedule["source"], written=written)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run one writer from the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("schedule", type=Path)
    arguments = parser.parse_args(argv)
    return run(arguments.schedule)


if __name__ == "__main__":  # pragma: no cover - a separate process
    sys.exit(main())
