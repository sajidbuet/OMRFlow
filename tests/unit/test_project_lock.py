"""Unit tests for project locking (Phase 10, §3)."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from omr_scanner.services import project_lock


class TestAcquireAndRelease:
    def test_acquire_creates_a_lock_file(self, tmp_path: Path) -> None:
        lock = project_lock.acquire(tmp_path)
        assert lock.path.is_file()
        payload = json.loads(lock.path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
        assert payload["hostname"] == socket.gethostname()
        lock.release()

    def test_release_removes_the_file_and_is_idempotent(self, tmp_path: Path) -> None:
        lock = project_lock.acquire(tmp_path)
        lock.release()
        assert not lock.path.is_file()
        lock.release()  # must not raise

    def test_context_manager_releases_on_exit(self, tmp_path: Path) -> None:
        with project_lock.acquire(tmp_path) as lock:
            assert lock.path.is_file()
        assert not lock.path.is_file()

    def test_a_second_acquire_while_held_is_refused(self, tmp_path: Path) -> None:
        first = project_lock.acquire(tmp_path)
        try:
            with pytest.raises(project_lock.ProjectLockHeldError) as excinfo:
                project_lock.acquire(tmp_path)
            assert excinfo.value.holder.pid == os.getpid()
            # This process's own lock, on this machine, with this PID
            # running: never reported as stale merely because it exists.
            assert excinfo.value.holder.likely_stale is False
        finally:
            first.release()

    def test_acquiring_after_release_succeeds(self, tmp_path: Path) -> None:
        first = project_lock.acquire(tmp_path)
        first.release()
        second = project_lock.acquire(tmp_path)
        second.release()


class TestStaleDetection:
    def test_a_lock_from_a_dead_pid_on_this_host_is_reported_likely_stale(
        self, tmp_path: Path
    ) -> None:
        # A pid essentially guaranteed not to be running.
        dead_pid = 999_999
        lock_path = project_lock._lock_path(tmp_path)
        lock_path.write_text(
            json.dumps(
                {
                    "pid": dead_pid,
                    "hostname": socket.gethostname(),
                    "app_version": "0.0.0",
                    "acquired_at": "2020-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(project_lock.ProjectLockHeldError) as excinfo:
            project_lock.acquire(tmp_path)
        assert excinfo.value.holder.likely_stale is True

    def test_a_lock_from_another_host_is_never_assumed_stale(self, tmp_path: Path) -> None:
        lock_path = project_lock._lock_path(tmp_path)
        lock_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "hostname": "some-other-machine",
                    "app_version": "0.0.0",
                    "acquired_at": "2020-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(project_lock.ProjectLockHeldError) as excinfo:
            project_lock.acquire(tmp_path)
        # This process's own pid is (of course) running, but on a *different*
        # recorded host - there is no way to check liveness on that host, so
        # this must never be reported as stale.
        assert excinfo.value.holder.likely_stale is False

    def test_a_corrupt_lock_file_is_reported_as_stale_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        lock_path = project_lock._lock_path(tmp_path)
        lock_path.write_text("not json at all", encoding="utf-8")
        with pytest.raises(project_lock.ProjectLockHeldError) as excinfo:
            project_lock.acquire(tmp_path)
        assert excinfo.value.holder.likely_stale is True

    def test_acquire_never_silently_removes_even_a_stale_lock(self, tmp_path: Path) -> None:
        lock_path = project_lock._lock_path(tmp_path)
        lock_path.write_text(
            json.dumps(
                {
                    "pid": 999_999,
                    "hostname": socket.gethostname(),
                    "app_version": "0.0.0",
                    "acquired_at": "2020-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(project_lock.ProjectLockHeldError):
            project_lock.acquire(tmp_path)
        # The lock file must still be there - acquire() never deletes it.
        assert lock_path.is_file()


class TestForceAcquire:
    def test_force_acquire_removes_the_existing_lock_and_claims_a_fresh_one(
        self, tmp_path: Path
    ) -> None:
        lock_path = project_lock._lock_path(tmp_path)
        lock_path.write_text(
            json.dumps(
                {
                    "pid": 999_999,
                    "hostname": socket.gethostname(),
                    "app_version": "0.0.0",
                    "acquired_at": "2020-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        lock = project_lock.force_acquire(tmp_path)
        try:
            payload = json.loads(lock.path.read_text(encoding="utf-8"))
            assert payload["pid"] == os.getpid()
        finally:
            lock.release()
