# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for the OMRFlow Windows application bundle.

Produces a one-directory build (`dist/OMRFlow/`) rather than a single file.
One-file unpacks the whole of Qt and OpenCV to a temporary directory on every
launch, which costs seconds of start-up and confuses antivirus software; a
directory is also what an installer wants to lay down.

Run through `scripts/release/Build-App.ps1`, which sets the version
environment variables this file reads. Building it directly still works, but
the executable's Windows metadata will then say "unknown".

What this file is responsible for:
  * finding the application's own bundled resources (icons, branding), which
    are loaded through `importlib.resources` at runtime and so are invisible
    to PyInstaller's import analysis;
  * excluding the development-only dependency tree, which otherwise adds
    over a hundred megabytes of test and build tooling to a user download;
  * stamping the executable with the version.
"""

from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

SPEC_DIR = Path(SPECPATH).resolve()
PROJECT_ROOT = SPEC_DIR.parent
PACKAGE_ROOT = PROJECT_ROOT / "src" / "omr_scanner"

APP_NAME = "OMRFlow"
ICON = PACKAGE_ROOT / "gui" / "resources" / "branding" / "icon.ico"
VERSION_RESOURCE = SPEC_DIR / "build" / "file_version_info.txt"

# The GUI resolves icons and branding through `importlib.resources` against
# the installed package, so nothing imports these paths and PyInstaller's
# analysis cannot see them. Collected explicitly, preserving the package
# layout `omr_scanner.gui.icons` expects to find them in.
datas = collect_data_files(
    "omr_scanner",
    includes=[
        "gui/resources/icons/lucide/*.svg",
        "gui/resources/icons/lucide/LICENSE",
        "gui/resources/branding/*",
        "resources/**/*",
    ],
)

# Everything a developer needs and a user does not. Left in, these add well
# over a hundred megabytes to the download and pull test frameworks into a
# shipped application.
excludes = [
    "pytest",
    "pytest_qt",
    "pytest_cov",
    "_pytest",
    "mypy",
    "ruff",
    "PyInstaller",
    "setuptools",
    "pip",
    "wheel",
    "build",
    "tkinter",
    "matplotlib",
    "IPython",
    "jupyter",
    "notebook",
    "sphinx",
    "pydoc_data",
    # Qt modules OMRFlow does not use. PySide6 is by far the largest single
    # contributor to the bundle, and these are its heaviest unused parts.
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtSerialPort",
    "PySide6.QtSensors",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtSql",
    "PySide6.QtOpenGLFunctions",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSpatialAudio",
    "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
]

analysis = Analysis(
    [str(PACKAGE_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT / "src")],
    binaries=[],
    datas=datas,
    # `omr_scanner.tools.*` are reached by `python -m`, never imported by the
    # GUI, so the analysis would drop them - and with them the qualification
    # harness a tester may be asked to run.
    hiddenimports=[
        "omr_scanner.tools.benchmark_stress",
        "omr_scanner.tools.phase10_qualification",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # A desktop application: no console window behind the GUI. Diagnostics go
    # to the per-user log file, which `SUPPORT.md` explains how to find.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.is_file() else None,
    version=str(VERSION_RESOURCE) if VERSION_RESOURCE.is_file() else None,
)

collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
