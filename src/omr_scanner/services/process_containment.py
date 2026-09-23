"""Bind worker-process lifetime to the coordinator's, on Windows.

Purpose:
    Fix a real, disclosed Phase 10 gap found during this project's own
    kill/resume testing: `Process.kill()` on a coordinator process does not
    terminate the worker processes it had already spawned - on Windows,
    child processes are not tied to their parent's lifetime by default, so
    an abrupt kill leaves orphaned workers running indefinitely, consuming
    memory and a CPU core, until found and killed by hand.

Responsibilities:
    * :func:`ensure_worker_processes_die_with_this_one` - the one function
      this module exists to provide. Called once, idempotently, before the
      first worker pool starts.

What does NOT belong here:
    * Anything about *what* a worker does - this module only ever changes
      whether a worker process outlives its coordinator, never what work it
      performs.
    * A cross-platform equivalent. POSIX process groups
      (`os.setpgrp`/`os.killpg`) require the *killer* to know to signal the
      whole group - they do not make an abrupt `SIGKILL` of the coordinator
      alone take its children with it, which is the Windows Job Object
      property this module specifically relies on. A POSIX equivalent would
      need a supervisor process explicitly watching for the coordinator's
      death, which is a different design this phase did not build.

Why a Windows Job Object, and why it works for a *forced* kill specifically:
    A Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` kills every
    process still assigned to it the moment its *last open handle* closes.
    Windows closes every handle a process holds when that process ends, for
    any reason at all - including ``TerminateProcess`` (what
    ``subprocess.Process.kill()`` calls, and what Task Manager's "End Task"
    does) - not only a clean exit. Child processes are members of their
    parent's job automatically unless they explicitly "break away", which
    Python's ``multiprocessing`` does not do. Assigning *this* process to
    such a job, once, before any worker pool exists, is therefore enough:
    every ``ProcessPoolExecutor`` worker this process ever spawns inherits
    job membership, and killing this process - however abruptly - closes
    the job handle and takes every still-running worker with it.

Why this is safe to call from a test, or from a sandboxed environment:
    Every failure mode (already inside an incompatible job, the Windows API
    unavailable, any other `OSError`) is caught and reported as `False`,
    never raised - this is a best-effort hardening measure, not a
    precondition for correct operation. §4's single-writer database
    architecture already guarantees an orphaned worker cannot corrupt or
    duplicate data on its own; this module only shortens how long one keeps
    running after a crash, for machines where it can be established at all.
"""

from __future__ import annotations

import ctypes
import logging
import sys

_LOGGER = logging.getLogger(__name__)

_JOB_HANDLE: int | None = None
"""The one job object handle this process ever creates. Kept as a module
global, deliberately never closed while the process is alive - closing it
early would defeat the whole point, since the job's kill-on-close behaviour
fires when *this* handle closes, whichever way that happens."""

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


class _IoCounters(ctypes.Structure):
    _fields_ = (
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    )


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    )


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


def ensure_worker_processes_die_with_this_one() -> bool:
    """Make an abrupt kill of this process take its worker processes with it.

    Idempotent and safe to call from every entry point (the GUI, the CLI,
    a test) - the first call establishes containment and every later call
    is a cheap no-op returning the same answer.

    Returns:
        ``True`` when containment was established (or already had been).
        ``False`` on any non-Windows platform, or when the Windows API
        calls themselves failed - never raises.
    """
    global _JOB_HANDLE
    if _JOB_HANDLE is not None:
        return True

    # A guarding `if sys.platform == "win32": ... else: ...` rather than the
    # early `if sys.platform != "win32": return False` this used to be, and the
    # difference is load-bearing for `mypy --strict --warn-unreachable`. mypy
    # analyses one platform at a time and deliberately does *not* warn about a
    # block it skipped because of a `sys.platform` check - but that exemption
    # covers the branch bodies, not code that merely follows an early return.
    # Analysed for Linux, the old shape made every statement below it
    # unreachable and the CI type-check job failed on it. Keeping both arms as
    # branches also keeps the Windows-only `ctypes.WinDLL` access inside a
    # block mypy skips off-Windows, so no `type: ignore` is needed on either
    # platform. There must be no statement *after* this if/else: on Windows the
    # `if` arm always returns, which would make a trailing line unreachable and
    # simply move the same error to the other platform.
    if sys.platform == "win32":
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            # Explicit argtypes/restype are not optional here: `GetCurrentProcess`
            # returns the pseudo-handle `(HANDLE)-1`, and ctypes' default 32-bit
            # `c_int` guess for an undeclared return type sign-extends or
            # truncates that value incorrectly on 64-bit Python, producing a
            # handle `AssignProcessToJobObject` then rejects as invalid. Found by
            # this module's own isolated test, which failed with
            # `ERROR_INVALID_HANDLE` before these declarations were added.
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            kernel32.CreateJobObjectW.restype = ctypes.c_void_p
            kernel32.SetInformationJobObject.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint32,
            ]
            kernel32.SetInformationJobObject.restype = ctypes.c_int
            kernel32.GetCurrentProcess.argtypes = []
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            kernel32.AssignProcessToJobObject.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int

            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                _LOGGER.warning(
                    "Could not create a job object for worker-process containment "
                    "(error %d); a forced kill may leave orphaned worker processes.",
                    ctypes.get_last_error(),
                )
                return False

            info = _JobObjectExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            configured = kernel32.SetInformationJobObject(
                handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not configured:
                _LOGGER.warning(
                    "Could not configure the worker-containment job object (error %d)",
                    ctypes.get_last_error(),
                )
                kernel32.CloseHandle(handle)
                return False

            current_process = kernel32.GetCurrentProcess()
            assigned = kernel32.AssignProcessToJobObject(handle, current_process)
            if not assigned:
                # The most common real cause: this process is already inside a
                # job object that forbids being placed into another one. Not
                # every Windows environment supports nested jobs (or a sandbox
                # may deliberately prevent it) - reported and skipped, never
                # fatal.
                _LOGGER.warning(
                    "Could not assign this process to the worker-containment job "
                    "object (error %d); a forced kill may leave orphaned worker "
                    "processes.",
                    ctypes.get_last_error(),
                )
                kernel32.CloseHandle(handle)
                return False

            _JOB_HANDLE = handle
            _LOGGER.info("Worker-process containment established (Windows job object)")
            return True
        except OSError:
            _LOGGER.exception(
                "Worker-process containment setup failed; a forced kill may leave "
                "orphaned worker processes"
            )
            return False
    else:
        return False


def containment_established() -> bool:
    """Whether :func:`ensure_worker_processes_die_with_this_one` succeeded.

    For tests and diagnostics - never a precondition callers must check
    before starting a worker pool.
    """
    return _JOB_HANDLE is not None


__all__ = [
    "containment_established",
    "ensure_worker_processes_die_with_this_one",
]
