r"""Installed-build checks for the ``0.1.1-alpha.0`` release gate (revised phase 10).

Two checks the release gate (``ACCEPTANCE_CRITERIA.md`` §8, items 4a and 6)
asks of the **installed** application - not of the source tree:

``upgrade``
    Copy a project written by an earlier build (the committed schema-12
    fixtures, or a project the actual ``v0.1.0-alpha.2`` code wrote), open it
    in the installed ``OMRFlow.exe`` (which migrates it), walk the workflow
    stages through Windows UI Automation, close the application, then verify
    from the outside: the schema, the pre-migration backup, integrity, Project
    Health, the scan-session backfill, and that **every row and value the old
    build wrote is still there** (new columns and rows may be added; nothing
    old may change, except the migration ledger itself).

``gui-recovery``
    Run the headless continuous engine *inside the installed executable* on a
    small two-source intake, kill it with ``TerminateProcess`` while sheets are
    in a worker, then open the project in the installed GUI and read - through
    UI Automation, before anything is started - what Scan and Resolve show:
    S2 (the interrupted session and its committed sheets are reconstructed
    from the database), S3 (the counts shown are the committed counts) and R1
    (Resolve shows the persisted session's queue without visiting Scan).

Both launch the application with ``OMRFLOW_CONFIG_DIR`` / ``OMRFLOW_LOG_DIR``
pointed into the work folder, so the operator's real OMRFlow profile is never
touched, and both write ``report.json`` / ``report.md``.

Usage (from the repository root, with ``pywinauto`` installed)::

    .venv\Scripts\python.exe -m tools.release_validation.installed_checks upgrade `
        --exe "$env:LOCALAPPDATA\Programs\OMRFlow\OMRFlow.exe" `
        --fixture tests\fixtures\schema12\unique_sets --work Scratch\phase10\upgrade\unique_sets
    .venv\Scripts\python.exe -m tools.release_validation.installed_checks gui-recovery `
        --exe "$env:LOCALAPPDATA\Programs\OMRFlow\OMRFlow.exe" --work Scratch\phase10\gui-recovery
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
STAGES = ("project", "template", "calibration", "scan", "resolve", "attendance", "answer_key",
          "results", "reports")
LABELS_OF_INTEREST = (
    "scanSessionLabel", "reviewBatchLabel", "reviewSummaryLabel", "activeRosterLabel",
    "reconciliationSummaryLabel", "attendanceBatchLabel", "answerKeyReadinessLabel",
    "answerKeySessionLabel", "resultsSummaryLabel", "resultsBatchLabel", "batchStateLabel",
    "workersLabel",
)
UNCHANGED_EXCEPT = {
    "schema_migration": "the migration ledger records the new migrations",
}
"""Tables whose old rows a migration may legitimately rewrite, with the reason."""


# ----------------------------------------------------------------------
# The installed application, driven through UI Automation
# ----------------------------------------------------------------------
def app_environment(work: Path) -> dict[str, str]:
    """The environment a user's double-click would give - with the profile redirected."""
    environment = {
        key: value for key, value in os.environ.items()
        if key.upper() not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX",
                               "QT_QPA_PLATFORM", "QT_PLUGIN_PATH")
    }
    environment["OMRFLOW_CONFIG_DIR"] = str(work / "user-config")
    environment["OMRFLOW_LOG_DIR"] = str(work / "user-logs")
    return environment


