"""Append-only evidence logs (revised phase 9).

Every process of a campaign - each scanner writer, the coordinator under test,
the supervisor - appends JSON lines to its **own** file, flushed and
``fsync``-ed per event, so a forced kill can lose at most the line being
written (a torn last line, which every reader skips). The supervisor's own log
is written by the one process nobody kills; the coordinator's log records what
it *intended* (claimed, submitted) before doing it, which is what the
"never re-recognised" evidence compares against the database read from outside.

Every line carries the campaign id, the writing process id and a wall-clock
time; a reader refuses lines of another campaign (stale evidence from an
earlier run in the same folder).
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any


class EvidenceLog:
    """One process's append-only JSON-lines log."""

    def __init__(self, path: Path, *, campaign_id: str, role: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.campaign_id = campaign_id
        self.role = role
        self._handle = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, event: str, **fields: Any) -> dict[str, Any]:
        """Append one event, durably, and return it."""
        record = {
            "event": event,
            "campaign": self.campaign_id,
            "role": self.role,
            "pid": os.getpid(),
            "t": time.time(),
            **fields,
        }
        line = json.dumps(record, default=str, separators=(",", ":"))
        with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()
            os.fsync(self._handle.fileno())
        return record

    def close(self) -> None:
        """Close the log file, ignoring any error."""
        with contextlib.suppress(Exception), self._lock:
            self._handle.close()


class StaleEvidenceError(ValueError):
    """A log line belongs to another campaign."""


def read_log(path: Path, *, campaign_id: str | None = None) -> list[dict[str, Any]]:
    """Every complete line of ``path`` (a torn last line is skipped).

    Raises:
        StaleEvidenceError: ``campaign_id`` given and a line names another one.
    """
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if campaign_id is not None and item.get("campaign") != campaign_id:
            raise StaleEvidenceError(
                f"{path.name} holds evidence of campaign {item.get('campaign')!r}, "
                f"not {campaign_id!r}"
            )
        events.append(item)
    return events


class LogTail:
    """Reads the complete lines appended to a log since the last call."""

    def __init__(self, path: Path, *, campaign_id: str) -> None:
        self.path = path
        self.campaign_id = campaign_id
        self._offset = 0
        self._partial = b""

    def read_new(self) -> list[dict[str, Any]]:
        """The complete lines appended since the last call (a partial tail is kept)."""
        if not self.path.is_file():
            return []
        with self.path.open("rb") as handle:
            handle.seek(self._offset)
            data = handle.read()
        if not data:
            return []
        self._offset += len(data)
        data = self._partial + data
        lines = data.split(b"\n")
        self._partial = lines.pop()  # incomplete (or empty) tail
        events: list[dict[str, Any]] = []
        for raw in lines:
            if not raw.strip():
                continue
            try:
                item = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if item.get("campaign") != self.campaign_id:
                raise StaleEvidenceError(
                    f"{self.path.name} holds evidence of campaign {item.get('campaign')!r}"
                )
            events.append(item)
        return events


__all__ = ["EvidenceLog", "LogTail", "StaleEvidenceError", "read_log"]
