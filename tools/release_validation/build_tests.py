"""Stage: did the build produce a complete, correct bundle?

Purpose:
    Inspect what PyInstaller laid down in ``dist/OMRFlow`` before anything
    tries to run it. Everything here is a file-system or metadata check, so it
    is fast and its failures name a specific missing thing rather than "the
    application did not start".

Reuses rather than reimplements:
    ``packaging/audit_dependencies.py`` (PE import tables) and
    ``packaging/verify_frozen_imports.py`` (the PYZ archive) already exist and
    are what the release scripts run. This stage invokes them and records their
    verdicts, because a second implementation of either would be a second thing
    to keep correct.

Building is opt-in:
    By default this stage validates whatever bundle is already there. ``--build``
    runs ``scripts/release/Build-App.ps1 -Clean`` first, which takes minutes and
    replaces ``dist/``, and is not something a ``--safe`` run should do behind
    the operator's back.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from tools.release_validation import config as cfg
from tools.release_validation.process_utils import run_command
from tools.release_validation.results import StageResult, Status

REQUIRED_BUNDLE_PATHS: tuple[tuple[str, str], ...] = (
    ("Qt core runtime", r"_internal\PySide6\Qt6Core.dll"),
    ("Qt GUI runtime", r"_internal\PySide6\Qt6Gui.dll"),
    ("Qt widgets runtime", r"_internal\PySide6\Qt6Widgets.dll"),
    ("Qt Windows platform plugin", r"_internal\PySide6\plugins\platforms\qwindows.dll"),
    ("Qt image format plugins", r"_internal\PySide6\plugins\imageformats"),
    ("Visual C++ runtime (bundled)", r"_internal\VCRUNTIME140.dll"),
    ("Pillow imaging backend", r"_internal\PIL"),
    ("OpenCV", r"_internal\cv2"),
    ("NumPy", r"_internal\numpy"),
    ("application icon", r"_internal\omr_scanner\gui\resources\branding\icon.ico"),
    ("application logo", r"_internal\omr_scanner\gui\resources\branding\logo.svg"),
    ("workflow stage icons", r"_internal\omr_scanner\gui\resources\icons\lucide"),
    ("bundled sample workbook", r"_internal\omr_scanner\resources\templates"),
)
"""What must exist beside the executable, and what a reader should call it.

Only things with a file-system presence. A pure-Python dependency lives inside
the PYZ archive and has no directory here, which is what
``verify_frozen_imports.py`` is for - looking for one and reporting it missing
was a real defect in an earlier version of this list.
"""

UNEXPECTED_PATTERNS: tuple[str, ...] = ("python.exe", "pythonw.exe", "*.pyc.bak", "*.orig")
"""Things whose presence means the bundle is not what it should be.

A Python interpreter inside the installation is the clearest sign the freeze
went wrong: the whole point is that the target machine needs none.
"""


def _version_of(path: Path) -> str:
    """The ProductVersion in a Windows binary's resources, or ``""``."""
    if sys.platform != "win32":  # pragma: no cover - non-Windows
        return ""
    code, output = run_command(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            f"(Get-Item -LiteralPath '{path}')"
            ".VersionInfo.ProductVersion",
        ],
        timeout_seconds=60,
    )
    return output.strip() if code == 0 else ""


def build_bundle(config: cfg.ValidationConfig) -> StageResult:
    """Run the repository's own build script."""
    started = time.monotonic()
    stage = StageResult(name="Application build")
    script = cfg.SCRIPTS_DIR / "Build-App.ps1"
    if not script.is_file():
        stage.fail("build script exists", f"{script} is missing")
        return stage

    code, output = run_command(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Clean",
        ],
        timeout_seconds=config.timeouts.build_seconds,
        cwd=cfg.REPOSITORY_ROOT,
    )
    log = config.logs_dir / "build-app.log"
    log.write_text(output, encoding="utf-8", errors="replace")
    stage.ok(
        "Build-App.ps1 completed",
        code == 0,
        detail=f"exit {code}",
        reason=f"the build failed (exit {code}) - see logs/{log.name}",
    )
    stage.duration_seconds = time.monotonic() - started
    return stage


def run(config: cfg.ValidationConfig) -> StageResult:
    """Inspect the built bundle."""
    started = time.monotonic()
    stage = StageResult(name="Build/package verification")

    if not cfg.BUNDLE_EXE.is_file():
        stage.skipped_reason = (
            f"no application bundle at {cfg.BUNDLE_DIR}. Run "
            ".\\scripts\\release\\Build-App.ps1 -Clean, or pass --build."
        )
        return stage

    stage.ok("the executable exists", True, detail=str(cfg.BUNDLE_EXE))

    # ------------------------------------------------------------- version
    from omr_scanner import _version

    reported = _version_of(cfg.BUNDLE_EXE)
    stage.ok(
        "the executable's version matches the source",
        reported == _version.__version__,
        detail=f"binary reports {reported!r}, source says {_version.__version__!r}",
        reason=(
            f"the bundle is version {reported!r} but the source is "
            f"{_version.__version__!r} - rebuild before qualifying it"
        ),
    )

    # ------------------------------------------------------- required files
    for label, relative in REQUIRED_BUNDLE_PATHS:
        stage.ok(
            f"bundled: {label}",
            (cfg.BUNDLE_DIR / relative).exists(),
            detail=relative,
            reason=f"{relative} is missing from the bundle",
        )

    icons = cfg.BUNDLE_DIR / r"_internal\omr_scanner\gui\resources\icons\lucide"
    icon_count = len(list(icons.glob("*.svg"))) if icons.is_dir() else 0
    stage.ok(
        "the stage icon set is complete",
        icon_count >= 9,
        detail=f"{icon_count} SVG icons",
        reason=f"only {icon_count} icons; the workflow ribbon needs at least nine",
    )

    # ---------------------------------------------------- unexpected files
    unexpected: list[str] = []
    for pattern in UNEXPECTED_PATTERNS:
        unexpected.extend(
            str(found.relative_to(cfg.BUNDLE_DIR))
            for found in cfg.BUNDLE_DIR.rglob(pattern)
        )
    stage.ok(
        "no unexpected build artifacts",
        not unexpected,
        detail="; ".join(unexpected[:5]) if unexpected else "none",
        reason=f"the bundle contains {len(unexpected)} thing(s) it should not: {unexpected[:5]}",
    )

    # ------------------------------------------------- the existing audits
    python = sys.executable
    for label, script, blocking_note in (
        ("no unresolved DLL imports", "audit_dependencies.py", "a dependency is missing"),
        (
            "every imported dependency survived the freeze",
            "verify_frozen_imports.py",
            "a Python dependency was dropped from the archive",
        ),
    ):
        path = cfg.PACKAGING_DIR / script
        if not path.is_file():
            stage.skip(label, f"{script} is not in this checkout")
            continue
        code, output = run_command(
            [python, str(path), str(cfg.BUNDLE_DIR)],
            timeout_seconds=600,
            cwd=cfg.REPOSITORY_ROOT,
        )
        log = config.logs_dir / f"{path.stem}.log"
        log.write_text(output, encoding="utf-8", errors="replace")
        stage.record(
            label,
            Status.PASS if code == 0 else Status.FAIL,
            detail=output.strip().splitlines()[-1] if output.strip() else f"exit {code}",
            reason="" if code == 0 else f"{blocking_note} - see logs/{log.name}",
            artifacts=[f"logs/{log.name}"],
        )

    stage.duration_seconds = time.monotonic() - started
    return stage
