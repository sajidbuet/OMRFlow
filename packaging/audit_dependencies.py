"""Audit which DLLs a packaged build imports, and where each resolves from.

Purpose:
    Answer the central clean-machine question - *did the build forget to
    bundle something?* - without needing a clean machine. Every binary in the
    bundle is parsed for its PE import table, and each imported DLL is
    classified as bundled, satisfied by Windows itself, or **unresolved**.

    An unresolved import is a build that works for whoever built it and fails
    on a fresh machine, which is the exact defect a clean-machine test
    exists to find.

What this does and does not prove:
    It proves that every DLL named in an import table is either inside the
    bundle or part of Windows. It does **not** prove the build is
    self-contained: a dependency loaded at run time (``ctypes``,
    ``LoadLibrary``, a Qt plugin discovered by path) appears in no import
    table, and a DLL present in ``System32`` on this machine because a
    developer tool installed it there is indistinguishable here from one
    Windows ships. Those gaps are why the real test still has to be run - see
    ``docs/release/CLEAN_MACHINE_TEST.md``.

Usage:
    python packaging/audit_dependencies.py dist/OMRFlow
"""

from __future__ import annotations

import struct
import sys
from collections import defaultdict
from pathlib import Path

BINARY_SUFFIXES = {".exe", ".dll", ".pyd"}

WINDOWS_DIRECTORIES = (
    Path(r"C:\Windows\System32"),
    Path(r"C:\Windows\SysWOW64"),
    Path(r"C:\Windows"),
)

RUNTIME_PREFIXES = (
    "vcruntime",
    "msvcp",
    "msvcr",
    "concrt",
    "ucrtbase",
    "vcomp",
)
"""The Microsoft C and C++ runtimes.

Called out separately because they are the classic clean-machine failure:
present on any machine with Visual Studio or the build tools, absent on a
fresh install unless the application ships them. A build that resolves these
from ``System32`` rather than from its own directory is the single most
likely thing to fail on a tester's computer.
"""

OS_RUNTIME_COMPONENTS = frozenset({"msvcrt.dll"})
"""Runtime-looking DLLs that are in fact part of Windows.

``msvcrt.dll`` matches :data:`RUNTIME_PREFIXES` but is the legacy C runtime
that Windows itself ships in ``System32`` and that applications are
explicitly *not* allowed to redistribute. Resolving it from Windows is
correct, so it must not be reported as a risk - otherwise the audit cries
wolf on every build and stops being read.
"""

API_SET_PREFIXES = ("api-ms-win-", "ext-ms-win-")
"""Windows API sets. Virtual DLLs resolved by the loader; always present."""


def _rva_to_offset(rva: int, sections: list[tuple[int, int, int, int]]) -> int | None:
    for virtual_address, virtual_size, raw_pointer, raw_size in sections:
        span = max(virtual_size, raw_size)
        if virtual_address <= rva < virtual_address + span:
            return raw_pointer + (rva - virtual_address)
    return None


def _read_cstring(data: bytes, offset: int) -> str:
    end = data.find(b"\0", offset)
    if end < 0:
        return ""
    return data[offset:end].decode("ascii", "replace")


def imported_dlls(path: Path) -> set[str]:
    """The DLL names in ``path``'s PE import and delay-import tables.

    Returns an empty set for anything that is not a parseable PE image,
    rather than raising: the bundle legitimately contains data files.
    """
    try:
        data = path.read_bytes()
    except OSError:  # pragma: no cover - unreadable file
        return set()

    if len(data) < 0x40 or data[:2] != b"MZ":
        return set()
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        return set()

    coff = pe_offset + 4
    section_count = struct.unpack_from("<H", data, coff + 2)[0]
    optional_size = struct.unpack_from("<H", data, coff + 16)[0]
    optional = coff + 20
    if optional + 2 > len(data):
        return set()

    magic = struct.unpack_from("<H", data, optional)[0]
    if magic == 0x10B:
        directories = optional + 96
    elif magic == 0x20B:
        directories = optional + 112
    else:
        return set()

    section_table = optional + optional_size
    sections: list[tuple[int, int, int, int]] = []
    for index in range(section_count):
        header = section_table + index * 40
        if header + 40 > len(data):
            break
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from(
            "<IIII", data, header + 8
        )
        sections.append((virtual_address, virtual_size, raw_pointer, raw_size))

    names: set[str] = set()

    # Directory 1 is the import table; directory 13 is the delay-import
    # table, which Qt and the C runtime both use.
    for directory_index, descriptor_size, name_field in ((1, 20, 12), (13, 32, 4)):
        entry = directories + directory_index * 8
        if entry + 8 > len(data):
            continue
        table_rva = struct.unpack_from("<I", data, entry)[0]
        if not table_rva:
            continue
        table = _rva_to_offset(table_rva, sections)
        if table is None:
            continue
        for index in range(4096):
            descriptor = table + index * descriptor_size
            if descriptor + descriptor_size > len(data):
                break
            chunk = data[descriptor : descriptor + descriptor_size]
            if not any(chunk):
                break
            name_rva = struct.unpack_from("<I", data, descriptor + name_field)[0]
            if not name_rva:
                continue
            name_offset = _rva_to_offset(name_rva, sections)
            if name_offset is None or name_offset >= len(data):
                continue
            name = _read_cstring(data, name_offset)
            if name:
                names.add(name.lower())
    return names


