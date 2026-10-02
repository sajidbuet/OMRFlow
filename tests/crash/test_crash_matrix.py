"""Real-process kill / restart matrix for Scan and Resolve (0.1.1 phase 3).

Every "forced kill" here terminates a real child OMRFlow process
(:mod:`tests.crash.scan_resolve_child`: the real main window, Scan stage and
Resolve stage on the offscreen platform, with a real worker pool) from
outside it with ``Process.kill()``; every "clean close" is the application's
own stop-and-exit path. Evidence is gathered outside the killed process: the
child's evidence log (written and flushed before each boundary is passed) and
the project database, opened read-only by this process after the child is
gone. Kill points are states ("N sheets committed", "paused at boundary X"),
never sleeps.

Scale: :data:`SHEETS` deterministic synthetic sheets (seed :data:`SEED`; the
stress dataset's mix - clean, blank and multiple answers, faint and ambiguous
marks, skew, an ambiguous and a duplicate Student ID, a byte-identical
duplicate, several sets, an unreadable file) and two workers. The
1 / 25 / 50 / 75 / 99 % series also runs at 1 000 sheets under ``-m stress``.

Case numbers follow the phase brief (§19). Results are recorded per case in
``development/releases/0.1.1-alpha.0/PHASE_C_HANDOFF.md``.
"""

from __future__ import annotations

import json
import math
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from tests.crash import harness as h

SEED = 42
SHEETS = 40
WORKERS = 2
CHILD_TIMEOUT = 300.0
REVIEWER = "Crash Harness"


# ----------------------------------------------------------------------
# Shared fixtures
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def sheets_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("crash_sheets")
    h.render_sheets(directory, SHEETS, seed=SEED)
    return directory


@pytest.fixture(scope="module")
def reference(tmp_path_factory: pytest.TempPathFactory, sheets_dir: Path) -> dict[str, Any]:
    """The same sheets processed once, uninterrupted, by the same child."""
    root = tmp_path_factory.mktemp("crash_reference")
    project = h.create_project_with_template(root, "Reference")
    events = h.run_to_exit(
        h.launch(project, root / "reference.jsonl", "scan", scans=sheets_dir, workers=WORKERS),
        timeout=CHILD_TIMEOUT,
    )
    assert len(h.submissions(events)) == 1
    return {"project": project, "semantics": semantics(project)}


