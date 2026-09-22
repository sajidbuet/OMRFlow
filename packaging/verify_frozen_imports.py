"""Check that every runtime dependency survived the freeze.

Why this exists:
    PyInstaller lays a package with compiled extensions out as a directory
    beside the executable - ``_internal/PIL``, ``_internal/cv2`` - where its
    absence is obvious. A *pure* Python package has no such directory: it is
    compiled into the PYZ archive inside ``OMRFlow.exe``, invisible to any
    check that looks at the file system.

    That asymmetry is a trap. ``openpyxl`` is pure Python, and OMRFlow needs
    it to write a result workbook. If the import analysis ever stopped
    finding it - an import moved inside a function, a module renamed, an
    ``excludes`` entry that matched more than it meant to - nothing on disk
    would change, the application would start perfectly, and the failure
    would arrive when an examination office pressed *Generate results*.

    So the archive is read directly and asked what is actually in it.

What it does NOT do:
    Import anything, or run the frozen application. Presence in the archive
    is necessary, not sufficient: a module can be present and still fail at
    run time for its own reasons. The end-to-end workflow test in
    ``docs/release/CLEAN_MACHINE_TEST.md`` is what covers that, and this does
    not stand in for it.

Usage:
    python packaging/verify_frozen_imports.py dist/OMRFlow
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Every distribution the application actually imports, spelled as the module
# it is imported by, together with a module from inside the package. The
# nested module matters: PyInstaller can include a package's `__init__` while
# dropping the submodule that does the work.
REQUIRED_MODULES: dict[str, tuple[str, ...]] = {
    "openpyxl": ("openpyxl", "openpyxl.workbook", "openpyxl.styles", "openpyxl.drawing.image"),
    "et_xmlfile": ("et_xmlfile",),
    "PIL": ("PIL", "PIL.Image"),
    "numpy": ("numpy",),
    "cv2": ("cv2",),
    "sqlalchemy": ("sqlalchemy", "sqlalchemy.orm"),
    "pydantic": ("pydantic",),
    "psutil": ("psutil",),
    "PySide6": ("PySide6", "PySide6.QtWidgets", "PySide6.QtGui", "PySide6.QtCore"),
    "omr_scanner": (
        "omr_scanner",
        "omr_scanner._version",
        "omr_scanner.main",
        "omr_scanner.gui.application",
        "omr_scanner.services.parallel_batch",
    ),
}

# Declared in `pyproject.toml` but imported by nothing in `src/`, so
# PyInstaller's analysis correctly leaves them out. Listed rather than
# omitted, because "it is not in the bundle" and "nobody checked whether it
# should be" look identical in a report that simply says nothing.
#
# `pandas` is declared for "Phase 7 - candidate list import/export", but that
# feature reads CSV with the standard library and XLSX with openpyxl, and no
# module imports pandas. Its absence from the installer is correct; the
# declaration is what is stale.
DECLARED_BUT_UNUSED: tuple[str, ...] = ("pandas",)


def _frozen_module_names(bundle: Path) -> set[str]:
    """Every module name reachable inside the bundle.

    Reads the PYZ archive embedded in the executable, and also treats each
    top-level directory in ``_internal`` as a package, because that is how a
    package with compiled extensions is shipped.
    """
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    executable = bundle / "OMRFlow.exe"
    if not executable.is_file():
        raise SystemExit(f"No OMRFlow.exe in {bundle}. Run Build-App.ps1 first.")

    names: set[str] = set()

    archive = CArchiveReader(str(executable))
    payload = archive.extract("PYZ.pyz")
    # Older and newer PyInstaller releases disagree on whether `extract`
    # returns the bytes or a (flag, bytes) pair.
    if isinstance(payload, tuple):
        payload = payload[1]

    with tempfile.NamedTemporaryFile(suffix=".pyz", delete=False) as handle:
        handle.write(payload)
        pyz_path = Path(handle.name)
    try:
        names.update(ZlibArchiveReader(str(pyz_path)).toc)
    finally:
        pyz_path.unlink(missing_ok=True)

    internal = bundle / "_internal"
    if internal.is_dir():
        for entry in internal.iterdir():
            if entry.is_dir():
                names.add(entry.name)
            elif entry.suffix in {".pyd", ".so"}:
                names.add(entry.name.split(".")[0])

    return names


def main(argv: list[str]) -> int:
    """Report on the bundle named by ``argv``; non-zero if anything is absent."""
    if len(argv) != 1:
        print(__doc__)
        print("usage: verify_frozen_imports.py <bundle-directory>", file=sys.stderr)
        return 2

    bundle = Path(argv[0]).resolve()
    names = _frozen_module_names(bundle)
    print(f"Frozen bundle : {bundle}")
    print(f"Modules found : {len(names)}")
    print()

    missing: list[str] = []
    for distribution, modules in REQUIRED_MODULES.items():
        absent = [
            module
            for module in modules
            # A directory in `_internal` covers the whole package, so a
            # submodule counts as present when its top-level package shipped
            # that way.
            if module not in names and module.split(".")[0] not in names
        ]
        status = "MISSING" if absent else "ok"
        print(f"  {status:8} {distribution:14} {len(modules)} module(s) required")
        if absent:
            for module in absent:
                print(f"           - {module}")
            missing.extend(absent)

    print()
    for distribution in DECLARED_BUT_UNUSED:
        present = distribution in names
        print(
            f"  {'note':8} {distribution:14} declared in pyproject.toml, imported by nothing"
            + (" - but IS in the bundle" if present else " - correctly absent")
        )

    print()
    if missing:
        print(f"{len(missing)} required module(s) did not survive the freeze.")
        print("This is a packaging defect: fix packaging/omrflow.spec, do not")
        print("document it as a limitation.")
        return 1

    print("Every dependency the application imports is present in the frozen bundle.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