def classify(name: str, bundled: set[str]) -> str:
    """Where ``name`` resolves from: ``bundled``, ``windows``, or ``MISSING``."""
    if name in bundled:
        return "bundled"
    if name.startswith(API_SET_PREFIXES):
        return "windows"
    for directory in WINDOWS_DIRECTORIES:
        if (directory / name).is_file():
            return "windows"
    return "MISSING"


def main(argv: list[str] | None = None) -> int:
    """Audit the bundle named on the command line."""
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: audit_dependencies.py <bundle-directory>", file=sys.stderr)
        return 2

    bundle = Path(arguments[0]).resolve()
    if not bundle.is_dir():
        print(f"No such bundle directory: {bundle}", file=sys.stderr)
        return 2

    binaries = sorted(
        path
        for path in bundle.rglob("*")
        if path.is_file() and path.suffix.lower() in BINARY_SUFFIXES
    )
    bundled = {path.name.lower() for path in binaries}

    print(f"Bundle : {bundle}")
    print(f"Binaries: {len(binaries)}")
    print()

    importers: defaultdict[str, list[str]] = defaultdict(list)
    for path in binaries:
        for name in imported_dlls(path):
            importers[name].append(path.relative_to(bundle).as_posix())

    buckets: defaultdict[str, list[str]] = defaultdict(list)
    for name in sorted(importers):
        buckets[classify(name, bundled)].append(name)

    print(f"Distinct imported DLLs: {len(importers)}")
    print(f"  satisfied from the bundle : {len(buckets['bundled'])}")
    print(f"  satisfied by Windows      : {len(buckets['windows'])}")
    print(f"  UNRESOLVED                : {len(buckets['MISSING'])}")
    print()

    runtimes = [
        name
        for name in importers
        if name.startswith(RUNTIME_PREFIXES) and name not in OS_RUNTIME_COMPONENTS
    ]
    print("Microsoft C/C++ runtime - the classic clean-machine failure:")
    if not runtimes:
        print("  none imported")
    for name in sorted(runtimes):
        where = classify(name, bundled)
        mark = "OK  " if where == "bundled" else "RISK"
        print(f"  {mark} {name:<48} resolves from: {where}")
    for name in sorted(set(importers) & OS_RUNTIME_COMPONENTS):
        print(f"  OK   {name:<48} part of Windows, not redistributable")
    print()

    if buckets["MISSING"]:
        print("UNRESOLVED imports - these would fail on a machine without them:")
        for name in buckets["MISSING"]:
            print(f"  {name}")
            for importer in sorted(importers[name])[:4]:
                print(f"      imported by {importer}")
        print()

    non_api_windows = [
        name
        for name in buckets["windows"]
        if not name.startswith(API_SET_PREFIXES)
    ]
    print(f"Resolved from Windows, excluding API sets ({len(non_api_windows)}):")
    for name in non_api_windows:
        print(f"  {name}")
    print()

    problems = len(buckets["MISSING"]) + sum(
        1 for name in runtimes if classify(name, bundled) != "bundled"
    )
    if problems:
        print(f"{problems} issue(s) found.")
        return 1
    print("No unresolved imports, and the C/C++ runtime is bundled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
