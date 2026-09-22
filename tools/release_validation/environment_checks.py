"""Is this machine able to run the qualification at all?

Purpose:
    Fail in the first ten seconds, with a sentence saying what to install,
    rather than forty minutes later inside a stage that assumed a tool was
    there. Everything here is cheap and read-only.

What counts as blocking:
    Only what every run needs: a Python that can import the application, the
    test runner, and a repository that looks like OMRFlow. Optional tooling -
    pywinauto, Inno Setup, PyInstaller - is reported as a warning naming the
    stage it will cost, because a source-only run should not be refused for
    want of an installer compiler.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sys
import time
from pathlib import Path

from tools.release_validation import config as cfg
from tools.release_validation.results import StageResult, Status

OPTIONAL_MODULES = {
    "pytestqt": "Qt GUI and accessibility stages (pip install pytest-qt)",
    "pywinauto": "richer packaged-application checks (pip install pywinauto)",
    "PIL": "visual baseline comparison (pip install pillow)",
    "PyInstaller": "the build stage (pip install pyinstaller)",
}


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # pragma: no cover - broken installs
        return False


def describe_environment() -> dict[str, object]:
    """Facts about this machine, recorded verbatim in the JSON report."""
    return {
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor_count": os.cpu_count(),
        "repository_root": str(cfg.REPOSITORY_ROOT),
        "qt_qpa_platform": os.environ.get("QT_QPA_PLATFORM", "(default)"),
        "modules": {name: _module_available(name) for name in OPTIONAL_MODULES},
    }


def run(config: cfg.ValidationConfig) -> StageResult:
    """Check the prerequisites and report what is missing."""
    started = time.monotonic()
    stage = StageResult(name="Environment")

    stage.ok(
        "running on Windows",
        sys.platform == "win32",
        detail=platform.platform(),
        reason="the packaged, installer and UI-automation stages are Windows-only",
    )

    stage.ok(
        "Python is 3.12 or newer",
        sys.version_info >= (3, 12),
        detail=sys.version.split()[0],
        reason=f"OMRFlow requires Python 3.12; this is {sys.version.split()[0]}",
    )

    # Importable rather than merely present on disk: a source tree that cannot
    # be imported produces 4000 collection errors in the next stage instead of
    # one clear message here.
    stage.ok(
        "the omr_scanner package can be imported",
        _module_available("omr_scanner"),
        detail=str(cfg.SOURCE_DIR),
        reason="run `pip install -e .[dev]` in the repository first",
    )

    stage.ok(
        "pytest is available",
        _module_available("pytest"),
        detail="required by every source and GUI stage",
        reason="run `pip install -e .[dev]`",
    )

    stage.ok(
        "this is an OMRFlow checkout",
        (cfg.REPOSITORY_ROOT / "pyproject.toml").is_file() and cfg.TESTS_DIR.is_dir(),
        detail=str(cfg.REPOSITORY_ROOT),
        reason="pyproject.toml or tests/ is missing",
    )

    # The application's own version, read the same way the release scripts read
    # it, so the report names the build being qualified.
    try:
        from omr_scanner import _version

        stage.record(
            "application version",
            Status.PASS,
            detail=f"{_version.__version__} ({_version.RELEASE_CHANNEL.value})",
        )
    except Exception as error:
        stage.fail("application version", f"could not read it: {error}", exception=error)

    # ------------------------------------------------------------- optional
    for module, purpose in OPTIONAL_MODULES.items():
        if _module_available(module):
            stage.record(f"optional: {module}", Status.PASS, detail=purpose)
        else:
            stage.warn(
                f"optional: {module}",
                detail=purpose,
                reason=f"not installed; this limits {purpose}",
            )

    iscc = _find_iscc()
    if iscc:
        stage.record("optional: Inno Setup compiler", Status.PASS, detail=iscc)
    else:
        stage.warn(
            "optional: Inno Setup compiler",
            reason="ISCC.exe not found; the installer cannot be rebuilt "
            "(winget install --id JRSoftware.InnoSetup). An installer that "
            "already exists can still be tested.",
        )

    stage.record(
        "isolated workspace prepared",
        Status.PASS,
        detail=str(config.workspace),
    )

    stage.duration_seconds = time.monotonic() - started
    return stage


def _find_iscc() -> str:
    """Where the Inno Setup compiler is, matching Build-Installer.ps1's search."""
    on_path = shutil.which("iscc")
    if on_path:
        return on_path
    candidates = [
        # Windows spells these with mixed case; os.environ on Windows is
        # case-insensitive, so the upper-case names find the same values.
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""