@dataclass
class InstalledGui:
    """One run of the installed application with a project open."""

    exe: Path
    work: Path
    project: Path | None = None
    process: subprocess.Popen[bytes] | None = None
    window: Any = None
    app: Any = None
    started_at: float = 0.0
    seen: dict[str, dict[str, str]] = field(default_factory=dict)

    def start(self, *, timeout: float = 120.0) -> None:
        """Launch and wait until the main window is visible and ready."""
        from pywinauto.application import Application

        args = [str(self.exe)] + ([str(self.project)] if self.project else [])
        self.started_at = time.time()
        self.process = subprocess.Popen(args, env=app_environment(self.work), cwd=str(self.work))
        self.app = Application(backend="uia").connect(process=self.process.pid, timeout=timeout)
        self.window = self.app.top_window()
        self.window.wait("visible ready", timeout=timeout)

    def title(self) -> str:
        """The main window's title."""
        return str(self.window.window_text())

    def other_windows(self) -> list[str]:
        """Titles of any other top-level window of the process - a dialog, an error box."""
        main = self.window.handle
        return [str(item.window_text()) for item in self.app.windows() if item.handle != main]

    def labels(self) -> dict[str, str]:
        """Every visible text, button and data item, keyed by its automation id leaf."""
        found: dict[str, str] = {}
        for element in self.window.descendants():
            info = element.element_info
            leaf = (info.automation_id or "").split(".")[-1]
            if info.name and leaf and leaf not in found:
                found[leaf] = str(info.name)
        return found

    def go(self, stage: str, *, settle: float = 2.5) -> dict[str, str]:
        """Select a workflow stage (UI Automation *Invoke*, no mouse) and read the page."""
        target = None
        for element in self.window.descendants(control_type="CheckBox"):
            if (element.element_info.automation_id or "").endswith(f".workflowStep_{stage}"):
                target = element
                break
        if target is None:
            raise RuntimeError(f"no workflow step {stage!r} in the window")
        target.invoke()
        time.sleep(settle)
        texts = self.labels()
        self.seen[stage] = {key: value for key, value in texts.items()
                            if key in LABELS_OF_INTEREST}
        return texts

    def responding(self) -> bool:
        """Whether the main window answers messages."""
        from tools.release_validation.process_utils import window_is_responding

        assert self.process is not None
        return window_is_responding(self.process.pid)

    def close(self, *, timeout: float = 60.0) -> dict[str, Any]:
        """Close the main window as a user would; report how the process ended."""
        assert self.process is not None
        outcome: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            self.window.close()
        try:
            outcome["exit_code"] = self.process.wait(timeout=timeout)
            outcome["closed_cleanly"] = True
        except subprocess.TimeoutExpired:
            outcome["closed_cleanly"] = False
            outcome["dialogs"] = self.other_windows()
            self.process.kill()
            outcome["exit_code"] = self.process.wait(timeout=30)
        return outcome

    def kill(self) -> None:
        """Make sure the process is gone (a check that failed half-way)."""
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            with contextlib.suppress(Exception):
                self.process.wait(timeout=30)

    def log_problems(self) -> list[str]:
        """ERROR / CRITICAL lines the application logged during this run."""
        problems: list[str] = []
        for path in (self.work / "user-logs").glob("*.log*"):
            with contextlib.suppress(OSError):
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if re.search(r"\b(ERROR|CRITICAL)\b", line):
                        problems.append(line[:300])
        return problems


# ----------------------------------------------------------------------
# Reading databases from outside
# ----------------------------------------------------------------------
def schema_version(database: Path) -> int:
    """The highest applied migration, read read-only (0 when unreadable)."""
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return 0
    try:
        return int(connection.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        connection.close()


def table_rows(database: Path) -> dict[str, dict[str, Any]]:
    """Every table's columns and rows (by primary key, or rowid), read read-only."""
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        tables: dict[str, dict[str, Any]] = {}
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )]
        for name in names:
            info = connection.execute(f'PRAGMA table_info("{name}")').fetchall()
            columns = [row[1] for row in info]
            keys = [row[1] for row in sorted(info, key=lambda r: r[5]) if row[5]]
            key_sql = ", ".join(f'"{k}"' for k in keys) if keys else "rowid"
            rows = connection.execute(
                f'SELECT {key_sql}, * FROM "{name}"'  # table names from sqlite_master
            ).fetchall()
            width = len(keys) if keys else 1
            tables[name] = {
                "columns": columns,
                "rows": {tuple(row[:width]): dict(zip(columns, row[width:], strict=True))
                         for row in rows},
            }
        return tables
    finally:
        connection.close()


