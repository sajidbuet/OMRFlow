r"""The ``0.1.1-alpha.0`` release gate, evaluated from a committed matrix (revised phase 10).

``ACCEPTANCE_CRITERIA.md`` §8 lists what must all hold before the Alpha may be
published. This module reads the machine-readable matrix
(``docs/release/validation/0.1.1-alpha.0-release-gate/release-gate.json``),
refuses a matrix that leaves out a gate, uses a status other than ``PASS`` /
``FAIL`` / ``NOT PERFORMED``, or cites evidence that is not in the repository,
and computes the verdict mechanically:

* ``READY FOR OWNER RELEASE APPROVAL`` - every gate required for the Alpha is
  ``PASS``;
* ``NOT READY FOR RELEASE — BLOCKERS REMAIN`` - otherwise, naming each blocker.

It never says "released": publication is the owner's act, outside this tool.

Usage::

    .venv\Scripts\python.exe -m tools.release_validation.release_gate            # verdict + table
    .venv\Scripts\python.exe -m tools.release_validation.release_gate --markdown out.md
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MATRIX = (REPOSITORY_ROOT / "docs" / "release" / "validation" / "0.1.1-alpha.0-release-gate"
          / "release-gate.json")

STATUSES = ("PASS", "FAIL", "NOT PERFORMED")
REQUIRED_GATES = ("1", "2", "3", "4", "4a", "5", "6", "7", "8", "9")
"""``ACCEPTANCE_CRITERIA.md`` §8, every item."""

READY = "READY FOR OWNER RELEASE APPROVAL"
NOT_READY = "NOT READY FOR RELEASE — BLOCKERS REMAIN"


class MatrixError(ValueError):
    """The matrix itself is incomplete or malformed - never a verdict."""


@dataclass(frozen=True, slots=True)
class Gate:
    """One row of the matrix."""

    gate: str
    title: str
    required: bool
    status: str
    evidence: tuple[str, ...]
    note: str


def load(path: Path = MATRIX) -> tuple[dict[str, Any], list[Gate]]:
    """Read and validate the matrix; raise :class:`MatrixError` for a defective one."""
    data = json.loads(path.read_text(encoding="utf-8"))
    gates: list[Gate] = []
    for item in data.get("gates", []):
        status = str(item.get("status", ""))
        if status not in STATUSES:
            raise MatrixError(f"gate {item.get('gate')}: status {status!r} not in {STATUSES}")
        gates.append(Gate(gate=str(item["gate"]), title=str(item["title"]),
                          required=bool(item["required"]), status=status,
                          evidence=tuple(item.get("evidence", ())),
                          note=str(item.get("note", ""))))
    listed = [gate.gate for gate in gates]
    missing = [gate for gate in REQUIRED_GATES if gate not in listed]
    if missing:
        raise MatrixError(f"the matrix leaves out §8 gate(s) {missing}")
    if len(set(listed)) != len(listed):
        raise MatrixError("a gate is listed twice")
    for gate in gates:
        if gate.gate in REQUIRED_GATES and not gate.required:
            raise MatrixError(f"§8 gate {gate.gate} must be required for the Alpha")
        if gate.status == "PASS" and not gate.evidence:
            raise MatrixError(f"gate {gate.gate} passes with no evidence cited")
        for reference in gate.evidence:
            target = REPOSITORY_ROOT / reference.split("#", 1)[0]
            if not target.exists():
                raise MatrixError(f"gate {gate.gate}: evidence {reference!r} is not in the "
                                  "repository")
    return data, gates


def verdict(gates: list[Gate]) -> tuple[str, list[Gate]]:
    """The release verdict and the blocking gates."""
    blockers = [gate for gate in gates if gate.required and gate.status != "PASS"]
    return (READY if not blockers else NOT_READY), blockers


def render_markdown(data: dict[str, Any], gates: list[Gate]) -> str:
    """The human-readable matrix with the computed verdict."""
    decided, blockers = verdict(gates)
    lines = [
        f"# `{data.get('version', '0.1.1-alpha.0')}` release gate",
        "",
        f"Evaluated by `tools/release_validation/release_gate.py` from `release-gate.json` "
        f"(candidate commit `{data.get('candidate_commit', '?')}`, recorded "
        f"{data.get('recorded', '?')}).",
        "",
        f"**Verdict: {decided}**",
        "",
    ]
    if blockers:
        lines += ["Blocking:", ""] + [f"- gate {gate.gate} - {gate.title}: **{gate.status}**"
                                      for gate in blockers] + [""]
    lines += ["| Gate | Required for alpha.0 | Status | Evidence | Note |", "|---|---|---|---|---|"]
    for gate in gates:
        evidence = "<br>".join(f"`{item}`" for item in gate.evidence) or "-"
        lines.append(f"| {gate.gate} {gate.title} | {'Yes' if gate.required else 'No'} | "
                     f"**{gate.status}** | {evidence} | {gate.note.replace('|', '/')} |")
    lines += ["", "No tag or release is created by this evaluation; publication is the "
                  "owner's decision.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Print the verdict; ``0`` when ready, ``1`` when blockers remain, ``2`` for a bad matrix."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--matrix", type=Path, default=MATRIX)
    parser.add_argument("--markdown", type=Path, help="also write the rendered matrix here")
    arguments = parser.parse_args(argv)
    try:
        data, gates = load(arguments.matrix)
    except (MatrixError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"defective release-gate matrix: {exc}", file=sys.stderr)
        return 2
    decided, blockers = verdict(gates)
    print(decided)
    for gate in gates:
        print(f"  {gate.status:14s} {'required' if gate.required else 'optional':9s} "
              f"{gate.gate:3s} {gate.title}")
    if arguments.markdown:
        arguments.markdown.write_text(render_markdown(data, gates), encoding="utf-8")
    return 0 if not blockers else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
