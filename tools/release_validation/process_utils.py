"""Launching, watching and stopping processes without hanging or overreaching.

Purpose:
    Two jobs the qualification run depends on and must get exactly right:

    * **Never block forever.** Every wait here takes a deadline. A harness
      meant to run unattended that hangs is worse than one that fails.
    * **Never kill something that is not ours.** The cleanup stage must be able
      to say "stop the OMRFlow instances this run started" without touching an
      OMRFlow the operator has open, or anything else on the machine.

How ownership is decided:
    By process identity, not by name. Every process this module starts is
    remembered in :class:`ProcessRegistry`, and only registered processes are
    ever terminated. A name-based sweep is offered separately and is used only
    when the operator asks for it, because "kill everything called OMRFlow" is
    a reasonable thing to want after a crash and an unreasonable default.
"""

from __future__ import annotations

import ctypes
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# ------------------------------------------------------------------ waiting


def wait_until(
    predicate: Callable[[], bool],
    timeout_seconds: float,
    *,
    interval_seconds: float = 0.25,
) -> bool:
    """Poll ``predicate`` until it is true or the deadline passes.

    Returns whether it became true. Deliberately returns rather than raising:
    "the window never appeared" is a result the caller records, not an error it
    has to catch.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_seconds)
    return predicate()


# -------------------------------------------------------- window enumeration
#
# Done with ctypes rather than a dependency: the packaged-application stage
# needs a window title and whether the window is responding, both of which are
# three Win32 calls. pywinauto is used when it is installed, for the richer
# UI Automation checks, but its absence must not stop the basic ones.

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None  # type: ignore[attr-defined]

_WM_NULL = 0x0000
_SMTO_ABORTIFHUNG = 0x0002


def _enum_windows_for_pid(pid: int) -> list[int]:
    """Handles of the top-level, visible windows belonging to ``pid``."""
    if _user32 is None:  # pragma: no cover - non-Windows
        return []
    handles: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)  # type: ignore[misc]
    def callback(hwnd: int, _lparam: int) -> bool:
        owner = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and _user32.IsWindowVisible(hwnd):
            handles.append(hwnd)
        return True

    _user32.EnumWindows(callback, 0)
    return handles


def window_title_for_pid(pid: int) -> str:
    """The first non-empty visible window title of ``pid``, or ``""``."""
    if _user32 is None:  # pragma: no cover - non-Windows
        return ""
    for hwnd in _enum_windows_for_pid(pid):
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            continue
        buffer = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buffer, length + 1)
        if buffer.value.strip():
            return buffer.value
    return ""


def window_is_responding(pid: int, timeout_ms: int = 5000) -> bool:
    """Whether the process's window answers a null message.

    This is what Task Manager means by "Not responding": the message loop is
    not pumping. A frozen application still exists, still has a window, and
    would pass every check that only asks whether it is running.
    """
    if _user32 is None:  # pragma: no cover - non-Windows
        return True
    handles = _enum_windows_for_pid(pid)
    if not handles:
        return False
    result = ctypes.c_ulong()
    for hwnd in handles:
        answered = _user32.SendMessageTimeoutW(
            hwnd, _WM_NULL, 0, 0, _SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result)
        )
        if answered:
            return True
    return False


def close_window_for_pid(pid: int) -> bool:
    """Ask the process's window to close, as a user clicking the X would.

    Posts WM_CLOSE rather than terminating, so the application runs its own
    shutdown - which is the thing being tested. Termination is the fallback.
    """
    if _user32 is None:  # pragma: no cover - non-Windows
        return False
    wm_close = 0x0010
    posted = False
    for hwnd in _enum_windows_for_pid(pid):
        if _user32.PostMessageW(hwnd, wm_close, 0, 0):
            posted = True
    return posted


# ------------------------------------------------------------------ launching


@dataclass
class LaunchedProcess:
    """A process this run started, and the files its output went to."""

    popen: subprocess.Popen[bytes]
    stdout_path: Path
    stderr_path: Path
    label: str

    @property
    def pid(self) -> int:
        """The operating-system process id."""
        return self.popen.pid

    @property
    def running(self) -> bool:
        """Whether the process has not yet exited."""
        return self.popen.poll() is None

    @property
    def exit_code(self) -> int | None:
        """Its exit status, or ``None`` while it is still running."""
        return self.popen.poll()

    def read_output(self, limit: int = 8000) -> str:
        """Whatever the process printed, truncated for a report."""
        parts: list[str] = []
        for name, path in (("stdout", self.stdout_path), ("stderr", self.stderr_path)):
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if text:
                parts.append(f"--- {name} ---\n{text}")
        combined = "\n".join(parts)
        return combined[:limit] + ("\n[truncated]" if len(combined) > limit else "")


@dataclass
class ProcessRegistry:
    """Every process this run started, so cleanup can stop exactly those."""

    processes: list[LaunchedProcess] = field(default_factory=list)

    def launch(
        self,
        command: Sequence[str | Path],
        *,
        label: str,
        log_dir: Path,
        environment: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> LaunchedProcess:
        """Start a process with its output captured to files.

        Output goes to files rather than pipes deliberately: a GUI application
        that fills a pipe nobody is draining deadlocks, and this harness has to
        survive being left alone.
        """
        log_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(character if character.isalnum() else "-" for character in label)
        stdout_path = log_dir / f"{safe}.stdout.log"
        stderr_path = log_dir / f"{safe}.stderr.log"
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            popen = subprocess.Popen(
                [str(part) for part in command],
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                env=environment,
                cwd=str(cwd) if cwd else None,
            )
        launched = LaunchedProcess(
            popen=popen, stdout_path=stdout_path, stderr_path=stderr_path, label=label
        )
        self.processes.append(launched)
        return launched

    def stop(self, process: LaunchedProcess, *, timeout_seconds: float = 30.0) -> str:
        """Close, then terminate, then kill. Returns how it ended.

        Escalating rather than killing outright, because a clean exit is
        evidence and a killed process destroys it.
        """
        if not process.running:
            return "already exited"
        close_window_for_pid(process.pid)
        if wait_until(lambda: not process.running, timeout_seconds * 0.5):
            return "closed its window"
        process.popen.terminate()
        if wait_until(lambda: not process.running, timeout_seconds * 0.5):
            return "terminated"
        process.popen.kill()
        wait_until(lambda: not process.running, 5.0)
        return "killed"

    def stop_all(self, *, timeout_seconds: float = 30.0) -> list[str]:
        """Stop every process this run started, newest first."""
        notes: list[str] = []
        for process in reversed(self.processes):
            if process.running:
                outcome = self.stop(process, timeout_seconds=timeout_seconds)
                notes.append(f"{process.label} (pid {process.pid}): {outcome}")
        return notes


def run_command(
    command: Sequence[str | Path],
    *,
    timeout_seconds: float,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run a command to completion and return ``(exit code, combined output)``.

    A timeout produces exit code ``-1`` and a message saying so, rather than an
    exception, because every caller records it as a failed check either way.
    """
    try:
        completed = subprocess.run(
            [str(part) for part in command],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            cwd=str(cwd) if cwd else None,
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout_seconds:.0f}s: {' '.join(str(p) for p in command)}"
    except OSError as error:
        return -1, f"could not run {command[0]}: {error}"
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")