def preserved(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
              ) -> tuple[list[str], dict[str, Any]]:
    """Failures where an old row or value did not survive; and per-table counts."""
    failures: list[str] = []
    counts: dict[str, Any] = {}
    for name, old in sorted(before.items()):
        new = after.get(name)
        counts[name] = {"before": len(old["rows"]),
                        "after": len(new["rows"]) if new else None}
        if new is None:
            failures.append(f"table {name} disappeared")
            continue
        if name in UNCHANGED_EXCEPT:
            continue
        missing_columns = [column for column in old["columns"] if column not in new["columns"]]
        if missing_columns:
            failures.append(f"{name}: columns removed {missing_columns}")
        for key, row in old["rows"].items():
            now = new["rows"].get(key)
            if now is None:
                failures.append(f"{name} {key}: row removed")
                continue
            changed = {column: (value, now.get(column)) for column, value in row.items()
                       if column in now and now[column] != value}
            if changed:
                failures.append(f"{name} {key}: changed {dict(list(changed.items())[:3])}")
    return failures, counts


def verify_migrated(project: Path, before: dict[str, dict[str, Any]], old_version: int
                    ) -> dict[str, Any]:
    """Everything checked from outside after the installed build migrated ``project``."""
    from omr_scanner.database.migrations import SCHEMA_VERSION
    from omr_scanner.evaluation.qualification import integrity_report

    database = project / "database.sqlite"
    checks: dict[str, Any] = {}
    failures: list[str] = []
    version = schema_version(database)
    checks["schema_version"] = version
    if version != SCHEMA_VERSION:
        failures.append(f"schema {version}, expected {SCHEMA_VERSION}")
    # The pre-migration backup.
    backups = sorted((project / "backups").glob(f"*before-migration-{old_version}-to-*.sqlite3"))
    checks["backups"] = [item.name for item in backups]
    if not backups:
        failures.append(f"no pre-migration backup for schema {old_version}")
    else:
        backup_version = schema_version(backups[-1])
        checks["backup_schema_version"] = backup_version
        if backup_version != old_version:
            failures.append(f"the backup is schema {backup_version}, not {old_version}")
        backup_failures, _ = preserved(before, table_rows(backups[-1]))
        if backup_failures:
            failures.append(f"the backup differs from the original: {backup_failures[:3]}")
        manifest = backups[-1].with_name(backups[-1].name + ".json")
        checks["backup_manifest"] = (json.loads(manifest.read_text(encoding="utf-8"))
                                     if manifest.is_file() else None)
    # Nothing the old build wrote was lost or rewritten.
    after = table_rows(database)
    lost, counts = preserved(before, after)
    failures += lost
    checks["tables"] = counts
    # The scan-session backfill (migration 14, ADR-0005) and set identity (13).
    sessions = after.get("scan_session", {"rows": {}})["rows"]
    batches = after.get("scan_batch", {"rows": {}})["rows"]
    unassigned = [key for key, row in batches.items() if not row.get("scan_session_id")]
    checks["scan_sessions"] = [
        {"name": row.get("name"), "state": row.get("state"), "origin": row.get("origin")}
        for row in sessions.values()
    ]
    checks["batches"] = len(batches)
    if batches and not sessions:
        failures.append("no scan session after the backfill")
    if unassigned:
        failures.append(f"batches without a scan session: {unassigned[:5]}")
    sets = after.get("project_set", {"rows": {}})["rows"]
    checks["sets"] = [{"code": row.get("code"), "canonical_code": row.get("canonical_code")}
                      for row in sets.values()]
    if any(not row.get("canonical_code") for row in sets.values()):
        failures.append("a set without a canonical code after migration 13")
    # Integrity and Project Health, read from outside.
    report = integrity_report(database)
    checks["integrity"] = {k: report.get(k) for k in (
        "quick_check", "integrity_check", "foreign_key_check", "journal_mode")}
    if report.get("quick_check") != ["ok"] or report.get("integrity_check") != ["ok"]:
        failures.append(f"integrity: {checks['integrity']}")
    if report.get("foreign_key_check") != []:
        failures.append(f"foreign keys: {report.get('foreign_key_check')}")
    issues = report.get("health_issues", [])
    checks["health"] = [{"level": item["level"], "code": item["code"]} for item in issues]
    for item in issues:
        if item["level"] in ("error", "critical"):
            failures.append(f"health {item['level']}: {item['code']} {item['message'][:160]}")
    checks["failures"] = failures
    return checks


