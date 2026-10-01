"""Architecture guard X11: set codes are compared only through set identity.

Why this test exists:
    ``0.1.0-alpha.2`` shipped a defect (ARCHITECTURE_NOTES.md §3, defect 5)
    because one layer compared set codes exactly and another upper-cased one
    side: a lower-case set could not find its answer key. Phase 0.1.1-A routes
    every comparison through :mod:`omr_scanner.domain.set_identity`
    (``canonical_code``, ``same_set``, ``SetCodeMap``, ``SetIdentity``). A
    single new ``row.set_code == code`` would quietly bring the defect back,
    so this test reads the business-logic layers and refuses one.

What it forbids, in ``services/``, ``domain/`` and ``database/``:
    * ``==`` / ``!=`` / ``in`` / ``not in`` where either operand is a name or
      attribute that *is* a set code - its final identifier is ``set_code``,
      ``set_codes``, ``set_code_value``, ``declared_set_code``,
      ``recognised_set_code``, ``declared_set``, ``wanted_set``,
      ``machine_set_code`` or ``printed_as`` / ``physical_mark`` - or is an
      attribute ``.code`` (a defined set's code).
    * ``.upper()``, ``.lower()`` or ``.casefold()`` called on such a value:
      that is a private canonicalisation rule competing with the real one.

What it permits:
    * Everything inside ``domain/set_identity.py`` and
      ``services/set_identity.py`` - the rule has to be written somewhere.
    * A comparison against the empty string, which asks "is there a code at
      all", not "which set is this".
    * A line carrying the pragma ``# set-identity: exact`` (on it or on the
      comment line directly above), for the rare comparison that genuinely
      means *the same spelling* - "did the operator leave the code unchanged",
      "prefer the row stored under this exact spelling". Each one is a
      deliberate, reviewable decision.
    * Persistence, serialisation and display - assigning, storing, logging or
      formatting a set code is not a comparison and is not inspected.
    * The GUI, evaluation (synthetic data generation) and tools layers, which
      do not decide set identity; the GUI calls the services for it.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "omr_scanner"

GUARDED_LAYERS = ("services", "domain", "database")

ALLOWED_FILES = frozenset(
    {
        Path("domain") / "set_identity.py",
        Path("services") / "set_identity.py",
    }
)

SET_CODE_NAMES = frozenset(
    {
        "set_code",
        "set_codes",
        "set_code_value",
        "declared_set_code",
        "recognised_set_code",
        "declared_set",
        "wanted_set",
        "machine_set_code",
        "printed_as",
        "physical_mark",
    }
)

CASE_FOLDING = frozenset({"upper", "lower", "casefold"})

PRAGMA = "set-identity: exact"


def _final_name(node: ast.AST) -> str | None:
    """The identifier a name or attribute ends in, through ``.strip()`` calls."""
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr != "strip":
            break
        node = node.func.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_set_code(node: ast.AST) -> bool:
    """Whether ``node`` is a set code.

    Also ``<anything>.code`` - an :class:`~omr_scanner.domain.exam_sets.ExamSet`'s
    or a ``project_set`` row's code - but not a bare name ``code``, which in
    these layers is as often a warning, status or reason code.
    """
    name = _final_name(node)
    if name in SET_CODE_NAMES:
        return True
    return name == "code" and isinstance(node, ast.Attribute)


def _is_empty_string(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == ""


def _excused(lines: list[str], lineno: int) -> bool:
    here = lines[lineno - 1]
    above = lines[lineno - 2] if lineno >= 2 else ""
    return PRAGMA in here or (above.strip().startswith("#") and PRAGMA in above)


def _violations(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    tree = ast.parse(text, filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            for op, left, right in zip(node.ops, operands, operands[1:], strict=False):
                if not isinstance(op, ast.Eq | ast.NotEq | ast.In | ast.NotIn):
                    continue
                if not (_is_set_code(left) or _is_set_code(right)):
                    continue
                if _is_empty_string(left) or _is_empty_string(right):
                    continue
                if _excused(lines, node.lineno):
                    continue
                found.append(f"{node.lineno}: {ast.unparse(node)}")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in CASE_FOLDING
            and _is_set_code(node.func.value)
            and not _excused(lines, node.lineno)
        ):
            found.append(f"{node.lineno}: {ast.unparse(node)}")
    return found


def _guarded_files() -> list[Path]:
    files: list[Path] = []
    for layer in GUARDED_LAYERS:
        files.extend(sorted((SOURCE_ROOT / layer).rglob("*.py")))
    return [path for path in files if path.relative_to(SOURCE_ROOT) not in ALLOWED_FILES]


def test_the_guarded_layers_exist() -> None:
    assert _guarded_files()
    for allowed in ALLOWED_FILES:
        assert (SOURCE_ROOT / allowed).is_file(), allowed


def test_set_codes_are_compared_only_through_set_identity() -> None:
    offenders = [
        f"{path.relative_to(SOURCE_ROOT)}:{line}"
        for path in _guarded_files()
        for line in _violations(path)
    ]
    assert not offenders, (
        "Set codes compared outside omr_scanner.domain/services.set_identity "
        "(use same_set / canonical_code / SetCodeMap / SetIdentity, or mark a "
        "deliberate exact-spelling comparison with '# set-identity: exact'):\n  "
        + "\n  ".join(offenders)
    )


def test_the_guard_catches_what_it_is_meant_to(tmp_path: Path) -> None:
    """The detector itself: a guard that never fires proves nothing."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def f(row, set_code, codes):\n"
        "    a = row.set_code == set_code\n"
        "    b = set_code in codes\n"
        "    c = set_code.strip().upper()\n"
        "    d = row.set_code != ''\n"
        "    # set-identity: exact (unchanged spelling)\n"
        "    e = row.set_code == set_code\n"
        "    f = row.code == set_code  # set-identity: exact\n"
        "    g = item.code == wanted\n"
        "    h = code in WARNING_CODES\n"
        "    return a, b, c, d, e, f, g, h\n",
        encoding="utf-8",
    )
    found = _violations(sample)
    assert [item.split(":", 1)[0] for item in found] == ["2", "3", "4", "9"]
