"""The release-gate evaluator: a verdict computed, never typed (revised phase 10)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tools.release_validation import release_gate


def _matrix(tmp_path: Path, **changes: object) -> Path:
    gates = [
        {"gate": gate, "title": f"gate {gate}", "required": True, "status": "PASS",
         "evidence": ["README.md"]}
        for gate in release_gate.REQUIRED_GATES
    ]
    gates.append({"gate": "x1", "title": "real scanning room", "required": False,
                  "status": "NOT PERFORMED", "evidence": []})
    for gate, value in changes.items():
        for item in gates:
            if item["gate"] == gate.replace("g", ""):
                item.update(value)  # type: ignore[arg-type]
    path = tmp_path / "release-gate.json"
    path.write_text(json.dumps({"version": "0.1.1-alpha.0", "gates": gates}), "utf-8")
    return path


def test_all_required_gates_passing_is_ready(tmp_path):
    _data, gates = release_gate.load(_matrix(tmp_path))
    assert release_gate.verdict(gates) == (release_gate.READY, [])


def test_one_required_gate_not_performed_blocks(tmp_path):
    _data, gates = release_gate.load(_matrix(tmp_path, g3={"status": "NOT PERFORMED"}))
    decided, blockers = release_gate.verdict(gates)
    assert decided == release_gate.NOT_READY
    assert [gate.gate for gate in blockers] == ["3"]


def test_an_optional_gate_never_blocks(tmp_path):
    _data, gates = release_gate.load(_matrix(tmp_path))
    assert any(not gate.required and gate.status == "NOT PERFORMED" for gate in gates)
    assert release_gate.verdict(gates)[0] == release_gate.READY


@pytest.mark.parametrize("change", [
    {"status": "WARNING"},          # a failure is never softened into a warning
    {"status": "PASS", "evidence": []},
    {"evidence": ["no/such/evidence.md"]},
    {"required": False},           # a §8 gate cannot be made optional
])
def test_a_defective_matrix_is_refused(tmp_path, change):
    with pytest.raises(release_gate.MatrixError):
        release_gate.load(_matrix(tmp_path, g4a=change))


def test_a_missing_gate_is_refused(tmp_path):
    path = _matrix(tmp_path)
    data = json.loads(path.read_text("utf-8"))
    data["gates"] = [item for item in data["gates"] if item["gate"] != "6"]
    path.write_text(json.dumps(data), "utf-8")
    with pytest.raises(release_gate.MatrixError, match="leaves out"):
        release_gate.load(path)


def test_the_committed_matrix_is_valid_and_its_verdict_is_computed():
    if not release_gate.MATRIX.is_file():
        pytest.skip("the committed matrix is written at the end of revised phase 10")
    data, gates = release_gate.load()
    decided, blockers = release_gate.verdict(gates)
    text = release_gate.render_markdown(data, gates)
    assert decided in text
    assert decided == (release_gate.READY if not blockers else release_gate.NOT_READY)


def test_the_command_line_exit_codes(tmp_path):
    assert release_gate.main(["--matrix", str(_matrix(tmp_path))]) == 0
    blocked = _matrix(tmp_path, g3={"status": "NOT PERFORMED"})
    out = tmp_path / "gate.md"
    assert release_gate.main(["--matrix", str(blocked), "--markdown", str(out)]) == 1
    assert release_gate.NOT_READY in out.read_text("utf-8")
    broken = _matrix(tmp_path, g5={"status": "WARNING"})
    assert release_gate.main(["--matrix", str(broken)]) == 2