def upgrade(exe: Path, fixture: Path, work: Path, label: str) -> dict[str, Any]:
    """The ``upgrade`` check; returns the report payload."""
    from omr_scanner.database.migrations import SCHEMA_VERSION

    work = work.resolve()
    if work.exists():
        shutil.rmtree(work)
    project = work / "project"
    shutil.copytree(fixture, project, ignore=shutil.ignore_patterns("*.lock", "omrflow.lock"))
    database = project / "database.sqlite"
    old_version = schema_version(database)
    before = table_rows(database)
    payload: dict[str, Any] = {
        "check": "upgrade", "label": label, "fixture": str(fixture), "exe": str(exe),
        "schema_before": old_version, "schema_expected": SCHEMA_VERSION,
        "rows_before": {name: len(table["rows"]) for name, table in before.items()},
    }
    gui = InstalledGui(exe, work, project)
    failures: list[str] = []
    try:
        gui.start()
        payload["title"] = gui.title()
        deadline = time.monotonic() + 120
        while schema_version(database) != SCHEMA_VERSION and time.monotonic() < deadline:
            time.sleep(0.5)
        payload["migrated_seconds"] = round(time.time() - gui.started_at, 2)
        if gui.other_windows():
            failures.append(f"a dialog appeared on opening: {gui.other_windows()}")
        for stage in STAGES:
            gui.go(stage)
            others = gui.other_windows()
            if others:
                failures.append(f"a dialog appeared on {stage}: {others}")
        payload["stages"] = gui.seen
        payload["responding"] = gui.responding()
        if not payload["responding"]:
            failures.append("the window stopped responding")
        payload["close"] = gui.close()
        if not payload["close"]["closed_cleanly"] or payload["close"]["exit_code"] != 0:
            failures.append(f"did not close cleanly: {payload['close']}")
        payload["lock_left"] = any(project.glob("*.lock"))
        if payload["lock_left"]:
            failures.append("the project lock was left behind")
    finally:
        gui.kill()
    payload["log_problems"] = gui.log_problems()
    failures += [f"logged: {line}" for line in payload["log_problems"]]
    checks = verify_migrated(project, before, old_version)
    failures += checks.pop("failures")
    payload["checks"] = checks
    payload["failures"] = failures
    payload["result"] = "PASS" if not failures else "FAIL"
    return payload


# ----------------------------------------------------------------------
# S2 / S3 / R1 on the installed GUI after a real kill
# ----------------------------------------------------------------------
def committed_view(database: Path, session_id: str) -> dict[str, Any]:
    """What the database says, read read-only (the counts the GUI must show)."""
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        statuses = dict(connection.execute(
            "SELECT s.status, COUNT(*) FROM batch_scan s JOIN scan_batch b ON b.batch_id = "
            "s.batch_id WHERE b.scan_session_id = ? GROUP BY s.status", (session_id,)).fetchall())
        conflicts = dict(connection.execute(
            "SELECT c.state, COUNT(*) FROM review_conflict c JOIN scan_batch b ON b.batch_id = "
            "c.batch_id WHERE b.scan_session_id = ? GROUP BY c.state", (session_id,)).fetchall())
        sessions = connection.execute(
            "SELECT scan_session_id, name, state FROM scan_session").fetchall()
        batches = connection.execute(
            "SELECT COUNT(*), SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) "
            "FROM scan_batch WHERE scan_session_id = ?", (session_id,)).fetchone()
    finally:
        connection.close()
    committed = sum(int(statuses.get(k, 0)) for k in ("completed", "warning", "failed"))
    return {"statuses": statuses, "committed": committed,
            "registered": sum(int(v) for v in statuses.values()),
            "conflicts": conflicts, "sessions": sessions,
            "batches": int(batches[0] or 0), "running_batches": int(batches[1] or 0)}


