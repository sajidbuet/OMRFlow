"""The operational GUI renders and controls services; it never re-decides them (revised phase 8).

Executable versions of ``PHASE_H_HANDOFF.md`` §3's rules, read from the
source with :mod:`ast` (nothing is imported), in the style of
``tests/unit/test_architecture.py``:

* no production GUI code calls the low-level session close / reopen
  primitives - every close goes through *Finish scan session*, every reopen
  through ``session_finish.reopen_session`` (via ``gui/session_close.py``);
* the GUI never writes a rejection, a ledger row or a control column itself
  (it cannot import the ORM - ``test_architecture`` - and calls no private
  service helper);
* the caught-up wording and the activity rules stay in the domain: the GUI
  carries no "Caught up" string of its own;
* no combined "overall" session percentage exists in the session GUI;
* the engine runner and the snapshot poller - code that runs in or speaks
  for worker threads - never open a dialog.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "omr_scanner"
GUI = SOURCE_ROOT / "gui"


def _gui_files() -> list[Path]:
    return sorted(GUI.rglob("*.py"))


def _called_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Attribute):
                names.add(target.attr)
            elif isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _string_constants(path: Path) -> list[str]:
    """Every string literal of the module except docstrings (which may explain a rule)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    # Attribute docstrings: a bare string statement after an assignment.
    docstrings |= {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _module_calls(path: Path, module: str) -> set[str]:
    """Functions called as ``module.name(...)`` in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == module
    }


def _imported(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_no_gui_code_calls_the_low_level_close_or_reopen_primitives():
    offenders = [
        f"{path.relative_to(SOURCE_ROOT)}: {name}"
        for path in _gui_files()
        for name in _called_names(path) & {"close_scan_session", "reopen_scan_session"}
    ]
    assert not offenders, (
        "Close through session_finish.finish_scan_session and reopen through "
        f"session_finish.reopen_session (gui/session_close.py): {offenders}"
    )


def test_no_gui_code_writes_lifecycle_ledger_or_controls_directly():
    forbidden = {
        # private helpers of the services that write those tables
        "_move",
        "_new_row",
        "_audit",
        "_write",
        "set_processing_intent",
        "record_decisions_in_session",
        "claim_scans",
        "release_claims",
    }
    offenders = [
        f"{path.relative_to(SOURCE_ROOT)}: {name}"
        for path in _gui_files()
        for name in _called_names(path) & forbidden
    ]
    assert not offenders, offenders


def test_the_gui_carries_no_caught_up_wording_of_its_own():
    # The activity label is the domain's (SessionActivity.label); a GUI string
    # "Caught up ..." would be a second definition of when to say it.
    offenders = [
        f"{path.relative_to(SOURCE_ROOT)}"
        for path in _gui_files()
        if any(text.lower().startswith("caught up") for text in _string_constants(path))
    ]
    assert not offenders, offenders


def test_no_overall_session_percentage_exists():
    session_gui = sorted((GUI / "scan").glob("session_*.py"))
    assert session_gui
    for path in session_gui:
        for text in _string_constants(path):
            lowered = text.lower()
            assert "overall" not in lowered, (path.name, text)
            assert "%p" not in text, (path.name, text)


def test_worker_side_gui_modules_open_no_dialog():
    for name in ("session_runner.py", "session_poller.py"):
        path = GUI / "scan" / name
        imported = _imported(path)
        assert not any(
            item.endswith(("QMessageBox", "QDialog", "QInputDialog", "QFileDialog"))
            for item in imported
        ), (name, sorted(imported))
        assert not {"exec", "exec_"} & _called_names(path), name


def test_session_close_is_the_only_gui_caller_of_the_finish_and_reopen_services():
    # Closing and reopening go through one adapter; the read-only preview
    # (`finish_blockers`) may be listed anywhere, as Reports does.
    callers = sorted(
        str(path.relative_to(SOURCE_ROOT))
        for path in _gui_files()
        if _module_calls(path, "session_finish") & {"finish_scan_session", "reopen_session"}
    )
    assert callers == [str(Path("gui") / "session_close.py")], callers
