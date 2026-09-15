"""Executable enforcement of the layering rules in ``docs/ARCHITECTURE.md``.

Why this test exists:
    The architecture rules ("the GUI must not call OpenCV", "imaging must not
    import Qt") are the project's most valuable invariants and the easiest ones
    to break by accident - especially for an AI agent adding a feature in the
    quickest place rather than the right one. Documenting them is not enough, so
    they are asserted here by reading the import statements of every source file.

How it works:
    Each source file is parsed with :mod:`ast` (no module is imported, so the
    test is fast and free of side effects) and its top-level module imports are
    compared against the allowed set for its layer.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "omr_scanner"

FORBIDDEN_IMPORTS: dict[str, tuple[str, ...]] = {
    # layer directory -> module prefixes it must never import
    "domain": ("PySide6", "cv2", "sqlalchemy", "omr_scanner.gui", "omr_scanner.services",
               "omr_scanner.database", "omr_scanner.imaging", "omr_scanner.recognition",
               "omr_scanner.reporting"),
    "imaging": ("PySide6", "omr_scanner.gui", "omr_scanner.services", "omr_scanner.database"),
    "recognition": ("PySide6", "omr_scanner.gui", "omr_scanner.services"),
    "reporting": ("PySide6", "omr_scanner.gui", "cv2"),
    "database": ("PySide6", "cv2", "omr_scanner.gui", "omr_scanner.services"),
    "services": ("PySide6", "omr_scanner.gui"),
    # Developer command line tools sit beside the GUI, not below it: they may
    # call any service or algorithm, but importing a widget would make them
    # depend on a display.
    "tools": ("PySide6", "omr_scanner.gui"),
    "gui": ("cv2", "sqlalchemy", "numpy", "omr_scanner.database", "omr_scanner.imaging",
            "omr_scanner.recognition"),
    "utils": ("PySide6", "cv2", "sqlalchemy", "omr_scanner.gui", "omr_scanner.services",
              "omr_scanner.domain", "omr_scanner.database"),
    "config": ("PySide6", "cv2", "sqlalchemy", "omr_scanner.gui", "omr_scanner.services",
               "omr_scanner.database", "omr_scanner.domain"),
}


def _imported_modules(path: Path) -> set[str]:
    """Return the dotted module names imported by ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            modules.add(node.module)
    return modules


def _source_files(layer: str) -> list[Path]:
    return sorted((SOURCE_ROOT / layer).rglob("*.py"))


def test_source_root_is_discoverable():
    assert SOURCE_ROOT.is_dir(), f"Expected package sources at {SOURCE_ROOT}"


@pytest.mark.parametrize("layer", sorted(FORBIDDEN_IMPORTS))
def test_layer_has_source_files(layer: str):
    assert _source_files(layer), f"Layer '{layer}' has no Python files"


@pytest.mark.parametrize("layer", sorted(FORBIDDEN_IMPORTS))
def test_layer_respects_its_dependency_direction(layer: str):
    forbidden = FORBIDDEN_IMPORTS[layer]
    violations: list[str] = []

    for path in _source_files(layer):
        for module in _imported_modules(path):
            for prefix in forbidden:
                if module == prefix or module.startswith(f"{prefix}."):
                    violations.append(f"{path.relative_to(SOURCE_ROOT)} imports {module}")

    assert not violations, (
        f"Layer '{layer}' violates the dependency direction documented in "
        f"docs/ARCHITECTURE.md:\n  " + "\n  ".join(violations)
    )


def test_gui_contains_no_omr_algorithms():
    """The GUI must not compute image or recognition results itself."""
    banned_calls = ("cvtColor", "adaptiveThreshold", "findContours", "warpPerspective")
    offenders: list[str] = []

    for path in _source_files("gui"):
        source = path.read_text(encoding="utf-8")
        offenders.extend(
            f"{path.name}: {call}" for call in banned_calls if f"{call}(" in source
        )

    assert not offenders, f"OpenCV algorithms found in the GUI layer: {offenders}"


def test_every_module_has_a_docstring():
    """Agent-maintainable code: a module without a docstring is undocumented context."""
    missing = [
        str(path.relative_to(SOURCE_ROOT))
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) is None
    ]

    assert not missing, f"Modules without a docstring: {missing}"
