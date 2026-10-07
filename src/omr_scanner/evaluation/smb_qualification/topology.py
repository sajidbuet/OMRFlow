r"""What the SMB qualification runs on, and whether that is genuine SMB (revised phase 10).

``ACCEPTANCE_CRITERIA.md`` §6: at least two Windows machines; sources on
genuine SMB shares (``\\host\share\...``) written by a process on the remote
machine; the project database on a local disk. A simulated share is never
reported as SMB. This module decides that from facts, not from intentions:

* a source root is genuine only if it is a UNC path whose host is not this
  machine (by name, fully qualified name, ``localhost``, a loopback address
  or one of this machine's own addresses) - a junction, a ``subst`` drive, a
  mapped drive letter or ``\\localhost\...`` / ``\\127.0.0.1\...`` is not;
* a writer is remote only if the host it reported in its own evidence log is
  not this machine;
* the database is local only if its drive is a fixed local disk.

It also measures the offset between this machine's clock and a share host's
(the writers log their own clock), and records the SMB connection details
Windows will disclose (``Get-SmbConnection`` needs elevation; when it is
refused that is recorded, never guessed).
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1", "."})


def unc_host(path: str) -> str | None:
    r"""The server of ``\\host\share\...`` or ``\\?\UNC\host\share\...``, else ``None``."""
    text = path.replace("/", "\\")
    if text.upper().startswith("\\\\?\\UNC\\"):
        rest = text[8:]
    elif text.startswith("\\\\") and not text.startswith("\\\\?\\") and not text.startswith(
        "\\\\.\\"
    ):
        rest = text[2:]
    else:
        return None
    parts = [part for part in rest.split("\\") if part]
    if len(parts) < 2:  # a UNC path names a host *and* a share
        return None
    return parts[0]


def local_identities() -> frozenset[str]:
    """Every name and address this machine answers to (lower case)."""
    names: set[str] = set(LOOPBACK_NAMES)
    for value in (socket.gethostname(), os.environ.get("COMPUTERNAME", "")):
        if value:
            names.add(value.lower())
            names.add(value.lower().split(".")[0])
    with contextlib.suppress(OSError):
        fqdn = socket.getfqdn()
        names.add(fqdn.lower())
        names.add(fqdn.lower().split(".")[0])
    with contextlib.suppress(OSError):
        for info in socket.getaddrinfo(socket.gethostname(), None):
            names.add(str(info[4][0]).lower().split("%")[0])
    return frozenset(name for name in names if name)


def is_local_host(host: str, identities: frozenset[str] | None = None) -> bool:
    """Whether ``host`` names this machine (or a loopback address)."""
    known = identities if identities is not None else local_identities()
    value = host.lower().strip("[]")
    if value in known or value.split(".")[0] in known:
        return True
    if value.startswith("127.") or value == "::1":
        return True
    with contextlib.suppress(OSError):
        resolved = {
            str(info[4][0]).lower().split("%")[0] for info in socket.getaddrinfo(value, None)
        }
        if resolved & known or any(item.startswith("127.") for item in resolved):
            return True
    return False


@dataclass(frozen=True, slots=True)
class SourceTopology:
    """One source root, judged."""

    label: str
    root: str
    host: str | None
    genuine_smb: bool
    reason: str

    def to_json(self) -> dict[str, Any]:
        """Plain data for the report."""
        return asdict(self)


def classify_source(
    label: str, root: str, identities: frozenset[str] | None = None
) -> SourceTopology:
    """Whether ``root`` is a genuine SMB share on another machine, with the reason."""
    host = unc_host(root)
    if host is None:
        return SourceTopology(label, root, None, False,
                              "not a UNC path (a local folder, junction, subst or mapped drive)")
    if is_local_host(host, identities):
        return SourceTopology(label, root, host, False, f"the share host {host!r} is this machine")
    return SourceTopology(label, root, host, True, f"UNC share on {host}")


def drive_type(path: Path) -> str:
    """Windows' drive type of ``path``'s volume: fixed / removable / remote / cdrom / ramdisk."""
    text = str(path.resolve())
    if text.startswith("\\\\"):
        return "remote"
    if sys.platform != "win32":  # pragma: no cover - the qualification runs on Windows
        return "fixed"
    import ctypes

    root = os.path.splitdrive(text)[0] + "\\"
    code = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
    return {0: "unknown", 1: "no_root", 2: "removable", 3: "fixed", 4: "remote", 5: "cdrom",
            6: "ramdisk"}.get(int(code), f"type {code}")


def database_location(project_root: Path) -> dict[str, Any]:
    """Where the project database is, and whether that is a local fixed disk."""
    database = project_root / "database.sqlite"
    kind = drive_type(project_root)
    return {"path": str(database), "drive_type": kind, "local": kind == "fixed",
            "unc": str(project_root.resolve()).startswith("\\\\")}


def smb_connections() -> dict[str, Any]:
    """``Get-SmbConnection`` (server, share, dialect) - or why Windows would not say."""
    if sys.platform != "win32":  # pragma: no cover
        return {"available": False, "reason": "not Windows"}
    command = ("Get-SmbConnection -ErrorAction Stop | Select-Object ServerName, ShareName, "
               "Dialect, NumOpens, Encrypted, Signed | ConvertTo-Json -Compress")
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    if done.returncode != 0:
        return {"available": False,
                "reason": (done.stderr.strip() or done.stdout.strip())[:400]
                or f"exit {done.returncode}"}
    text = done.stdout.strip()
    if not text:
        return {"available": True, "connections": []}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"available": False, "reason": f"unparseable output: {text[:200]}"}
    return {"available": True, "connections": data if isinstance(data, list) else [data]}


def machine_description() -> dict[str, Any]:
    """This (the OMRFlow) machine, for the report."""
    import platform

    description: dict[str, Any] = {
        "host": socket.gethostname(),
        "computer_name": os.environ.get("COMPUTERNAME", ""),
        "identities": sorted(local_identities()),
        "platform": platform.platform(),
        "windows": platform.win32_ver() if sys.platform == "win32" else None,
        "processor": platform.processor(),
        "cpu_logical": os.cpu_count(),
    }
    with contextlib.suppress(Exception):
        import psutil

        description["ram_bytes"] = psutil.virtual_memory().total
    return description


def clock_offset(probe_dir: Path, *, samples: int = 5) -> dict[str, Any]:
    """Estimate (share host clock - this clock) from the times the share stamps on a probe file.

    A file written through SMB gets its last-write time from the server's
    clock. Each sample writes a probe and reads the stamp back; the stamp
    fell between the local times before and after, so the offset is the
    stamp minus the midpoint, give or take half the round trip. The probe
    folder must be beside the watched folder, never inside it.
    """
    probe_dir.mkdir(parents=True, exist_ok=True)
    offsets: list[float] = []
    bounds: list[float] = []
    errors: list[str] = []
    for index in range(max(1, samples)):
        probe = probe_dir / f"omrflow-clock-probe-{os.getpid()}-{index}.tmp"
        try:
            before = time.time()
            with probe.open("wb") as handle:
                handle.write(b"omrflow clock probe")
                handle.flush()
                os.fsync(handle.fileno())
            stamped = probe.stat().st_mtime
            after = time.time()
            offsets.append(stamped - (before + after) / 2)
            bounds.append((after - before) / 2)
        except OSError as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            with contextlib.suppress(OSError):
                probe.unlink()
        time.sleep(0.2)
    if not offsets:
        return {"ok": False, "errors": errors[:5]}
    return {
        "ok": True,
        "offset_seconds": round(statistics.median(offsets), 4),
        "uncertainty_seconds": round(max(bounds) + 0.01, 4),
        "samples": len(offsets),
        "spread_seconds": round(max(offsets) - min(offsets), 4),
        "errors": errors[:5],
    }


__all__ = [
    "SourceTopology",
    "classify_source",
    "clock_offset",
    "database_location",
    "drive_type",
    "is_local_host",
    "local_identities",
    "machine_description",
    "smb_connections",
    "unc_host",
]
