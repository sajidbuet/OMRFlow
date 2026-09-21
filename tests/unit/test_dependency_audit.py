"""Tests for the packaged build's dependency audit.

Scope:
    ``packaging/audit_dependencies.py`` answers the central clean-machine
    question - *did the build forget to bundle something?* - and a release is
    gated on it. If it silently under-reports, a build that cannot start on a
    tester's computer ships with a green tick beside it, which is worse than
    having no audit at all.

    ===== ==========================================================
    Test  Behaviour
    ===== ==========================================================
    A     The PE parser reads real import tables, and rejects non-PE input.
    B     Classification: bundled, satisfied by Windows, or unresolved.
    C     The Microsoft runtime is distinguished from Windows' own copy.
    D     The exit code reflects whether a release should proceed.
    ===== ==========================================================

Why the module is loaded by path:
    ``packaging/`` is build tooling, not part of the installed package, and
    the name is already taken on ``sys.path`` by the PyPI ``packaging``
    distribution that :mod:`tests.unit.test_version` uses. Importing by file
    location keeps both reachable and keeps the audit script free of a
    package structure it does not otherwise need.
"""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AUDIT_SCRIPT = REPOSITORY_ROOT / "packaging" / "audit_dependencies.py"


def _load_audit_module() -> Any:
    specification = importlib.util.spec_from_file_location(
        "omrflow_audit_dependencies", AUDIT_SCRIPT
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def audit() -> Any:
    """The audit script, loaded from its path in the repository."""
    if not AUDIT_SCRIPT.is_file():
        pytest.fail(f"The dependency audit is missing: {AUDIT_SCRIPT}")
    return _load_audit_module()


# --------------------------------------------------------------------------
# A. The PE parser
# --------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="PE imports are a Windows concern")
def test_it_reads_the_import_table_of_a_real_executable(audit: Any) -> None:
    """The running interpreter is a PE image, and every one imports kernel32.

    A parser that returns nothing would make every audit pass, so the useful
    assertion is that it finds something known-present rather than merely
    that it does not raise.
    """
    names = audit.imported_dlls(Path(sys.executable))

    assert names, f"No imports found in {sys.executable}; the parser read nothing"
    assert any(
        name.startswith("kernel32") or name.startswith("api-ms-win-core")
        for name in names
    ), f"Expected the Windows core API among {sorted(names)}"


def test_a_file_that_is_not_a_pe_image_yields_no_imports(
    audit: Any, tmp_path: Path
) -> None:
    """A bundle legitimately contains data files; they must not raise."""
    text = tmp_path / "template.json"
    text.write_text('{"not": "a pe image"}', encoding="utf-8")

    assert audit.imported_dlls(text) == set()


def test_a_truncated_pe_header_yields_no_imports(
    audit: Any, tmp_path: Path
) -> None:
    """Refuse to read past the end of a file that only starts like a PE."""
    truncated = tmp_path / "truncated.dll"
    truncated.write_bytes(b"MZ" + b"\0" * 0x3E + struct.pack("<I", 0xFFFF))

    assert audit.imported_dlls(truncated) == set()


def test_a_missing_file_yields_no_imports(audit: Any, tmp_path: Path) -> None:
    assert audit.imported_dlls(tmp_path / "absent.dll") == set()


# --------------------------------------------------------------------------
# B. Classification
# --------------------------------------------------------------------------


def test_a_bundled_dll_is_reported_as_bundled(audit: Any) -> None:
    assert audit.classify("qt6core.dll", {"qt6core.dll"}) == "bundled"


def test_an_api_set_is_satisfied_by_windows(audit: Any) -> None:
    """API sets are virtual: the loader resolves them and no file exists."""
    assert audit.classify("api-ms-win-core-file-l1-1-0.dll", set()) == "windows"
    assert audit.classify("ext-ms-win-ntuser-window-l1-1-0.dll", set()) == "windows"


@pytest.mark.skipif(sys.platform != "win32", reason="looks in the Windows directories")
def test_a_windows_dll_is_found_outside_the_bundle(audit: Any) -> None:
    assert audit.classify("kernel32.dll", set()) == "windows"


def test_an_unknown_dll_is_reported_as_missing(audit: Any) -> None:
    """The case the audit exists to find: named, not bundled, not on Windows."""
    assert audit.classify("libsomethingnobodyships-99.dll", set()) == "MISSING"


def test_the_bundle_takes_precedence_over_windows(audit: Any) -> None:
    """A bundled copy is what the loader will use, so report it as bundled.

    This matters for the C++ runtime specifically: `msvcp140.dll` exists in
    System32 on a development machine *and* in the bundle, and reporting the
    system one would invert the result that the release depends on.
    """
    assert audit.classify("msvcp140.dll", {"msvcp140.dll"}) == "bundled"


# --------------------------------------------------------------------------
# C. The Microsoft runtime, and Windows' own copy of it
# --------------------------------------------------------------------------


def test_the_redistributable_runtime_is_recognised(audit: Any) -> None:
    """These are what a fresh machine lacks unless the build ships them."""
    for name in (
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "msvcp140.dll",
        "msvcp140_1.dll",
        "concrt140.dll",
        "vcomp140.dll",
        "ucrtbase.dll",
    ):
        assert name.startswith(audit.RUNTIME_PREFIXES), f"{name} not recognised"


def test_msvcrt_is_not_treated_as_a_redistributable(audit: Any) -> None:
    """`msvcrt.dll` is part of Windows and must not be shipped.

    It matches the runtime prefixes, so without this exclusion the audit
    reports a RISK on every build. An audit that always complains is one
    nobody reads, which is how a real unresolved import gets missed.
    """
    assert "msvcrt.dll".startswith(audit.RUNTIME_PREFIXES)
    assert "msvcrt.dll" in audit.OS_RUNTIME_COMPONENTS


def test_the_excluded_os_components_are_genuinely_part_of_windows(
    audit: Any,
) -> None:
    """Nothing may be excused from the audit unless Windows really ships it.

    The exclusion list is a hole in the audit, so it is asserted to be a hole
    only where Windows itself fills it.
    """
    for name in audit.OS_RUNTIME_COMPONENTS:
        if sys.platform == "win32":
            assert any(
                (directory / name).is_file() for directory in audit.WINDOWS_DIRECTORIES
            ), f"{name} is excused from the audit but is not present in Windows"


# --------------------------------------------------------------------------
# D. The exit code - what a release is gated on
# --------------------------------------------------------------------------


def test_a_bundle_with_an_unresolved_import_fails(
    audit: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: a build missing a dependency must not report success.

    A synthetic bundle is used rather than the real one, because the real
    one passing proves nothing about whether failure is detected.
    """
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "app.exe").write_bytes(b"MZ" + b"\0" * 64)

    monkeypatch.setattr(audit, "imported_dlls", lambda _path: {"nowhere-at-all.dll"})
    exit_code = audit.main([str(bundle)])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "UNRESOLVED" in output
    assert "nowhere-at-all.dll" in output


def test_a_bundle_whose_imports_all_resolve_passes(
    audit: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "app.exe").write_bytes(b"MZ" + b"\0" * 64)
    (bundle / "helper.dll").write_bytes(b"MZ" + b"\0" * 64)

    monkeypatch.setattr(audit, "imported_dlls", lambda _path: {"helper.dll"})
    exit_code = audit.main([str(bundle)])

    assert exit_code == 0
    assert "No unresolved imports" in capsys.readouterr().out


def test_a_runtime_taken_from_the_system_fails(
    audit: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing the C++ runtime without bundling it is release-blocking.

    This is the defect that works perfectly on the machine that built it.
    """
    if sys.platform != "win32":
        pytest.skip("depends on the runtime being present in the Windows directories")
    system_runtime = next(
        (
            name
            for name in ("msvcp140.dll", "vcruntime140.dll")
            if any(
                (directory / name).is_file() for directory in audit.WINDOWS_DIRECTORIES
            )
        ),
        None,
    )
    if system_runtime is None:
        pytest.skip("no VC++ runtime in System32 on this machine to borrow")

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "app.exe").write_bytes(b"MZ" + b"\0" * 64)

    monkeypatch.setattr(audit, "imported_dlls", lambda _path: {system_runtime})
    exit_code = audit.main([str(bundle)])

    assert exit_code == 1, "A runtime resolved from System32 must not pass"
    assert "RISK" in capsys.readouterr().out


def test_msvcrt_alone_does_not_fail_the_audit(
    audit: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The false positive that made the audit cry wolf, pinned closed."""
    if sys.platform != "win32":
        pytest.skip("msvcrt.dll resolution is a Windows concern")

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "app.exe").write_bytes(b"MZ" + b"\0" * 64)

    monkeypatch.setattr(audit, "imported_dlls", lambda _path: {"msvcrt.dll"})
    exit_code = audit.main([str(bundle)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "RISK" not in output
    assert "not redistributable" in output


def test_a_missing_argument_is_a_usage_error(audit: Any) -> None:
    assert audit.main([]) == 2
    assert audit.main(["one", "two"]) == 2


def test_a_nonexistent_bundle_is_an_error(audit: Any, tmp_path: Path) -> None:
    assert audit.main([str(tmp_path / "absent")]) == 2
