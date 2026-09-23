"""Fixtures shared by the release-qualification suites.

These suites are ordinary pytest files, run by the framework in a subprocess.
They deliberately reuse the repository's own fixtures - ``tests/conftest.py``
is imported for its template and sheet builders - rather than growing a second,
divergent set of test data.

Isolation:
    ``tests/conftest.py`` is *not* on the path here (these files live under
    ``tools/``), so its autouse ``isolated_user_environment`` does not apply.
    :func:`isolated_user_environment` below does the same job, because a
    qualification run must never write into the operator's real configuration.

Screenshots:
    :func:`checkpoint` grabs a widget to a PNG under the directory named by
    ``OMRFLOW_VALIDATION_SCREENSHOTS``. The framework sets that variable; when
    it is unset the fixture is a no-op, so these files still run under a plain
    ``pytest`` invocation.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

# The repository root, so that `tests.conftest` and `omr_scanner` both import.
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

pytest.importorskip("pytestqt", reason="pytest-qt is required for the GUI suites")


@pytest.fixture(autouse=True)
def isolated_user_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the application's configuration and logs into this test's dir.

    Autouse and unconditional. Every test in these suites constructs real
    application objects, and several of them save settings; without this the
    first run would overwrite the operator's window state and recent-project
    list.
    """
    from omr_scanner.config.paths import ENV_CONFIG_DIR, ENV_LOG_DIR

    monkeypatch.setenv(ENV_CONFIG_DIR, str(tmp_path / "user_config"))
    monkeypatch.setenv(ENV_LOG_DIR, str(tmp_path / "user_logs"))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """An empty directory that may hold throwaway project folders."""
    directory = tmp_path / "workspace"
    directory.mkdir()
    return directory


@pytest.fixture
def main_window(qtbot, tmp_path: Path):
    """A real :class:`MainWindow`, registered with qtbot so it is destroyed.

    Built exactly as ``omr_scanner.gui.application`` builds it, but with its
    configuration file inside the test's temporary directory.
    """
    from omr_scanner.config import AppConfig
    from omr_scanner.gui.main_window import MainWindow

    window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
    qtbot.addWidget(window)
    return window


@pytest.fixture
def shown_window(main_window, qtbot):
    """A main window that has been shown and has had its layout settle.

    Several checks - ribbon layout mode, focus order, screenshots - are
    meaningless on a window that was never shown.
    """
    main_window.resize(1280, 860)
    main_window.show()
    qtbot.waitExposed(main_window)
    qtbot.wait(120)
    return main_window


@pytest.fixture
def silent_dialogs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Capture message boxes instead of showing them.

    A modal box in an unattended run is a hang. Every one raised during
    qualification is recorded and answered, never displayed - which is also how
    a test can assert that a warning *was* raised.
    """
    shown: list[tuple[str, str]] = []

    def record(kind: str) -> staticmethod:
        def handler(_parent: object, title: str, text: str, *_args: object) -> int:
            shown.append((f"{kind}:{title}", text))
            return 0

        return staticmethod(handler)

    for kind in ("warning", "critical", "information", "question"):
        for target in (
            "omr_scanner.gui.error_reporting.QMessageBox",
            "PySide6.QtWidgets.QMessageBox",
        ):
            try:
                monkeypatch.setattr(f"{target}.{kind}", record(kind))
            except (AttributeError, ImportError):  # pragma: no cover
                continue
    return shown


@pytest.fixture(scope="session")
def screenshot_dir() -> Path | None:
    """Where checkpoint screenshots go, or ``None`` when not requested."""
    raw = os.environ.get("OMRFLOW_VALIDATION_SCREENSHOTS")
    if not raw:
        return None
    directory = Path(raw)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture
def checkpoint(
    screenshot_dir: Path | None, qtbot
) -> Iterator[Callable[[object, str], Path | None]]:
    """Grab a widget to ``<screenshots>/<name>.png``.

    Returns the path, or ``None`` when screenshots were not requested. Grabbing
    the widget rather than the screen means the image is the same whatever else
    is on the desktop, which is what makes a visual baseline comparable at all.
    """

    def take(widget: object, name: str) -> Path | None:
        if screenshot_dir is None:
            return None
        # Let pending paint events run before grabbing.
        qtbot.wait(150)
        pixmap = widget.grab()  # type: ignore[attr-defined]
        destination = screenshot_dir / f"{name}.png"
        pixmap.save(str(destination), "PNG")
        return destination

    yield take