def queue_summary(project: Path, session_id: str) -> dict[str, int]:
    """Resolve's queue summary computed from the committed rows by the production services.

    The same counts the Resolve page renders (``live_queue.summary_counts``:
    session-wide conflicts excluding withdrawn ones and those of rejected
    sheets, outstanding rescans, unanswered suggestions), read through a
    read-only open while the installed application holds the project - so the
    check is that the installed GUI shows the persisted state, not a recount
    of its own.
    """
    from omr_scanner.services import open_project, session_population
    from omr_scanner.services.quality_decisions import count_outstanding
    from omr_scanner.services.review_store import count_conflicts
    from omr_scanner.services.scan_lifecycle import count_cases

    session = open_project(project, read_only=True)
    try:
        database = session.database
        batches = session_population.session_batch_ids(database, session_id)
        counts = count_conflicts(database, batches[0], session_wide=True)
        rescans = count_cases(database, batches[0], session_wide=True)
        return {"total": counts.total, "unresolved": counts.unresolved,
                "resolved": counts.resolved, "withdrawn": counts.withdrawn,
                "rescans_outstanding": rescans.outstanding,
                "suggested": count_outstanding(database, session_id)}
    finally:
        session.close()


def _number(text: str) -> int | None:
    match = re.search(r"([\d,]+)", text)
    return int(match.group(1).replace(",", "")) if match else None