def evidence(case: str, **facts: Any) -> None:
    """Write one case's measured facts where ``OMRFLOW_CRASH_EVIDENCE`` points, if set."""
    import os

    target = os.environ.get("OMRFLOW_CRASH_EVIDENCE")
    if not target:
        return
    directory = Path(target)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "case": case,
        "sheets": SHEETS,
        "workers": WORKERS,
        **facts,
        # Cumulative over the module so far - see harness.run_to_exit and
        # harness.settle_journal.
        "teardown_aborts_after_clean_close_so_far": list(h.TEARDOWN_ABORTS),
        "hot_journals_rolled_back_so_far": len(h.HOT_JOURNALS_ROLLED_BACK),
    }
    (directory / f"{case}.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )


def reread_count(committed_before: set[str], later_events: list[dict[str, Any]]) -> int:
    return len({name for run in h.submissions(later_events) for name in run} & committed_before)


def retried(later_events: list[dict[str, Any]]) -> int:
    return len({name for run in h.submissions(later_events) for name in run})


def new_project(tmp_path: Path, name: str = "Crash") -> Path:
    return h.create_project_with_template(tmp_path, name)


def log(tmp_path: Path, label: str) -> Path:
    return tmp_path / f"{label}.jsonl"


# ----------------------------------------------------------------------
# What every recovered project must satisfy
# ----------------------------------------------------------------------
def assert_consistent(project: Path) -> h.Integrity:
    """Cases 9 and 12: integrity, health, and no duplicated logical rows."""
    found = h.integrity(project)
    assert found.sqlite_ok, found
    assert found.health_errors == [], found.health_codes
    assert h.duplicate_conflict_identities(project) == 0
    assert h.detected_twice(project) == 0
    shot = h.snapshot(project)
    assert len(shot["scan_sessions"]) == 1, shot["scan_sessions"]
    assert len(shot["scan_batches"]) == 1, shot["scan_batches"]
    assert shot["supersessions"] == 0
    assert shot["batch_scans"] == SHEETS
    return found


def assert_never_reread(committed_before: set[str], later_events: list[dict[str, Any]]) -> None:
    """Case 2/4/13: a sheet committed before a kill is never submitted again."""
    resubmitted = {name for run in h.submissions(later_events) for name in run}
    assert not (resubmitted & committed_before), sorted(resubmitted & committed_before)


def assert_each_sheet_read_once(project: Path) -> None:
    """Independent witness: every row's attempt counter says it was recorded once."""
    rows = h.committed_rows(project)
    assert {row["attempts"] for row in rows.values()} == {1}, {
        name: row["attempts"] for name, row in rows.items() if row["attempts"] != 1
    }


def semantics(project: Path) -> dict[str, Any]:
    """Case 10/37: everything an examination result depends on, comparable across projects."""
    from omr_scanner.services import open_project

    rows = h.committed_rows(project)
    sheets = {}
    for name, row in rows.items():
        payload = json.loads(row["result"]) if row["result"] else {}
        # When and how fast - the only fields two reads of one file may differ in.
        for volatile in ("elapsed_seconds", "recognised_at", "timings"):
            payload.pop(volatile, None)
        sheets[name] = (row["status"], json.dumps(payload, sort_keys=True))
    shot = h.snapshot(project)
    by_scan = {row["scan_id"]: name for name, row in rows.items()}
    conflicts = sorted(
        (by_scan[scan_id], kind, zone, group, state, machine)
        for _cid, scan_id, kind, zone, group, state, machine in shot["conflicts"]
    )

    session = open_project(project, read_only=True)
    try:
        from omr_scanner.services import review_store

        batch_id = next(iter({row["batch_id"] for row in rows.values()}))
        identifiers = {
            by_scan[scan_id]: (item.value, item.unresolved)
            for scan_id, item in review_store.effective_identifiers(
                session.database, batch_id
            ).items()
        }
        set_codes = {
            by_scan[scan_id]: (item.value, item.unresolved)
            for scan_id, item in review_store.effective_set_codes(
                session.database, batch_id
            ).items()
        }
    finally:
        session.close()
    return {
        "sheets": sheets,
        "conflicts": conflicts,
        "identifiers": identifiers,
        "set_codes": set_codes,
    }


def downstream(project: Path, tmp_path: Path) -> dict[str, Any]:
    """Case 10: Attendance, Answer Key and Results on the single downstream batch."""
    from omr_scanner.domain.scoring import ScoringPolicy
    from omr_scanner.evaluation import stress_dataset
    from omr_scanner.evaluation.test_cases import FieldLayout
    from omr_scanner.services import (
        open_project,
        reconciliation_store,
        scan_sessions,
        scoring_store,
    )
    from omr_scanner.services.answer_key import plan_for, read_key
    from omr_scanner.services.candidate_import import read_roster

    template = h.template()
    plan = plan_for(template)
    spec = stress_dataset.StressDatasetSpec(seed=SEED, sheet_count=SHEETS)
    layout = FieldLayout.of(template)
    roster = tmp_path / f"roster_{project.name}.csv"
    lines = [f"Roll No.,Name,Total ({plan.question_count})"]
    lines += [
        f"{stress_dataset.natural_roll(spec, layout, index)},"
        f"CANDIDATE {index},{plan.question_count}"
        for index in range(SHEETS)
    ]
    roster.write_text("\n".join(lines) + "\n", encoding="utf-8")

    session = open_project(project, force_lock=True)
    try:
        database = session.database
        batch_id = scan_sessions.downstream_batch_id(database)
        assert batch_id is not None
        roster_id = reconciliation_store.import_roster(
            database, read_roster(roster), imported_by=REVIEWER
        )
        counts = reconciliation_store.reconcile_batch(database, roster_id, batch_id)
        key = scoring_store.save_key(
            database, read_key("A" * plan.question_count, plan, "A").to_key()
        )
        scoring_store.verify_key(database, key.key_id, verified_by=REVIEWER)
        scoring_store.save_policy(
            database, ScoringPolicy(correct_mark=Fraction(1)), created_by=REVIEWER
        )
        try:
            scoring_store.score_batch(database, roster_id, batch_id, template, computed_by=REVIEWER)
            scored = "scored"
        except Exception as exc:  # recorded and compared, not hidden
            scored = f"refused: {type(exc).__name__}"
        results = sorted(
            (item.candidate_id, str(item.final_score), str(item.status))
            for item in scoring_store.list_results(database, roster_id, batch_id, template)
        )
        return {
            "reconciliation": {
                key_: value
                for key_, value in vars(counts).items()
                if isinstance(value, int)
            }
            if hasattr(counts, "__dict__")
            else str(counts),
            "scoring": scored,
            "results": results,
        }
    finally:
        session.close()


def committed_now(project: Path) -> set[str]:
    return h.terminal_names(h.committed_rows(project))


def kill_at_commits(
    project: Path, tmp_path: Path, label: str, count: int, **launch: Any
) -> tuple[set[str], h.Killed]:
    """Run until the project holds ``count`` committed sheets, pause there, kill.

    ``count`` is cumulative over the batch; the child counts its own run's
    commits, so it is told how many more to commit before pausing.
    """
    already = len(committed_now(project)) if (project / "database.sqlite").exists() else 0
    more = max(count - already, 1)
    child = h.launch(
        project, log(tmp_path, label), "scan", pause="committed", pause_count=more, **launch
    )
    h.wait_for(
        child,
        lambda events: any(item["event"] == "paused" for item in events),
        timeout=CHILD_TIMEOUT,
        what=f"{count} committed sheets",
    )
    killed = h.kill(child)
    assert killed.orphans == (), "worker processes outlived the coordinator"
    assert killed.lock_left, "a real kill releases nothing"
    committed = committed_now(project)
    logged = set(h.committed_names(killed.events))
    # The log line is written after the commit returned: what the child saw
    # committed is committed.
    assert logged <= committed
    assert len(committed) >= count
    return committed, killed


def resume_to_end(project: Path, tmp_path: Path, label: str) -> list[dict[str, Any]]:
    return h.run_to_exit(
        h.launch(project, log(tmp_path, label), "scan", workers=WORKERS, force_lock=True),
        timeout=CHILD_TIMEOUT,
    )


def opened(events: list[dict[str, Any]]) -> dict[str, Any]:
    return next(item for item in events if item["event"] == "opened")


# ----------------------------------------------------------------------
# Scan
# ----------------------------------------------------------------------
def test_case_01_clean_close_halfway_through_scan(tmp_path, sheets_dir, reference):
    project = new_project(tmp_path)
    events = h.run_to_exit(
        h.launch(
            project, log(tmp_path, "run1"), "scan", scans=sheets_dir, workers=WORKERS,
            clean_close_after=SHEETS // 2,
        ),
        timeout=CHILD_TIMEOUT,
    )
    from omr_scanner.services.project_lock import LOCK_FILE_NAME

    assert any(item["event"] == "stopped_by_operator" for item in events)
    assert not (project / LOCK_FILE_NAME).exists()  # a clean close releases the project
    committed = committed_now(project)
    rows = h.committed_rows(project)
    assert len(committed) >= SHEETS // 2
    # Nothing that was not read is recorded as failed: only real failures are.
    failures = {name for name, row in rows.items() if row["status"] == "failed"}
    real_failures = {
        name
        for name, (status, _p) in reference["semantics"]["sheets"].items()
        if status == "failed"
    }
    assert failures <= real_failures
    assert {row["status"] for name, row in rows.items() if name not in committed} == {"cancelled"}

    later = resume_to_end(project, tmp_path, "resume")
    reopened = opened(later)
    assert reopened["scan_entries_with_result"] == len(committed)
    assert reopened["scan_resume_enabled"] is True
    assert_never_reread(committed, later)
    assert_each_sheet_read_once(project)
    found = assert_consistent(project)
    evidence(
        "case_01", kill_point=f"clean stop-and-exit after {SHEETS // 2} committed",
        committed_before=len(committed), retried=retried(later),
        reread=reread_count(committed, later), false_failures=0,
        reopened_counts=reopened["scan_counts_label"], health=found.health_codes,
    )


def test_case_02_forced_kill_halfway_through_scan(tmp_path, sheets_dir):
    project = new_project(tmp_path)
    child = h.launch(project, log(tmp_path, "run1"), "scan", scans=sheets_dir, workers=WORKERS)
    h.wait_for(
        child,
        lambda events: len(h.committed_names(events)) >= SHEETS // 2,
        timeout=CHILD_TIMEOUT,
        what="half the sheets committed",
    )
    killed = h.kill(child)  # free-running: sheets are in the workers right now
    committed = committed_now(project)
    assert set(h.committed_names(killed.events)) <= committed
    assert SHEETS // 2 <= len(committed) < SHEETS

    later = resume_to_end(project, tmp_path, "resume")
    assert opened(later)["scan_entries_with_result"] == len(committed)
    assert_never_reread(committed, later)
    again = {name for run in h.submissions(later) for name in run}
    assert again == {f"sheet_{i:05d}.png" for i in range(SHEETS)} - committed
    assert_each_sheet_read_once(project)
    found = assert_consistent(project)
    evidence(
        "case_02", kill_point=f"free-running kill once >= {SHEETS // 2} commits logged",
        kill_landed_inside_a_commit=killed.hot_journal,
        committed_before=len(committed), retried=len(again),
        reread=reread_count(committed, later),
        reopened_counts=opened(later)["scan_counts_label"], health=found.health_codes,
    )


def test_case_03_kill_while_a_sheet_is_in_the_worker(tmp_path, sheets_dir):
    project = new_project(tmp_path)
    # One worker: the run reports each sheet as it starts. Paused as sheet 6
    # is about to be read - five committed, one in the worker.
    child = h.launch(
        project, log(tmp_path, "run1"), "scan", scans=sheets_dir, workers=1,
        pause="started", pause_count=5,
    )
    events = h.wait_for(
        child, lambda ev: any(item["event"] == "paused" for item in ev),
        timeout=CHILD_TIMEOUT, what="sheet 6 starting",
    )
    in_worker = next(item for item in reversed(events) if item["event"] == "started")["path"]
    h.kill(child)
    rows = h.committed_rows(project)
    assert rows[in_worker]["status"] == "queued"  # submitted, never committed
    assert in_worker not in committed_now(project)

    inspected = h.run_to_exit(
        h.launch(project, log(tmp_path, "inspect"), "inspect", force_lock=True),
        timeout=CHILD_TIMEOUT,
    )
    rows = h.committed_rows(project)
    assert rows[in_worker]["status"] == "pending"  # retryable: not failed, not completed
    assert rows[in_worker]["result"] == ""
    assert "pending" in opened(inspected)["scan_counts_label"]

    later = resume_to_end(project, tmp_path, "resume")
    assert in_worker in {name for run in h.submissions(later) for name in run}
    assert_each_sheet_read_once(project)
    found = assert_consistent(project)
    evidence(
        "case_03", kill_point=f"paused as {in_worker} entered the (single) worker",
        committed_before=5, in_flight_sheet=in_worker, status_after_reopen="pending",
        retried=retried(later), reread=reread_count(set(h.committed_names(events)), later),
        health=found.health_codes,
    )


def test_case_04_kill_after_a_commit_before_the_next_sheet(tmp_path, sheets_dir):
    project = new_project(tmp_path)
    child = h.launch(
        project, log(tmp_path, "run1"), "scan", scans=sheets_dir, workers=1,
        pause="committed", pause_count=7,
    )
    events = h.wait_for(
        child, lambda ev: any(item["event"] == "paused" for item in ev),
        timeout=CHILD_TIMEOUT, what="7 committed",
    )
    h.kill(child)
    committed = committed_now(project)
    assert committed == set(h.committed_names(events))  # nothing lost, nothing extra
    assert len(committed) == 7
    last = h.committed_names(events)[-1]

    later = resume_to_end(project, tmp_path, "resume")
    assert_never_reread(committed, later)
    assert last not in {name for run in h.submissions(later) for name in run}
    assert_each_sheet_read_once(project)
    found = assert_consistent(project)
    evidence(
        "case_04", kill_point=f"paused right after the commit of {last} (7th), before the next",
        committed_before=len(committed), retried=retried(later),
        reread=reread_count(committed, later), health=found.health_codes,
    )


# ----------------------------------------------------------------------
# Resolve
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def scanned(tmp_path_factory: pytest.TempPathFactory, sheets_dir: Path) -> Path:
    """A project fully processed by the child, for copying into Resolve cases."""
    root = tmp_path_factory.mktemp("crash_scanned")
    project = h.create_project_with_template(root, "Scanned")
    h.run_to_exit(
        h.launch(project, root / "scan.jsonl", "scan", scans=sheets_dir, workers=WORKERS),
        timeout=CHILD_TIMEOUT,
    )
    return project


def copy_project(source: Path, tmp_path: Path) -> Path:
    import shutil

    target = tmp_path / source.name
    shutil.copytree(source, target)
    return target


def unresolved_queue(project: Path) -> list[int]:
    from omr_scanner.domain.review import ConflictState
    from omr_scanner.services import open_project, review_store, scan_sessions

    session = open_project(project, read_only=True)
    try:
        batch_id = scan_sessions.downstream_batch_id(session.database)
        assert batch_id is not None
        return [
            item.conflict_id
            for item in review_store.list_conflicts(
                session.database,
                batch_id,
                filters=review_store.ConflictFilter(
                    states=(ConflictState.OPEN, ConflictState.DEFERRED)
                ),
            )
        ]
    finally:
        session.close()


def withdrawn_by_decisions(project: Path) -> set[int]:
    """Conflicts the system withdrew, with an audit event, after a human decision.

    0.1.1 phase 4 detects duplicate Student IDs on the *effective* value, so a
    correction that makes one of a duplicate pair unique withdraws its
    partner's undecided duplicate record - audited, never silent. Before
    phase 4 the queue lost only what was decided; now it also loses exactly
    these, and the crash assertions account for them explicitly.
    """
    shot = h.snapshot(project)
    first_human = min(
        (row[0] for row in shot["audit"] if row[1] in ("accepted", "corrected")),
        default=None,
    )
    if first_human is None:
        return set()
    return {
        int(row[2])
        for row in shot["audit"]
        if row[1] == "withdrawn" and row[0] > first_human and row[2] is not None
    }


def human_events(project: Path) -> list[tuple]:
    shot = h.snapshot(project)
    return [row for row in shot["audit"] if row[1] in ("accepted", "corrected")]


def test_case_05_clean_close_halfway_through_resolve(tmp_path, scanned):
    project = copy_project(scanned, tmp_path)
    before = unresolved_queue(project)
    assert len(before) >= 4
    decisions = len(before) // 2
    events = h.run_to_exit(
        h.launch(project, log(tmp_path, "resolve"), "resolve", decisions=decisions),
        timeout=CHILD_TIMEOUT,
    )
    decided = [item["conflict_id"] for item in events if item["event"] == "decision_committed"]
    assert len(decided) == decisions
    assert len(human_events(project)) == decisions
    withdrawn = withdrawn_by_decisions(project)
    assert unresolved_queue(project) == [
        cid for cid in before if cid not in decided and cid not in withdrawn
    ]
    found = assert_consistent(project)
    evidence(
        "case_05", kill_point=f"clean close after {decisions} Resolve decisions",
        conflicts_before=len(before), decisions_committed=len(decided),
        decisions_in_ledger=len(human_events(project)),
        unresolved_after=len(unresolved_queue(project)), health=found.health_codes,
    )


@pytest.fixture(scope="module")
def killed_during_resolve(
    tmp_path_factory: pytest.TempPathFactory, scanned: Path
) -> dict[str, Any]:
    """Case 6's scenario, run once: several corrections, then a forced kill."""
    root = tmp_path_factory.mktemp("crash_resolve_kill")
    project = copy_project(scanned, root)
    before = unresolved_queue(project)
    decisions = max(3, len(before) // 2)
    child = h.launch(
        project, root / "resolve.jsonl", "resolve", decisions=decisions,
        pause="decisions", pause_count=decisions,
    )
    h.wait_for(
        child, lambda ev: any(item["event"] == "paused" for item in ev),
        timeout=CHILD_TIMEOUT, what=f"{decisions} decisions",
    )
    killed = h.kill(child)
    after_kill = h.snapshot(project)
    inspected = [
        h.run_to_exit(
            h.launch(project, root / f"inspect{n}.jsonl", "inspect", force_lock=True),
            timeout=CHILD_TIMEOUT,
        )
        for n in range(3)
    ]
    return {
        "project": project,
        "before": before,
        "decided": [
            (item["conflict_id"], item["action"])
            for item in killed.events
            if item["event"] == "decision_committed"
        ],
        "after_kill": after_kill,
        "inspected": inspected,
    }


def test_case_06_forced_kill_after_several_resolve_corrections(killed_during_resolve):
    scenario = killed_during_resolve
    decided = scenario["decided"]
    assert len(decided) >= 3
    events = human_events(scenario["project"])
    # Every decision the child saw commit is in the ledger, in order, once.
    assert [(row[2], row[1]) for row in events] == [
        (cid, "corrected" if action == "correct" else "accepted") for cid, action in decided
    ]
    found = assert_consistent(scenario["project"])
    evidence(
        "case_06", kill_point=f"paused after the commit of decision {len(decided)}",
        conflicts_before=len(scenario["before"]), decisions_committed=len(decided),
        corrections=sum(1 for _c, action in decided if action == "correct"),
        decisions_in_ledger=len(events), health=found.health_codes,
    )


def test_case_07_reopen_retains_machine_override_effective_and_history(killed_during_resolve):
    from omr_scanner.services import open_project, review_store

    scenario = killed_during_resolve
    shot = scenario["after_kill"]
    machine_at_kill = {row[0]: row[6] for row in shot["conflicts"]}
    session = open_project(scenario["project"], read_only=True)
    try:
        for conflict_id, action in scenario["decided"]:
            record = review_store.get_conflict(session.database, conflict_id)
            assert record is not None
            assert record.observation.value == machine_at_kill[conflict_id]  # machine kept
            provenance = review_store.provenance_for(session.database, conflict_id)
            history = review_store.history_for(session.database, conflict_id)
            human = [item for item in history if item.reviewer]
            assert len(human) == 1 and human[0].reviewer == REVIEWER
            if action == "correct":
                assert provenance.value == "9"  # the override is the effective value
                assert human[0].new_value == "9"
            else:
                assert provenance.value == record.observation.value
    finally:
        session.close()
    evidence("case_07", decisions_checked=len(scenario["decided"]))


def test_case_08_unresolved_items_remain_unresolved(killed_during_resolve):
    scenario = killed_during_resolve
    decided = {cid for cid, _action in scenario["decided"]}
    withdrawn = withdrawn_by_decisions(scenario["project"])
    expected = [
        cid for cid in scenario["before"] if cid not in decided and cid not in withdrawn
    ]
    assert unresolved_queue(scenario["project"]) == expected
    for events in scenario["inspected"]:
        assert opened(events)["resolve_queue"] == expected
    evidence("case_08", unresolved=len(expected), reopens=len(scenario["inspected"]))


def test_case_09_repeated_restarts_change_nothing(killed_during_resolve):
    scenario = killed_during_resolve
    project = scenario["project"]
    final = h.snapshot(project)
    # Three reopens after the kill: identical rows, identical ledger.
    assert final == scenario["after_kill"]

    def shown(events: list[dict[str, Any]]) -> dict[str, Any]:
        return {k: v for k, v in opened(events).items() if k not in ("pid", "t")}

    first = shown(scenario["inspected"][0])
    assert all(shown(events) == first for events in scenario["inspected"])
    found = assert_consistent(project)
    evidence(
        "case_09", reopens=len(scenario["inspected"]),
        rows={
            "conflicts": len(final["conflicts"]), "audit": len(final["audit"]),
            "sessions": len(final["scan_sessions"]), "batches": len(final["scan_batches"]),
            "supersessions": final["supersessions"], "scans": final["batch_scans"],
            **final["other_tables"],
        },
        health=found.health_codes,
    )


def test_case_15_resolve_is_reachable_after_reopen_without_visiting_scan(killed_during_resolve):
    scenario = killed_during_resolve
    first = opened(scenario["inspected"][0])
    assert first["resolve_batch_id"] is not None
    assert first["resolve_queue"], "Resolve opened with an empty queue"
    resolved = len(scenario["decided"])
    assert f"<b>{resolved}</b> resolved" in first["resolve_summary"]
    evidence("case_15", resolve_summary=first["resolve_summary"], queue=len(first["resolve_queue"]))


# ----------------------------------------------------------------------
# Consistency
# ----------------------------------------------------------------------
def test_case_10_results_after_interruption_equal_an_uninterrupted_run(
    tmp_path, sheets_dir, reference
):
    project = new_project(tmp_path)
    committed, _killed = kill_at_commits(
        project, tmp_path, "run1", SHEETS // 3, scans=sheets_dir, workers=WORKERS
    )
    second, _ = kill_at_commits(
        project, tmp_path, "run2", (2 * SHEETS) // 3, workers=WORKERS, force_lock=True
    )
    later = resume_to_end(project, tmp_path, "resume")
    assert_never_reread(second, later)
    assert_each_sheet_read_once(project)
    found = assert_consistent(project)
    assert semantics(project) == reference["semantics"]
    reference_copy = copy_project(reference["project"], tmp_path / "ref")
    interrupted, uninterrupted = downstream(project, tmp_path), downstream(reference_copy, tmp_path)
    assert interrupted == uninterrupted
    evidence(
        "case_10", kill_points=[SHEETS // 3, (2 * SHEETS) // 3],
        committed_before=[len(committed), len(second)], retried=retried(later),
        reread=reread_count(second, later), downstream=interrupted, health=found.health_codes,
    )


@pytest.mark.parametrize("percentages", [(1, 25, 50, 75, 99)])
def test_case_11_kills_at_several_percentages(tmp_path, sheets_dir, reference, percentages):
    record = chain(tmp_path, sheets_dir, SHEETS, percentages, reference["semantics"])
    evidence("case_11", chain=record)


def chain(
    tmp_path: Path, sheets_dir: Path, total: int, percentages: Any, expected: Any
) -> list[dict[str, Any]]:
    """Kill at each percentage in turn on one project, resuming between, then finish."""
    project = new_project(tmp_path)
    record = []
    committed: set[str] = set()
    for step, percent in enumerate(percentages):
        target = max(1, math.floor(total * percent / 100))
        previous = committed
        committed, killed = kill_at_commits(
            project, tmp_path, f"kill{percent}", target,
            scans=sheets_dir if step == 0 else None, workers=WORKERS, force_lock=step > 0,
        )
        resubmitted = {name for run in h.submissions(killed.events) for name in run}
        record.append(
            {
                "percent": percent,
                "kill_after_committed": target,
                "committed_in_db": len(committed),
                "in_flight_at_kill": sum(
                    1 for row in h.committed_rows(project).values() if row["status"] == "queued"
                ),
                "submitted_this_run": len(resubmitted),
                "resubmitted_committed": len(resubmitted & previous),
                "kill_landed_inside_a_commit": killed.hot_journal,
            }
        )
        assert previous <= committed  # nothing committed is ever lost
        assert not (resubmitted & previous)  # nor read again
    later = resume_to_end(project, tmp_path, "final")
    assert_never_reread(committed, later)
    assert_each_sheet_read_once(project)
    assert_consistent(project)
    assert semantics(project) == expected
    (tmp_path / "chain.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def test_case_12_integrity_after_a_kill_before_any_recovery(tmp_path, sheets_dir):
    project = new_project(tmp_path)
    kill_at_commits(project, tmp_path, "run1", 12, scans=sheets_dir, workers=WORKERS)
    # Straight after the kill, before any reopen: structurally sound, and the
    # only findings are the ones recovery exists to repair.
    found = h.integrity(project)
    assert found.sqlite_ok, found
    assert found.health_errors == []
    assert {"BATCH_LEFT_RUNNING", "STALE_PROCESSING_JOBS"} <= set(found.health_codes)
    h.run_to_exit(
        h.launch(project, log(tmp_path, "inspect"), "inspect", force_lock=True),
        timeout=CHILD_TIMEOUT,
    )
    after = h.integrity(project)
    assert not ({"BATCH_LEFT_RUNNING", "STALE_PROCESSING_JOBS"} & set(after.health_codes))
    assert_consistent(project)
    evidence(
        "case_12", quick_check=found.quick_check, integrity_check=found.integrity_check,
        foreign_key_check=found.foreign_key_check, health_after_kill=found.health_codes,
        health_after_reopen=after.health_codes,
    )


# ----------------------------------------------------------------------
# The S1 boundary: recognition committed, conflicts not yet generated
# ----------------------------------------------------------------------
def test_case_13a_killed_after_recognition_before_the_batch_review_pass(
    tmp_path, sheets_dir, reference
):
    project = new_project(tmp_path)
    child = h.launch(
        project, log(tmp_path, "run1"), "scan", scans=sheets_dir, workers=WORKERS,
        pause="run_recognised",
    )
    h.wait_for(
        child, lambda ev: any(item["event"] == "paused" for item in ev),
        timeout=CHILD_TIMEOUT, what="every result committed",
    )
    h.kill(child)
    committed = committed_now(project)
    assert len(committed) == SHEETS
    kinds = {row[2] for row in h.snapshot(project)["conflicts"]}
    assert "identifier_duplicate" not in kinds  # the batch-scope pass never ran

    later = h.run_to_exit(
        h.launch(project, log(tmp_path, "inspect"), "inspect", force_lock=True),
        timeout=CHILD_TIMEOUT,
    )
    assert h.submissions(later) == []  # nothing recognised by the reopen
    assert_each_sheet_read_once(project)
    assert_consistent(project)
    assert semantics(project)["conflicts"] == reference["semantics"]["conflicts"]
    again = h.snapshot(project)
    h.run_to_exit(
        h.launch(project, log(tmp_path, "inspect2"), "inspect", force_lock=True),
        timeout=CHILD_TIMEOUT,
    )
    assert h.snapshot(project) == again  # generated exactly once
    evidence(
        "case_13a", kill_point="paused after every result committed, before the batch-scope pass",
        committed_before=len(committed), duplicate_id_conflicts_before=0,
        duplicate_id_conflicts_after=sum(
            1 for row in again["conflicts"] if row[2] == "identifier_duplicate"
        ),
        reread=0, conflicts_after=len(again["conflicts"]),
    )


def test_case_13b_an_earlier_builds_results_without_conflicts_are_completed_once(
    tmp_path, sheets_dir, reference
):
    project = new_project(tmp_path)
    child = h.launch(
        project, log(tmp_path, "legacy"), "legacy_scan", scans=sheets_dir, workers=WORKERS,
        pause="committed", pause_count=SHEETS // 2,
    )
    events = h.wait_for(
        child, lambda ev: any(item["event"] == "paused" for item in ev),
        timeout=CHILD_TIMEOUT, what="legacy commits",
    )
    h.kill(child)
    committed = committed_now(project)
    assert set(h.committed_names(events)) <= committed
    assert h.snapshot(project)["conflicts"] == []  # recognition committed, conflicts missing

    inspected = h.run_to_exit(
        h.launch(project, log(tmp_path, "inspect"), "inspect", force_lock=True),
        timeout=CHILD_TIMEOUT,
    )
    assert h.submissions(inspected) == []
    expected = [
        row for row in reference["semantics"]["conflicts"]
        if row[0] in committed and row[1] != "identifier_duplicate"
    ]
    now = [row for row in semantics(project)["conflicts"] if row[1] != "identifier_duplicate"]
    assert now == expected
    assert h.detected_twice(project) == 0

    later = resume_to_end(project, tmp_path, "resume")
    assert_never_reread(committed, later)
    assert_each_sheet_read_once(project)
    found = assert_consistent(project)
    assert semantics(project)["conflicts"] == reference["semantics"]["conflicts"]
    evidence(
        "case_13b", kill_point=f"pre-phase-3 write path paused after {SHEETS // 2} commits",
        committed_before=len(committed), conflicts_at_kill=0,
        conflicts_created_on_reopen=len(now), retried=retried(later),
        reread=reread_count(committed, later), health=found.health_codes,
    )


# ----------------------------------------------------------------------
# SEALED batch interrupted
# ----------------------------------------------------------------------
def test_case_14_an_interrupted_sealed_batch_stays_sealed_and_resumes_its_members(
    tmp_path, sheets_dir
):
    from omr_scanner.services import open_project, scan_sessions

    project = new_project(tmp_path)
    h.run_to_exit(
        h.launch(
            project, log(tmp_path, "run1"), "scan", scans=sheets_dir, workers=WORKERS,
            clean_close_after=8,
        ),
        timeout=CHILD_TIMEOUT,
    )
    session = open_project(project)
    try:
        current = scan_sessions.active_scan_session(session.database)
        assert current is not None
        # Close (seals the batch), then reopen: the batch stays sealed.
        scan_sessions.close_scan_session(
            session.database, current.scan_session_id, closed_by=REVIEWER
        )
        scan_sessions.reopen_scan_session(
            session.database, current.scan_session_id, reopened_by=REVIEWER
        )
        (batch,) = scan_sessions.batches_of(session.database, current.scan_session_id)
        sealed_at = batch.sealed_at
        assert sealed_at is not None
    finally:
        session.close()

    before = committed_now(project)
    committed, _ = kill_at_commits(
        project, tmp_path, "run2", len(before) + 10, workers=WORKERS, force_lock=False
    )
    later = resume_to_end(project, tmp_path, "resume")
    assert_never_reread(committed, later)
    session = open_project(project, read_only=True)
    try:
        (after,) = scan_sessions.batches_of(session.database, current.scan_session_id)
        assert after.batch_id == batch.batch_id
        assert after.sealed_at == sealed_at
        assert after.total_scans == SHEETS
    finally:
        session.close()
    assert_each_sheet_read_once(project)
    found = assert_consistent(project)
    evidence(
        "case_14",
        kill_point=f"paused after {len(before) + 10} commits while resuming a sealed batch",
        committed_before=len(committed), retried=retried(later),
        reread=reread_count(committed, later), sealed_at=str(sealed_at),
        members=SHEETS, health=found.health_codes,
    )


# ----------------------------------------------------------------------
# Release-relevant scale (excluded from the ordinary run)
# ----------------------------------------------------------------------
@pytest.mark.stress
def test_case_11_at_one_thousand_sheets(tmp_path_factory: pytest.TempPathFactory):
    total = 1000
    sheets = tmp_path_factory.mktemp("crash_1000_sheets")
    h.render_sheets(sheets, total, seed=SEED)
    root = tmp_path_factory.mktemp("crash_1000_reference")
    reference_project = h.create_project_with_template(root, "Reference")
    h.run_to_exit(
        h.launch(reference_project, root / "reference.jsonl", "scan", scans=sheets, workers=4),
        timeout=1800,
    )
    expected = semantics(reference_project)
    work = tmp_path_factory.mktemp("crash_1000_chain")
    record = chain_at(work, sheets, total, (1, 25, 50, 75, 99), expected, workers=4)
    evidence("case_11_1000", sheets=total, workers=4, chain=record)


def chain_at(tmp_path: Path, sheets_dir: Path, total: int, percentages, expected, *, workers: int):  # type: ignore[no-untyped-def]
    global SHEETS, WORKERS, CHILD_TIMEOUT
    saved = (SHEETS, WORKERS, CHILD_TIMEOUT)
    SHEETS, WORKERS, CHILD_TIMEOUT = total, workers, 1800.0
    try:
        return chain(tmp_path, sheets_dir, total, percentages, expected)
    finally:
        SHEETS, WORKERS, CHILD_TIMEOUT = saved
