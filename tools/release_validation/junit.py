"""Turn a pytest JUnit XML file into the framework's own result rows.

Why go through JUnit XML rather than calling pytest in-process:
    The GUI, accessibility, workflow and visual suites are ordinary pytest
    files, run in a subprocess so that a Qt crash or a C-level fault takes down
    the suite rather than the whole qualification run. JUnit XML is what comes
    back, it is pytest's own record of what happened, and parsing it means the
    report cannot disagree with what pytest actually did.

What a reader gets from this:
    One row per test, carrying the test's name, how long it took and - when it
    failed - pytest's own message. Not a summary line saying "12 failed".
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from pathlib import Path

from tools.release_validation.results import CheckResult, Status


def _readable(classname: str, name: str) -> str:
    """``tests.gui.test_x`` + ``test_the_window_opens`` -> a readable name.

    pytest's underscored test names are already sentences; turning them back
    into one and dropping the module path makes the console summary legible
    without losing which suite it came from (the stage already says that).
    """
    module = classname.rsplit(".", 1)[-1] if classname else ""
    readable = name.removeprefix("test_").replace("_", " ").strip()
    return f"{readable} [{module}]" if module else readable


def parse_junit(path: Path, *, artifact: str | None = None) -> list[CheckResult]:
    """Read ``path`` and return one :class:`CheckResult` per test case.

    A missing or unparsable file returns a single failing row rather than an
    empty list: "the suite produced no results" must never look like "the suite
    had nothing to do".
    """
    if not path.is_file():
        return [
            CheckResult(
                name="pytest produced a JUnit report",
                status=Status.FAIL,
                reason=f"no JUnit XML at {path}; the suite did not run to completion",
            )
        ]
    try:
        tree = ElementTree.parse(path)
    except ElementTree.ParseError as error:
        return [
            CheckResult(
                name="pytest JUnit report is readable",
                status=Status.FAIL,
                reason=f"{path.name} is not valid XML: {error}",
            )
        ]

    artifacts = [artifact] if artifact else []
    results: list[CheckResult] = []
    for case in tree.iter("testcase"):
        name = _readable(case.get("classname", ""), case.get("name", "?"))
        try:
            duration = float(case.get("time", "0") or 0)
        except ValueError:
            duration = 0.0

        failure = case.find("failure")
        error = case.find("error")
        skipped = case.find("skipped")

        if failure is not None or error is not None:
            node = failure if failure is not None else error
            assert node is not None
            message = (node.get("message") or "").strip()
            body = (node.text or "").strip()
            results.append(
                CheckResult(
                    name=name,
                    status=Status.FAIL,
                    duration_seconds=duration,
                    reason=message or "test failed",
                    # The tail, not the head: pytest puts the assertion and the
                    # values that produced it at the end of the traceback.
                    exception=body[-4000:],
                    artifacts=list(artifacts),
                )
            )
        elif skipped is not None:
            results.append(
                CheckResult(
                    name=name,
                    status=Status.SKIPPED,
                    duration_seconds=duration,
                    reason=(skipped.get("message") or "skipped").strip(),
                    artifacts=list(artifacts),
                )
            )
        else:
            results.append(
                CheckResult(
                    name=name,
                    status=Status.PASS,
                    duration_seconds=duration,
                    artifacts=list(artifacts),
                )
            )

    if not results:
        results.append(
            CheckResult(
                name="pytest collected at least one test",
                status=Status.FAIL,
                reason=f"{path.name} contains no test cases - check the selection arguments",
            )
        )
    return results