def gui_recovery(exe: Path, work: Path, *, template: Path) -> dict[str, Any]:
    """The ``gui-recovery`` check; returns the report payload."""
    from dataclasses import replace

    from omr_scanner.evaluation.intake_qualification.config import self_test_config
    from omr_scanner.evaluation.intake_qualification.project import create_campaign_project
    from omr_scanner.evaluation.intake_qualification.supervisor import (
        ContinuousRun,
        RunEvidence,
        coordinator_command_for,
        descendants,
        make_link,
        prepare_campaign,
    )
    from omr_scanner.evaluation.qualification import kill_run_abruptly

    work = work.resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    config = replace(self_test_config(), sources=2, candidates_per_set=30, duration_seconds=80.0,
                     workers=2, run_control=False, run_finite=False, reprocess=False)
    campaign = prepare_campaign(config, work / "campaign", template,
                                coordinator_command=coordinator_command_for(exe))
    run = ContinuousRun(campaign, "gui-recovery", campaign.plan, interrupted=True)
    for label in config.source_labels:
        (run.disk / f"Scanner_{label}").mkdir(parents=True, exist_ok=True)
        make_link(run.share / f"Scanner_{label}", run.disk / f"Scanner_{label}")
    facts = create_campaign_project(
        run.dir / "workspace", "Installed recovery check", campaign.plan, campaign.template_path,
        run.inputs, source_roots={label: run.share / f"Scanner_{label}"
                                  for label in config.source_labels},
        stability=config.stability,
    )
    run.project_template = facts.template_path
    run.evidence = RunEvidence(name=run.name, project=facts.root,
                               session_id=facts.scan_session_id, interrupted=True)
    payload: dict[str, Any] = {"check": "gui-recovery", "exe": str(exe),
                               "project": str(facts.root), "session": facts.scan_session_id}
    failures: list[str] = []
    gui: InstalledGui | None = None
    try:
        run.launch(force_lock=False, await_go=False)
        started = run.wait_started()
        payload["coordinator_runtime"] = next(iter(run.events_of("runtime")), {})
        payload["coordinator_started"] = {k: started.get(k) for k in ("seconds", "intent")}
        run.start_writers(list(campaign.plan.main_arrivals))
        total = len({item.content for item in campaign.plan.main_arrivals})

        def ready() -> bool:
            progress = run.progress()
            open_conflicts = run.facts().conflicts if progress["committed"] >= 20 else []
            return (progress["committed"] >= max(20, total // 3)
                    and progress.get("processing", 0) >= 2
                    and any(item.state == "open" for item in open_conflicts))

        run.wait_for(ready, what="committed sheets, sheets in a worker and an open conflict",
                     timeout=900)
        assert run.process is not None
        pre = run._pre_kill()
        workers = descendants(run.process.pid)
        kill = kill_run_abruptly(run.process, workers, facts.root,
                                 committed_before_kill=int(pre["committed"]))
        payload["kill"] = {"exit_code": kill.exit_code, "orphans": list(kill.orphan_pids),
                           "lock_left": kill.lock_file_left_behind, "pre": pre}
        (run.dir / "stop_writers").write_text("stop", encoding="utf-8")
        for proc in run.writers.values():
            proc.wait(timeout=120)
        if kill.orphan_pids:
            failures.append(f"worker processes outlived the kill: {list(kill.orphan_pids)}")
        # The operator's "Remove stale lock" choice: the lock belongs to a
        # process that no longer exists on this machine.
        for lock in facts.root.glob("*.lock"):
            lock.unlink()
            payload["stale_lock_removed"] = lock.name
        gui = InstalledGui(exe, work / "gui", facts.root)
        (work / "gui").mkdir(exist_ok=True)
        gui.start()
        time.sleep(3)
        database = facts.root / "database.sqlite"
        after_open = committed_view(database, facts.scan_session_id)
        payload["database_after_open"] = after_open
        # R1 first: Resolve before Scan was ever visited.
        resolve = gui.go("resolve")
        summary = resolve.get("reviewSummaryLabel", "")
        batch_label = resolve.get("reviewBatchLabel", "")
        conflicts = after_open["conflicts"]
        queue = queue_summary(facts.root, facts.scan_session_id)
        payload["R1"] = {"reviewSummaryLabel": summary, "reviewBatchLabel": batch_label,
                         "database_conflicts": conflicts, "service_summary": queue}
        shown = re.search(r"(\d+) total\s+(\d+) unresolved\s+(\d+) resolved", summary)
        r1 = []
        if "Qualification examination" not in batch_label:
            r1.append(f"Resolve does not name the persisted session: {batch_label!r}")
        if not shown:
            r1.append(f"Resolve shows no queue summary: {summary!r}")
        elif (int(shown.group(1)), int(shown.group(2)), int(shown.group(3))) != (
                queue["total"], queue["unresolved"], queue["resolved"]):
            r1.append(f"Resolve shows {summary!r}; the committed rows give {queue}")
        if sum(int(v) for v in conflicts.values()) == 0:
            r1.append("no conflict existed at the kill (R1 not exercised)")
        payload["R1"]["result"] = "PASS" if not r1 else "FAIL"
        payload["R1"]["failures"] = r1
        failures += [f"R1: {item}" for item in r1]
        # S2 / S3: Scan shows the reconstructed session from committed rows.
        scan = gui.go("scan")
        payload["S2_S3"] = {"labels": {k: v for k, v in scan.items() if any(
            word in k.lower() for word in ("progress", "state", "activity", "session", "source",
                                            "recognition", "intake", "count", "summary"))}}
        texts = list(scan.values())
        recognition = next((text for key, text in scan.items()
                            if "recognition" in key.lower() and "/" in text), "")
        if not recognition:
            recognition = next((text for text in texts
                                if re.match(r"^[\d,]+ / [\d,]+", text)), "")
        payload["S2_S3"]["recognition_text"] = recognition
        s = []
        shown = _number(recognition) if recognition else None
        if shown is None:
            s.append("Scan shows no recognition count")
        elif shown != after_open["committed"]:
            s.append(f"Scan shows {shown} read; the database has {after_open['committed']} "
                     "committed (S3)")
        if len(after_open["sessions"]) != 1:
            s.append(f"sessions after reopening: {after_open['sessions']} (S2)")
        if not any("not running in this window" in text for text in texts):
            s.append("Scan does not say processing is not running in this window (nothing "
                     "may start by itself)")
        if int(after_open["statuses"].get("processing", 0)):
            s.append("sheets still 'processing' after the open's recovery (S2)")
        payload["S2_S3"]["result"] = "PASS" if not s else "FAIL"
        payload["S2_S3"]["failures"] = s
        failures += [f"S2/S3: {item}" for item in s]
        payload["other_windows"] = gui.other_windows()
        payload["close"] = gui.close()
        if not payload["close"]["closed_cleanly"]:
            failures.append(f"did not close cleanly: {payload['close']}")
        payload["log_problems"] = gui.log_problems()
    except Exception as exc:  # report, never hide
        failures.append(f"{type(exc).__name__}: {exc}")
        run.abort()
    finally:
        if gui is not None:
            gui.kill()
        with contextlib.suppress(Exception):
            from omr_scanner.evaluation.intake_qualification.supervisor import cleanup_links

            cleanup_links(campaign.root)
    payload["failures"] = failures
    payload["result"] = "PASS" if not failures else "FAIL"
    return payload


# ----------------------------------------------------------------------
def write_report(work: Path, payload: dict[str, Any]) -> Path:
    """``report.json`` and a short ``report.md`` in ``work``."""
    work.mkdir(parents=True, exist_ok=True)
    (work / "report.json").write_text(json.dumps(payload, indent=1, default=str), "utf-8")
    lines = [f"# Installed-build check: {payload['check']} {payload.get('label', '')}".rstrip(),
             "", f"**Result: {payload['result']}**", "", f"- executable: `{payload['exe']}`"]
    for key in ("fixture", "schema_before", "schema_expected", "title", "migrated_seconds",
                "project", "session", "kill", "stale_lock_removed"):
        if key in payload:
            lines.append(f"- {key}: `{payload[key]}`")
    for key in ("R1", "S2_S3"):
        if key in payload:
            detail = {k: v for k, v in payload[key].items() if k != "result"}
            lines.append(f"- {key}: **{payload[key]['result']}** "
                         f"{json.dumps(detail, default=str)}")
    if payload.get("stages"):
        lines += ["", "## What the installed GUI showed", ""]
        for stage, labels in payload["stages"].items():
            for key, value in labels.items():
                lines.append(f"- {stage} / `{key}`: {value.splitlines()[0][:160]}")
    if payload.get("checks"):
        lines += ["", "## Checked from outside", "", "```json",
                  json.dumps({k: v for k, v in payload["checks"].items() if k != "tables"},
                             indent=1, default=str)[:6000], "```"]
    lines += ["", "## Failures", ""] + ([f"- {item}" for item in payload["failures"]] or ["none"])
    path = work / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    """Run one check; ``0`` when it passed, ``1`` when it failed, ``2`` for bad arguments."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("upgrade")
    up.add_argument("--exe", type=Path, required=True)
    up.add_argument("--fixture", type=Path, required=True)
    up.add_argument("--work", type=Path, required=True)
    up.add_argument("--label", default="")
    rec = commands.add_parser("gui-recovery")
    rec.add_argument("--exe", type=Path, required=True)
    rec.add_argument("--work", type=Path, required=True)
    rec.add_argument("--template", type=Path,
                     default=REPOSITORY_ROOT / "examples" / "templates"
                     / "synthetic_answer_sheet.omrt")
    arguments = parser.parse_args(argv)
    if not arguments.exe.is_file():
        print(f"no executable at {arguments.exe}", file=sys.stderr)
        return 2
    if arguments.command == "upgrade":
        payload = upgrade(arguments.exe.resolve(), arguments.fixture.resolve(), arguments.work,
                          arguments.label or arguments.fixture.name)
    else:
        payload = gui_recovery(arguments.exe.resolve(), arguments.work,
                               template=arguments.template.resolve())
    report = write_report(arguments.work.resolve(), payload)
    print(f"{payload['check']}: {payload['result']} - {report}")
    for item in payload["failures"][:20]:
        print(f"  {item}")
    return 0 if payload["result"] == "PASS" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
