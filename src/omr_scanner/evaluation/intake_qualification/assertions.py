"""The release-blocking assertions, the crash matrix, the endurance cases and the verdict.

Revised phase 9 (``ACCEPTANCE_CRITERIA.md`` §5.2-§5.4). The registry below is
explicit and the evaluator emits **every** name in it - a test fails if one
disappears. Each result carries what it checked and a minimum: a case the
campaign was supposed to exercise and did not is ``not_exercised``, which
fails exactly like a failed check (a vacuous pass is worse than a failure,
the Phase 10 lesson). There is no warning level.

Verdicts (generated, never typed):
    * ``FAILED`` - any required assertion, crash case or endurance case is not
      ``pass``;
    * ``QUALIFIED`` - everything passed **and** the measured scale meets the
      release requirements (sources, arrivals, sets, real images, features);
    * ``ALL RUNS PASSED — NOT THE RELEASE QUALIFICATION`` - everything passed
      below release scale.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from omr_scanner.evaluation.intake_qualification import reference
from omr_scanner.evaluation.intake_qualification.cohort import (
    CampaignPlan,
    Category,
    ContentKind,
    TaskKind,
    WritePattern,
)
from omr_scanner.evaluation.intake_qualification.config import (
    OPERATOR,
    RELEASE_MIN_ARRIVALS,
    RELEASE_MIN_SETS,
    RELEASE_MIN_SOURCES,
    REQUIRED_KILL_PERCENTS,
    VERDICT_FAILED,
    VERDICT_QUALIFIED,
    VERDICT_SMALL,
    Mode,
)

REPORT_SCHEMA_VERSION = 1

REQUIRED_ASSERTIONS: tuple[str, ...] = (
    "stable_files_discovered_exactly_once",
    "no_incomplete_file_processed",
    "source_provenance_retained",
    "duplicate_content_identified",
    "independent_filenames_do_not_collide",
    "batches_finite",
    "no_accepted_image_lost",
    "no_completed_scan_rerecognised",
    "offline_arrivals_discovered",
    "conflict_counts_correct_as_population_grows",
    "rescan_relationships_survive_restart",
    "aggregate_counts_consistent",
    "session_results_match_ground_truth",
    "sqlite_integrity",
    "application_invariants",
    "finite_mode_regression",
)
"""``ACCEPTANCE_CRITERIA.md`` §5.2, by stable name, in order. Never shortened."""

CRASH_CASES: tuple[tuple[str, str], ...] = (
    ("case_01_clean_close_during_scan", "Clean application close halfway through Scan"),
    ("case_02_forced_kill_during_scan", "Forced process termination halfway through Scan"),
    ("case_03_sheet_interrupted_in_processing", "Sheet interrupted during processing"),
    ("case_04_kill_after_commit", "Kill after a recognition commit, before the next"),
    ("case_05_clean_close_during_resolve", "Clean close halfway through Resolve"),
    ("case_06_forced_kill_after_resolve_corrections", "Forced kill after Resolve corrections"),
    ("case_07_restart_retains_corrections", "Restart retains resolved corrections"),
    ("case_08_unresolved_stay_unresolved", "Unresolved items remain unresolved"),
    ("case_09_repeated_restarts", "Repeated restarts create nothing twice"),
    ("case_10_results_after_interruption", "Session results after interruption = uninterrupted"),
    ("case_11_resume_percentages", "Resume at about 1 / 25 / 50 / 75 / 99 %"),
    ("case_12_integrity_after_every_case", "Integrity after every case"),
    ("case_13_kill_before_conflict_generation",
     "Kill after recognition, before conflict generation"),
    ("case_14_sealed_batch_interrupted", "Sealed batch interrupted mid-processing"),
    ("case_15_resolve_reachable_after_reopen", "Resolve reachable after reopen without Scan"),
)
"""``ACCEPTANCE_CRITERIA.md`` §5.4, by stable id."""

ENDURANCE_CASES: tuple[tuple[str, str], ...] = (
    ("endurance_a_many_finite_batches", ">= 10 sealed batches in one session, several sources"),
    ("endurance_b_continuous_random_intake", "Continuous random intake with operator decisions"),
    ("endurance_c_supersession_reprocess", "Reprocess All mid-session: nothing counted twice"),
    ("endurance_d_repeated_kill_restart", "Repeated real kills and restarts"),
    ("endurance_e_uninterrupted_control", "Interrupted results = uninterrupted control"),
)
"""``ACCEPTANCE_CRITERIA.md`` §5.3, by stable id."""

PASS = "pass"
FAIL = "fail"
NOT_EXERCISED = "not_exercised"
NOT_EVALUATED = "not_evaluated"
STATUSES = (PASS, FAIL, NOT_EXERCISED, NOT_EVALUATED)

MAX_LISTED = 25


@dataclass
class CheckResult:
    """One assertion's (or case's) outcome, with its evidence."""

    name: str
    title: str = ""
    status: str = NOT_EVALUATED
    checked: int = 0
    minimum: int = 1
    failures: list[str] = field(default_factory=list)
    failure_count: int = 0
    """Every failure recorded - the list keeps only the first :data:`MAX_LISTED`.
    A field of its own, so an assertion that rebuilds :attr:`evidence` can
    never wipe out the record that it failed."""
    evidence: dict[str, Any] = field(default_factory=dict)
    scale: str = ""

    def fail(self, message: str) -> None:
        """Record one failure (listing only the first :data:`MAX_LISTED`)."""
        if len(self.failures) < MAX_LISTED:
            self.failures.append(message)
        self.failure_count += 1

    def settle(self) -> CheckResult:
        """Decide the status from failures and the exercise minimum."""
        if self.failure_count or self.failures:
            self.status = FAIL
        elif self.checked < self.minimum:
            self.status = NOT_EXERCISED
        else:
            self.status = PASS
        return self

    @property
    def passed(self) -> bool:
        """Whether the status is :data:`PASS`."""
        return self.status == PASS

    def to_json(self) -> dict[str, Any]:
        """The result as JSON-safe data."""
        data: dict[str, Any] = _plain(asdict(self))
        return data


def _plain(value: Any) -> Any:
    """JSON-safe: tuples to lists, sets sorted, datetimes ISO, Fractions as text."""
    from fractions import Fraction

    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted(_plain(item) for item in value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Fraction):
        return str(value)
    return value


def _epoch(moment: datetime | None) -> float | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


# ----------------------------------------------------------------------
# Checkpoints and recoveries (evaluated by the supervisor as they happen)
# ----------------------------------------------------------------------
def committed_facts(facts: Any, sha_to_content: dict[str, str]) -> reference.CommittedFacts:
    """The reference's inputs: committed sheets and the operator's committed decisions."""
    conflicts = {item.conflict_id: item for item in facts.conflicts}
    corrected: set[int] = set()
    accepted: set[int] = set()
    acknowledged: set[int] = set()
    for event in facts.audit:
        if event.reviewer != OPERATOR or event.conflict_id is None:
            continue
        conflict = conflicts.get(event.conflict_id)
        if conflict is None:
            continue
        if event.action == "corrected" and conflict.kind.startswith("identifier_"):
            corrected.add(conflict.scan_id)
        elif event.action == "accepted" and conflict.kind == "identifier_duplicate":
            accepted.add(conflict.scan_id)
        elif event.action == "accepted" and conflict.kind == "registration_failed":
            acknowledged.add(conflict.scan_id)
    rejected = frozenset(
        scan for scan, (state, _replacement) in facts.rejections.items() if state != "active"
    )
    sheets = {
        item.scan_id: sha_to_content[item.sha]
        for item in facts.committed() if item.sha in sha_to_content
    }
    return reference.CommittedFacts(
        sheets=sheets, corrected=frozenset(corrected), accepted_duplicate=frozenset(accepted),
        acknowledged=frozenset(acknowledged), rejected=rejected,
    )


def actual_open_conflicts(facts: Any, sha_to_content: dict[str, str]) -> set[tuple[str, str]]:
    live = {item.scan_id: item for item in facts.committed()}
    rejected = {scan for scan, (state, _r) in facts.rejections.items() if state != "active"}
    found: set[tuple[str, str]] = set()
    for conflict in facts.conflicts:
        if conflict.state != "open" or conflict.scan_id not in live:
            continue
        if conflict.scan_id in rejected:
            continue
        key = sha_to_content.get(live[conflict.scan_id].sha, f"?{conflict.scan_id}")
        found.add((key, conflict.kind))
    return found


def evaluate_checkpoint(
    plan: CampaignPlan,
    facts: Any,
    snapshot: Any,
    population: Any,
    *,
    sha_to_content: dict[str, str],
    seal_digests: dict[str, str],
) -> dict[str, Any]:
    """One consistent checkpoint: conflicts vs the reference, the partition vs a recount."""
    from omr_scanner.evaluation.intake_qualification.inspect import recount_partition

    failures: list[str] = []
    expected = reference.expected_open_conflicts(
        plan, committed_facts(facts, sha_to_content)
    )
    actual = actual_open_conflicts(facts, sha_to_content)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing:
        failures.append(f"conflicts missing: {missing[:10]}")
    if unexpected:
        failures.append(f"conflicts unexpected: {unexpected[:10]}")
    buckets = snapshot.partition.as_dict()
    recount = recount_partition(facts)
    if snapshot.partition.total != snapshot.discovered_excluding_ignored:
        failures.append(
            f"partition {snapshot.partition.total} != discovered "
            f"{snapshot.discovered_excluding_ignored}"
        )
    differing = {name: (value, recount.get(name, 0)) for name, value in buckets.items()
                 if value != recount.get(name, 0)}
    if differing:
        failures.append(f"snapshot vs recount: {differing}")
    effective = len(population.effective)
    counted = buckets["accepted"] + buckets["conflict"] + snapshot.outstanding_suggestions
    if effective != counted:
        failures.append(
            f"effective population {effective} != accepted+conflict+suggested {counted}"
        )
    batch_failures = []
    for batch in facts.session_batches():
        recorded = seal_digests.get(batch.batch_id)
        if recorded is not None and batch.digest != recorded:
            batch_failures.append(f"sealed batch {batch.batch_id[:8]} membership changed")
    outside = [b.batch_id for b in facts.batches.values() if b.session_id != facts.session_id]
    if outside:
        batch_failures.append(f"batches outside the session: {outside[:5]}")
    failures += batch_failures
    duplicate_expected = sum(1 for _key, kind in expected if kind == "identifier_duplicate")
    return {
        "ok": not failures,
        "failures": failures,
        "committed": len(facts.committed()),
        "conflicts": {"expected": len(expected), "actual": len(actual),
                      "duplicate_expected": duplicate_expected,
                      "missing": missing[:20], "unexpected": unexpected[:20],
                      "ok": not missing and not unexpected},
        "aggregate": {"snapshot": buckets, "recount": recount,
                      "discovered": snapshot.discovered_excluding_ignored,
                      "effective": effective, "ok": not differing and effective == counted
                      and snapshot.partition.total == snapshot.discovered_excluding_ignored},
        "batches": {"session": len(facts.session_batches()),
                    "sealed": sum(1 for b in facts.session_batches() if b.sealed),
                    "ok": not batch_failures},
    }


def summarize_down_state(facts: Any) -> dict[str, Any]:
    """What a kill (or close) left durably, for the comparison after the restart."""
    committed = {item.scan_id: (item.status, item.attempts) for item in facts.committed()}
    processing = sorted(item.scan_id for item in facts.session_sheets()
                        if item.status == "processing")
    audit_digest = hashlib.sha256(
        json.dumps([[a.event_id, a.action, a.reviewer, a.scan_id, a.conflict_id]
                    for a in facts.audit]).encode()
    ).hexdigest()
    rejected = {scan for scan, (state, _r) in facts.rejections.items() if state != "active"}
    live = {item.scan_id for item in facts.committed()}
    open_ids = sorted(c.conflict_id for c in facts.conflicts
                      if c.state == "open" and c.scan_id in live and c.scan_id not in rejected)
    resolved_live = sorted(c.conflict_id for c in facts.conflicts
                           if c.state == "resolved" and c.scan_id in live
                           and c.scan_id not in rejected)
    unfinished_sealed = sorted({
        item.batch_id for item in facts.session_sheets()
        if item.status in ("pending", "queued", "processing", "cancelled")
        and facts.batches[item.batch_id].sealed
    })
    return {
        "committed_ids": sorted(committed),
        "failed_ids": sorted(
            scan for scan, (status, _a) in committed.items() if status == "failed"
        ),
        "committed": {str(k): list(v) for k, v in committed.items()},
        "processing_ids": processing,
        "audit_count": len(facts.audit),
        "audit_last": facts.audit[-1].event_id if facts.audit else 0,
        "audit_digest": audit_digest,
        "operator_events": sum(1 for a in facts.audit if a.reviewer == OPERATOR),
        "conflicts": len(facts.conflicts),
        "open_conflict_ids": open_ids,
        "resolved_live_conflict_ids": resolved_live,
        "resolved_conflict_ids": sorted(c.conflict_id for c in facts.conflicts
                                        if c.state == "resolved"),
        "sessions": [list(item) for item in facts.sessions],
        "batches": {b.batch_id: [b.sealed, b.digest, b.status] for b in facts.batches.values()},
        "rejections": {str(k): list(v) for k, v in facts.rejections.items()},
        "unfinished_sealed_batches": unfinished_sealed,
        "running_batches": sorted(b.batch_id for b in facts.session_batches()
                                  if b.status == "running"),
        "supersessions": facts.supersessions,
        "_audit_rows": [[a.event_id, a.action, a.reviewer, a.scan_id, a.conflict_id]
                        for a in facts.audit],
        "_conflict_rows": [[c.conflict_id, c.scan_id, c.kind, c.zone, c.group, c.state]
                           for c in facts.conflicts],
    }


def compare_recovery(down: Any, recovered: Any) -> dict[str, Any]:
    """What the restart sequence changed - before the restarted engine did any new work.

    Must hold: the same session(s), the same batches (sealed ones with the same
    members), every committed sheet still committed with its attempt count,
    every in-flight sheet back to ``pending`` (never ``completed`` or
    ``failed`` by recovery), the audit history unchanged as a prefix with no
    operator event added, the rejections and replacement links unchanged, and
    no conflict identity duplicated. Conflicts recovery adds are listed (the
    duplicate pass a kill cut short).
    """
    before = summarize_down_state(down)
    after = summarize_down_state(recovered)
    failures: list[str] = []
    if before["sessions"] != after["sessions"]:
        failures.append(f"sessions changed: {before['sessions']} -> {after['sessions']}")
    new_batches = sorted(set(after["batches"]) - set(before["batches"]))
    if new_batches:
        failures.append(f"recovery created batch(es) {new_batches}")
    for batch_id, (sealed, digest, _status) in before["batches"].items():
        now = after["batches"].get(batch_id)
        if now is None:
            failures.append(f"batch {batch_id[:8]} disappeared")
        elif sealed and (not now[0] or now[1] != digest):
            failures.append(f"sealed batch {batch_id[:8]} changed by recovery")
    for scan, (status, attempts) in before["committed"].items():
        now = after["committed"].get(scan)
        if now is None or now[0] != status or now[1] != attempts:
            failures.append(f"committed sheet {scan} changed: {[status, attempts]} -> {now}")
    statuses = {item.scan_id: item.status for item in recovered.session_sheets()}
    returned = [scan for scan in before["processing_ids"] if statuses.get(scan) == "pending"]
    wrong = [scan for scan in before["processing_ids"]
             if statuses.get(scan) not in ("pending", "cancelled")]
    if wrong:
        failures.append(f"in-flight sheets not returned to pending: {wrong[:10]}")
    rows_before, rows_after = before["_audit_rows"], after["_audit_rows"]
    if rows_after[: len(rows_before)] != rows_before:
        failures.append("audit history before the kill is not a prefix of the history after")
    added = rows_after[len(rows_before):]
    added_operator = [row for row in added if row[2] == OPERATOR]
    if added_operator:
        failures.append(f"recovery added operator audit events: {added_operator[:5]}")
    if before["rejections"] != after["rejections"]:
        failures.append("rejections or replacement links changed by recovery")
    identities = Counter(
        (row[1], row[2], row[3], row[4]) for row in after["_conflict_rows"]
    )
    doubled = [
        key for key, count in identities.items() if count > 1 and key[1] != "manual_override"
    ]
    if doubled:
        failures.append(f"conflict identities duplicated: {doubled[:5]}")
    before_ids = {row[0] for row in before["_conflict_rows"]}
    added_conflicts = [row for row in after["_conflict_rows"] if row[0] not in before_ids]
    resolved_lost = sorted(
        set(before["resolved_conflict_ids"]) - set(after["resolved_conflict_ids"])
    )
    if resolved_lost:
        failures.append(f"resolved conflicts no longer resolved: {resolved_lost[:10]}")
    return {
        "ok": not failures,
        "failures": failures,
        "returned": len(returned),
        "in_flight_at_kill": len(before["processing_ids"]),
        "committed_at_kill": len(before["committed_ids"]),
        "added_conflicts": [row[:3] for row in added_conflicts][:50],
        "added_audit_actions": dict(Counter(row[1] for row in added)),
        "unfinished_sealed_batches": before["unfinished_sealed_batches"],
        "running_batches_at_kill": before["running_batches"],
        "open_conflict_ids_at_kill": before["open_conflict_ids"],
        "operator_events_at_kill": before["operator_events"],
        "committed_ids": before["committed_ids"],
    }


# ----------------------------------------------------------------------
# Evidence indexes
# ----------------------------------------------------------------------
@dataclass
class RunIndex:
    """Lookups over one run's evidence."""

    writes: dict[tuple[str, str], dict[str, Any]]
    first_claim: dict[int, float]
    claims_by_incarnation: dict[int, set[int]]
    ledger_by_path: dict[tuple[str, str], list[Any]]
    content_of_sha: dict[str, str]
    scan_content: dict[int, str]

    @classmethod
    def build(cls, run: Any, sha_to_content: dict[str, str]) -> RunIndex:
        """Index one run's writer and coordinator logs and its final facts."""
        writes = {
            (item["source"], item["name"]): item
            for item in run.writer_events if item["event"] == "write_completed"
        }
        first: dict[int, float] = {}
        by_incarnation: dict[int, set[int]] = defaultdict(set)
        for item in run.coordinator_events:
            if item["event"] != "claimed":
                continue
            incarnation = int(str(item["role"]).split(":")[1])
            for scan in item["scans"]:
                first[int(scan)] = min(first.get(int(scan), float("inf")), float(item["t"]))
                by_incarnation[incarnation].add(int(scan))
        ledger: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for row in run.final_facts.ledger:
            label = row.source_label.removeprefix("Scanner ").strip()
            ledger[(label, row.relative_path)].append(row)
        scan_content = {
            item.scan_id: sha_to_content[item.sha]
            for item in run.final_facts.sheets.values() if item.sha in sha_to_content
        }
        return cls(writes, first, by_incarnation, ledger, sha_to_content, scan_content)


def effective_contents_of(
    facts: Any, population: Any, sha_to_content: dict[str, str]
) -> Counter[str]:
    found: Counter[str] = Counter()
    for scan in population.effective:
        sheet = facts.sheets.get(scan)
        if sheet is not None:
            found[sha_to_content.get(sheet.sha, f"?{scan}")] += 1
    return found


# ----------------------------------------------------------------------
# The sixteen assertions
# ----------------------------------------------------------------------
def a_stable_files(plan: CampaignPlan, run: Any, index: RunIndex) -> CheckResult:
    result = CheckResult("stable_files_discovered_exactly_once")
    planned = {(item.source, item.name) for item in plan.arrivals}
    result.minimum = len(planned)
    used_scans: Counter[int] = Counter()
    for key in sorted(planned):
        write = index.writes.get(key)
        if write is None:
            result.fail(f"{key} was never completely written")
            continue
        rows = index.ledger_by_path.get(key, [])
        if len(rows) != 1:
            result.fail(f"{key}: {len(rows)} ledger rows")
            continue
        row = rows[0]
        if row.sha != write["sha256"]:
            result.fail(f"{key}: ledger hash differs from the written bytes")
        if row.state not in ("registered", "duplicate_content"):
            result.fail(f"{key}: ledger state {row.state}")
        if row.state == "registered":
            if row.batch_scan_id is None:
                result.fail(f"{key}: registered without a sheet")
            else:
                used_scans[row.batch_scan_id] += 1
        result.checked += 1
    known = {(item.source, item.name) for item in plan.arrivals}
    watched = set(plan.config.source_labels)
    manual = 0
    for key, rows in index.ledger_by_path.items():
        if key in known:
            continue
        if key[0] not in watched:
            # The built-in manual source: *Reprocess All* registers the
            # project copies it re-reads there (the engine's registration
            # step for a batch intake did not register) - not a discovery.
            manual += len(rows)
            continue
        for row in rows:
            if not (row.state == "ignored" and key[1].endswith(".part")):
                result.fail(f"unplanned ledger row {key} ({row.state})")
    for scan, count in used_scans.items():
        if count > 1:
            result.fail(f"sheet {scan} registered from {count} ledger rows")
    effective = effective_contents_of(run.final_facts, run.final_population, index.content_of_sha)
    for content, count in effective.items():
        if count > 1:
            result.fail(f"content {content} has {count} effective sheets")
    result.evidence = {**result.evidence, "written_files": result.checked,
                       "manual_source_rows": manual,
                       "ledger_rows": sum(len(v) for v in index.ledger_by_path.values()),
                       "temporary_names_ignored": sum(
                           1 for k, v in index.ledger_by_path.items()
                           if k[1].endswith(".part") for _ in v)}
    return result.settle()


def a_no_incomplete(plan: CampaignPlan, run: Any, index: RunIndex) -> CheckResult:
    result = CheckResult("no_incomplete_file_processed")
    patterns = Counter(item.pattern.value for item in plan.arrivals)
    partial = sum(
        count for pattern, count in patterns.items() if pattern != WritePattern.ATOMIC.value
    )
    margins: list[float] = []
    sheets = run.final_facts.sheets
    watched = set(plan.config.source_labels)
    for key, rows in index.ledger_by_path.items():
        if key[0] not in watched:
            continue  # the built-in manual source (Reprocess All): no writer, see provenance
        for row in rows:
            if row.state != "registered" or row.batch_scan_id is None:
                continue
            write = index.writes.get(key)
            if write is None:
                result.fail(f"{key} registered but never completely written")
                continue
            done = float(write["write_completed_at"])
            registered = _epoch(row.registered_at)
            if registered is not None and registered < done:
                result.fail(f"{key} registered {done - registered:.3f}s before its writer finished")
            sheet = sheets.get(row.batch_scan_id)
            if sheet is not None and sheet.sha != write["sha256"]:
                result.fail(f"{key}: the sheet's bytes are not the completed file's")
            claim = index.first_claim.get(row.batch_scan_id)
            if claim is None:
                if sheet is not None and sheet.status in ("completed", "warning", "failed"):
                    result.fail(f"{key}: read, but no submission was logged")
                continue
            if claim < done:
                result.fail(f"{key} submitted {done - claim:.3f}s before its writer finished")
            margins.append(claim - done)
            result.checked += 1
    result.minimum = max(1, len({item.content for item in plan.arrivals}) - 5)
    if partial == 0:
        result.fail("no partial-write pattern was planned")
    margins.sort()
    result.evidence = {
        "patterns": dict(patterns), "partial_writes": partial,
        "min_seconds_completion_to_submission": round(margins[0], 3) if margins else None,
        "median_seconds_completion_to_submission":
            round(margins[len(margins) // 2], 3) if margins else None,
    }
    return result.settle()


def a_provenance(plan: CampaignPlan, run: Any, index: RunIndex, share_root: Any) -> CheckResult:
    result = CheckResult("source_provenance_retained")
    sheets = run.final_facts.sheets
    batches = run.final_facts.batches
    watched = set(plan.config.source_labels)
    traced = 0
    for (source, name), rows in index.ledger_by_path.items():
        if source not in watched:
            for row in rows:
                if row.state == "registered" and row.batch_scan_id is not None:
                    traced += 1
                    trace = _trace_reprocessed(run, index, row.batch_scan_id)
                    if trace:
                        result.fail(trace)
            continue
        for row in rows:
            if row.state != "registered" or row.batch_scan_id is None:
                continue
            result.checked += 1
            expected_path = str(share_root / f"Scanner_{source}" / name)
            if row.file_name != name or row.relative_path != name:
                result.fail(
                    f"{source}/{name}: ledger names {row.relative_path!r}/{row.file_name!r}"
                )
            if row.absolute_path != expected_path:
                result.fail(f"{source}/{name}: observed path {row.absolute_path!r}")
            sheet = sheets.get(row.batch_scan_id)
            if sheet is None:
                result.fail(f"{source}/{name}: sheet {row.batch_scan_id} missing")
                continue
            if sheet.intake_file_id != row.intake_file_id:
                result.fail(f"{source}/{name}: sheet does not point back to its ledger row")
            batch = batches.get(sheet.batch_id)
            if batch is None or batch.source_id != row.source_id:
                result.fail(f"{source}/{name}: unit not attributed to its source")
    names = getattr(run, "original_names", None) or {}
    for scan, shown in names.items():
        content = index.scan_content.get(int(scan))
        if content is None:
            continue
        planned = {item.name for item in plan.arrivals if item.content == content}
        if shown not in planned:
            result.fail(f"sheet {scan} shown as {shown!r}, not an arrival name of its content")
    result.minimum = 1
    result.evidence = {"registered_files_checked": result.checked,
                       "original_names_checked": len(names),
                       "reprocessed_sheets_traced": traced}
    return result.settle()


def _trace_reprocessed(run: Any, index: RunIndex, scan_id: int) -> str:
    """A *Reprocess All* sheet's provenance: its content's sheet in the batch it re-read.

    *Reprocess All* registers the project copies it re-reads in the built-in
    manual source (the engine's registration step for a batch intake did not
    register). The scanner, path and name are then those of the superseded
    unit's sheet with the same bytes - which must exist and itself be a
    watched arrival. Returns ``""`` when traced, else the failure.
    """
    facts = run.final_facts
    sheet = facts.sheets.get(scan_id)
    if sheet is None:
        return f"reprocessed sheet {scan_id} missing"
    batch = facts.batches.get(sheet.batch_id)
    if batch is None or batch.role != "reprocess":
        return f"sheet {scan_id} registered in the manual source outside a Reprocess All"
    ledger = {row.intake_file_id: row for rows in index.ledger_by_path.values() for row in rows}
    for other in facts.sheets.values():
        if other.sha != sheet.sha or other.batch_id not in facts.superseded_batches:
            continue
        origin = ledger.get(other.intake_file_id) if other.intake_file_id is not None else None
        if origin is not None and origin.source_label.startswith("Scanner "):
            label = origin.source_label.removeprefix("Scanner ").strip()
            if (label, origin.relative_path) in index.writes:
                return ""
    return f"reprocessed sheet {scan_id}: no watched arrival behind it"


def a_duplicates(plan: CampaignPlan, run: Any, index: RunIndex) -> CheckResult:  # noqa: ARG001 - uniform a_* signature
    result = CheckResult("duplicate_content_identified")
    groups = reference.duplicate_groups(plan)
    planted_copies = 0
    within = across = same_name_across = 0
    expected_copies: set[tuple[str, str]] = set()
    for _key, places in groups.items():
        rows = [index.ledger_by_path.get(place, [None])[0] for place in places]
        registered = [(place, row) for place, row in zip(places, rows, strict=True)
                      if row is not None and row.state == "registered"]
        if len(registered) != 1:
            result.fail(f"copies {places}: {len(registered)} registered (expected 1)")
            continue
        keeper_place, keeper = registered[0]
        for place, row in zip(places, rows, strict=True):
            if place == keeper_place:
                continue
            planted_copies += 1
            expected_copies.add(place)
            if place[0] == keeper_place[0]:
                within += 1
            else:
                across += 1
                if place[1] == keeper_place[1]:
                    same_name_across += 1
            if row is None or row.state != "duplicate_content":
                result.fail(
                    f"{place}: {row.state if row else 'no ledger row'} (expected a duplicate)"
                )
            elif row.duplicate_of_scan_id != keeper.batch_scan_id:
                result.fail(
                    f"{place}: linked to {row.duplicate_of_scan_id}, not {keeper.batch_scan_id}"
                )
            result.checked += 1
    flagged = {key for key, rows in index.ledger_by_path.items()
               for row in rows if row.state == "duplicate_content"}
    false_positive = sorted(flagged - expected_copies)
    if false_positive:
        result.fail(f"not planted, yet classified duplicate content: {false_positive[:10]}")
    result.minimum = 1
    result.evidence = {"planted_copies": planted_copies, "within_source": within,
                       "across_sources": across, "same_name_across_sources": same_name_across,
                       "flagged": len(flagged), "false_positives": len(false_positive)}
    if within == 0 or across == 0:
        result.fail("byte copies must be planted both within and across sources")
    return result.settle()


def a_names(plan: CampaignPlan, run: Any, index: RunIndex) -> CheckResult:  # noqa: ARG001 - uniform a_* signature
    result = CheckResult("independent_filenames_do_not_collide")
    by_name: dict[str, list[Any]] = defaultdict(list)
    for item in plan.arrivals:
        by_name[item.name].append(item)
    shared = {name: items for name, items in by_name.items()
              if len({item.source for item in items}) > 1}
    distinct_bytes = 0
    for name, items in shared.items():
        rows = []
        for item in items:
            found = index.ledger_by_path.get((item.source, name), [])
            if len(found) != 1:
                result.fail(f"{item.source}/{name}: {len(found)} ledger rows")
                continue
            rows.append((item, found[0]))
            result.checked += 1
        if len({row.intake_file_id for _item, row in rows}) != len(rows):
            result.fail(f"{name}: one ledger row shared across sources")
        contents = Counter(item.content for item, _row in rows)
        for item, row in rows:
            if contents[item.content] == 1 and not _is_planted_copy(plan, item):
                distinct_bytes += 1
                if row.state != "registered":
                    result.fail(f"{item.source}/{name}: {row.state}, though its bytes are its own")
    result.minimum = 2
    result.evidence = {"names_on_several_sources": len(shared), "files_checked": result.checked,
                       "independent_registered": distinct_bytes}
    return result.settle()


def _is_planted_copy(plan: CampaignPlan, arrival: Any) -> bool:
    return sum(1 for item in plan.main_arrivals if item.content == arrival.content) > 1


def a_batches(plan: CampaignPlan, run: Any) -> CheckResult:  # noqa: ARG001 - uniform a_* signature
    result = CheckResult("batches_finite")
    facts = run.final_facts
    session = run.session_id
    for batch in facts.batches.values():
        result.checked += 1
        if batch.session_id != session:
            result.fail(f"batch {batch.batch_id[:8]} belongs to {batch.session_id}")
        if not batch.sealed:
            result.fail(f"batch {batch.batch_id[:8]} not sealed at the end (session closed)")
        if batch.total != len(batch.members):
            result.fail(
                f"batch {batch.batch_id[:8]}: total {batch.total} != members {len(batch.members)}"
            )
        recorded = run.seal_digests.get(batch.batch_id)
        if recorded is not None and recorded != batch.digest:
            result.fail(f"sealed batch {batch.batch_id[:8]} gained or lost a member")
    for checkpoint in run.checkpoints:
        if not checkpoint["batches"]["ok"]:
            result.fail(f"checkpoint {checkpoint['label']}: {checkpoint['failures']}")
    for kill in run.kills:
        for failure in kill.restart.get("failures", []):
            if "batch" in failure:
                result.fail(f"{kill.label}: {failure}")
    sources = Counter(b.source_id for b in facts.batches.values() if b.source_id)
    result.evidence = {"batches": len(facts.batches),
                       "sealed": sum(1 for b in facts.batches.values() if b.sealed),
                       "watched_sources_with_units": len(sources),
                       "digests_rechecked": len(run.seal_digests)}
    result.minimum = 1
    return result.settle()


def a_no_lost(plan: CampaignPlan, run: Any, index: RunIndex) -> CheckResult:
    result = CheckResult("no_accepted_image_lost")
    expected = reference.effective_contents(plan, phase="reopen")
    actual = effective_contents_of(run.final_facts, run.final_population, index.content_of_sha)
    missing = sorted(expected - set(actual))
    extra = sorted(set(actual) - expected)
    if missing:
        result.fail(f"effective sheets lost: {missing[:20]}")
    if extra:
        result.fail(f"effective sheets not expected: {extra[:20]}")
    for kill in run.kills:
        for failure in kill.restart.get("failures", []):
            if failure.startswith("committed sheet") or "disappeared" in failure:
                result.fail(f"{kill.label}: {failure}")
    result.checked = len(expected)
    result.minimum = 1
    result.evidence = {"expected_effective": len(expected),
                       "actual_effective": sum(actual.values()),
                       "kills_checked": len(run.kills)}
    return result.settle()


def a_not_reread(plan: CampaignPlan, run: Any, index: RunIndex) -> CheckResult:  # noqa: ARG001 - uniform a_* signature
    result = CheckResult("no_completed_scan_rerecognised")
    per_kill = []
    for kill in run.kills:
        committed = set(kill.restart.get("committed_ids", ()))
        later = set()
        for incarnation, scans in index.claims_by_incarnation.items():
            if incarnation > kill.incarnation:
                later |= scans
        again = sorted(committed & later)
        if again:
            result.fail(f"{kill.label}: committed sheets submitted again: {again[:10]}")
        if not committed:
            result.fail(f"{kill.label}: nothing was committed before it (vacuous)")
        result.checked += len(committed)
        per_kill.append({"kill": kill.label, "committed_before": len(committed),
                         "resubmitted": len(again)})
        for failure in kill.restart.get("failures", []):
            if failure.startswith("committed sheet"):
                result.fail(f"{kill.label}: {failure}")
    multiple: Counter[tuple[str, int]] = Counter()
    for item in run.coordinator_events:
        if item["event"] == "claimed":
            multiple.update((item["role"], int(scan)) for scan in item["scans"])
    claimed_twice_same = sum(1 for count in multiple.values() if count > 1)
    if claimed_twice_same:
        result.fail(f"{claimed_twice_same} sheet(s) claimed twice by one incarnation")
    result.minimum = 1
    result.evidence = {"kills": per_kill, "claimed_twice_in_one_incarnation": claimed_twice_same}
    return result.settle()


def a_offline(plan: CampaignPlan, run: Any, index: RunIndex) -> CheckResult:  # noqa: ARG001 - uniform a_* signature
    result = CheckResult("offline_arrivals_discovered")
    intervals = run.resource.get("down_intervals", [])
    outages = [(o["start"], o.get("end", o["start"])) for o in run.outages]
    offline = outage = 0
    for (source, name), write in index.writes.items():
        done = float(write["write_completed_at"])
        while_down = any(start <= done <= end for start, end in intervals)
        during_outage = any(start <= done <= end for start, end in outages) and any(
            o["source"] == source for o in run.outages)
        if not (while_down or during_outage):
            continue
        rows = index.ledger_by_path.get((source, name), [])
        if len(rows) != 1 or rows[0].state not in ("registered", "duplicate_content"):
            result.fail(f"{source}/{name} written while {'down' if while_down else 'unreachable'}"
                        f" was not discovered ({[r.state for r in rows]})")
        offline += while_down
        outage += during_outage
        result.checked += 1
    seen = [o for o in run.outages if o.get("seen_unreachable")]
    if run.interrupted:
        if not run.outages:
            result.fail("no source outage was performed")
        elif not seen:
            result.fail("the source outage was never recorded as unreachable")
        if offline == 0:
            result.fail("no file was written while the coordinator was down")
        if outage == 0:
            result.fail("no file was written during the source outage")
    result.minimum = 1
    result.evidence = {"written_while_coordinator_down": offline,
                       "written_while_source_unreachable": outage,
                       "down_intervals": len(intervals), "outages": _plain(run.outages)}
    return result.settle()


def a_conflict_growth(plan: CampaignPlan, run: Any) -> CheckResult:  # noqa: ARG001 - uniform a_* signature
    result = CheckResult("conflict_counts_correct_as_population_grows")
    with_duplicates = 0
    for checkpoint in run.checkpoints:
        conflicts = checkpoint["conflicts"]
        result.checked += conflicts["expected"] + conflicts["actual"]
        if not conflicts["ok"]:
            result.fail(f"{checkpoint['label']}: missing {conflicts['missing'][:5]}, "
                        f"unexpected {conflicts['unexpected'][:5]}")
        if conflicts["duplicate_expected"]:
            with_duplicates += 1
    if len(run.checkpoints) < 2:
        result.fail("fewer than two checkpoints during growth")
    if with_duplicates == 0:
        result.fail("no checkpoint saw an open duplicate-ID conflict (not exercised)")
    result.minimum = 1
    result.evidence = {
        "checkpoints": [
            {"label": c["label"], "committed": c["committed"],
             "expected": c["conflicts"]["expected"], "actual": c["conflicts"]["actual"],
             "duplicate_expected": c["conflicts"]["duplicate_expected"]}
            for c in run.checkpoints
        ],
    }
    return result.settle()


def a_rescans(plan: CampaignPlan, run: Any, index: RunIndex) -> CheckResult:
    result = CheckResult("rescan_relationships_survive_restart")
    facts = run.final_facts
    effective = set(run.final_population.effective)
    scans_of: dict[str, list[int]] = defaultdict(list)
    for scan, key in index.scan_content.items():
        scans_of[key].append(scan)
    source_of = {item.content: item.source for item in plan.main_arrivals}
    cross = chains = 0
    actions: dict[int, list[str]] = defaultdict(list)
    for event in facts.audit:
        if event.scan_id is not None:
            actions[event.scan_id].append(event.action)
    for task in plan.tasks:
        if task.kind is not TaskKind.CONFIRM_REPLACEMENT:
            continue
        result.checked += 1
        originals = scans_of.get(task.content, [])
        replacements = scans_of.get(task.value, [])
        if len(originals) != 1 or len(replacements) != 1:
            result.fail(f"{task.content}->{task.value}: sheets {originals} -> {replacements}")
            continue
        original, replacement = originals[0], replacements[0]
        state, linked = facts.rejections.get(original, ("active", None))
        if state != "superseded_by_replacement" or linked != replacement:
            result.fail(
                f"{task.content}: state {state}, linked to {linked}, expected {replacement}"
            )
        if original in effective:
            result.fail(f"{task.content}: the superseded original still counts")
        history = actions.get(original, [])
        if "rejected" not in history or "replaced" not in history:
            result.fail(f"{task.content}: lifecycle history {history}")
        if source_of.get(task.content) != source_of.get(task.value):
            cross += 1
        if task.content in {t.value for t in plan.tasks if t.kind is TaskKind.CONFIRM_REPLACEMENT}:
            chains += 1
    final_replacements = {t.value for t in plan.tasks if t.kind is TaskKind.CONFIRM_REPLACEMENT} - {
        t.content for t in plan.tasks if t.kind is TaskKind.CONFIRM_REPLACEMENT}
    for key in final_replacements:
        if not any(scan in effective for scan in scans_of.get(key, [])):
            result.fail(f"replacement {key} does not count")
    for kill in run.kills:
        for failure in kill.restart.get("failures", []):
            if "rejection" in failure or "replacement" in failure:
                result.fail(f"{kill.label}: {failure}")
    if cross == 0:
        result.fail("no replacement arrived from a different source")
    if chains == 0:
        result.fail("no replacement chain was exercised")
    restarts_after = sum(1 for kill in run.kills if kill.restart.get("ok") is not None)
    result.minimum = 1
    result.evidence = {"links": result.checked, "cross_source": cross, "chains": chains,
                       "restarts_checked": restarts_after}
    return result.settle()


def a_aggregate(plan: CampaignPlan, run: Any, final_checkpoint: dict[str, Any]) -> CheckResult:  # noqa: ARG001 - uniform a_* signature
    result = CheckResult("aggregate_counts_consistent")
    for checkpoint in [*run.checkpoints, final_checkpoint]:
        result.checked += 1
        if not checkpoint["aggregate"]["ok"]:
            result.fail(f"{checkpoint['label']}: {checkpoint['aggregate']}")
    result.minimum = 3
    result.evidence = {"checkpoints": result.checked,
                       "final": final_checkpoint["aggregate"]}
    return result.settle()


def results_vs_truth(
    plan: CampaignPlan, outcomes_by_phase: dict[str, Any], *, label: str,
) -> tuple[int, list[str], dict[str, Any]]:
    """Compare one run's attendance, marks and report cells with the reference."""
    failures: list[str] = []
    checked = 0
    cells = 0
    for phase, outcomes in outcomes_by_phase.items():
        if not outcomes:
            failures.append(f"{label} {phase}: no downstream outcome")
            continue
        for code in plan.config.sets:
            outcome = outcomes.get(code)
            if outcome is None:
                failures.append(f"{label} {phase} set {code}: missing")
                continue
            if outcome["status"] != "success":
                failures.append(f"{label} {phase} set {code}: report {outcome['status']} "
                                f"{outcome['warnings'][:3]}")
            want_before = reference.expected_statuses_before(plan, code, phase)
            if outcome["statuses_before"] != want_before:
                diff = {k: (want_before.get(k), outcome["statuses_before"].get(k))
                        for k in set(want_before) | set(outcome["statuses_before"])
                        if want_before.get(k) != outcome["statuses_before"].get(k)}
                failures.append(f"{label} {phase} set {code}: statuses before decisions {diff}")
            want_after = reference.expected_statuses_after(plan, code, phase)
            if outcome["statuses_after"] != want_after:
                diff = {k: (want_after.get(k), outcome["statuses_after"].get(k))
                        for k in set(want_after) | set(outcome["statuses_after"])
                        if want_after.get(k) != outcome["statuses_after"].get(k)}
                failures.append(f"{label} {phase} set {code}: statuses after decisions {diff}")
            checked += len(want_before) + len(want_after)
            if outcome.get("report"):
                compared, mismatches = reference.compare_workbook(
                    plan, code, phase, outcome["report"]
                )
                cells += compared
                checked += compared
                for item in mismatches[:10]:
                    failures.append(f"{label} {phase} set {code}: {item.describe()}")
    return checked, failures, {"cells_compared": cells}


def stored_results_vs_truth(
    plan: CampaignPlan, project: Any, session_id: str, roster_ids: dict[str, int],
    template_path: Any,
) -> tuple[int, list[str]]:
    """Every stored candidate result of the final reports, against the reference."""
    from omr_scanner.evaluation.intake_qualification.inspect import open_read_only
    from omr_scanner.services import session_scope
    from omr_scanner.services.template_service import load_template

    template = load_template(template_path)
    failures: list[str] = []
    checked = 0
    database = open_read_only(project)
    try:
        for code in plan.config.sets:
            stored = {item.candidate_id: item for item in session_scope.results(
                database, roster_ids[code], session_id, template)}
            for expected in reference.expected_results(plan, code, "reopen"):
                checked += 1
                item = stored.get(expected.roll)
                if item is None:
                    failures.append(f"set {code} {expected.roll}: no stored result")
                    continue
                if expected.absent:
                    if item.status.value != "absent":
                        failures.append(
                            f"set {code} {expected.roll}: {item.status.value}, expected absent"
                        )
                    continue
                mark = expected.mark
                assert mark is not None
                got = (item.status.value, item.final_score, item.correct_count,
                       item.incorrect_count, item.blank_count)
                want = ("scored", mark.final, mark.correct, mark.incorrect, mark.blank)
                if got != want:
                    failures.append(f"set {code} {expected.roll}: stored {got}, expected {want}")
    finally:
        database.close()
    return checked, failures


def identifiers_vs_truth(plan: CampaignPlan, run: Any, index: RunIndex) -> tuple[int, list[str]]:
    from omr_scanner.evaluation.intake_qualification.inspect import open_read_only
    from omr_scanner.services import session_population

    corrected = reference.corrected_contents(plan)
    failures: list[str] = []
    database = open_read_only(run.project)
    try:
        identities = session_population.effective_identifiers(database, run.final_population)
    finally:
        database.close()
    checked = 0
    effective = set(run.final_population.effective)
    for scan, item in identities.items():
        key = index.scan_content.get(scan)
        if key is None or scan not in effective:
            continue  # a byte copy's own row, a superseded or rejected sheet
        content = plan.content(key)
        if content.kind is ContentKind.BLANK_PAGE or not content.registers:
            continue
        want = reference.expected_identifier(content, corrected=key in corrected)
        checked += 1
        if item.value != want:
            failures.append(f"{key}: effective Student ID {item.value!r}, expected {want!r}")
    return checked, failures


def a_ground_truth(
    plan: CampaignPlan, run: Any, index: RunIndex, template_path: Any
) -> CheckResult:
    result = CheckResult("session_results_match_ground_truth")
    end = run.endgame
    checked, failures, extra = results_vs_truth(
        plan, {"first_close": end.get("first_downstream"),
               "reopen": end.get("second_downstream")}, label=run.name)
    result.checked += checked
    for failure in failures:
        result.fail(failure)
    count, failures = identifiers_vs_truth(plan, run, index)
    result.checked += count
    for failure in failures:
        result.fail(failure)
    rosters = {code: outcome["roster_id"]
               for code, outcome in (end.get("second_downstream") or {}).items()}
    if rosters:
        count, failures = stored_results_vs_truth(plan, run.project, run.session_id, rosters,
                                                  template_path)
        result.checked += count
        for failure in failures:
            result.fail(failure)
    expected = reference.effective_contents(plan, phase="reopen")
    actual = set(effective_contents_of(run.final_facts, run.final_population, index.content_of_sha))
    if expected != actual:
        result.fail(f"effective population differs: missing {sorted(expected - actual)[:5]}, "
                    f"extra {sorted(actual - expected)[:5]}")
    result.minimum = 1
    result.evidence = {**extra, "results_compared": result.checked,
                       "summary": reference.summarize_expected(plan)}
    return result.settle()


def _integrity_failures(report: dict[str, Any], label: str) -> list[str]:
    failures = []
    if report.get("error"):
        failures.append(f"{label}: {report['error']}")
    if report.get("quick_check") != ["ok"]:
        failures.append(f"{label}: quick_check {report.get('quick_check')}")
    if report.get("integrity_check") != ["ok"]:
        failures.append(f"{label}: integrity_check {report.get('integrity_check')}")
    foreign = report.get("foreign_key_check")
    if foreign is None:
        failures.append(f"{label}: foreign_key_check did not run")
    elif foreign != []:
        failures.append(f"{label}: foreign_key_check {list(foreign)[:5]}")
    return failures


def _integrity_reports(runs: list[Any], finite: Any) -> list[tuple[str, dict[str, Any]]]:
    reports = []
    for run in runs:
        for kill in run.kills:
            reports.append((f"{run.name} {kill.label}", kill.integrity))
        reports.append((f"{run.name} final", run.final_integrity))
    if finite is not None:
        reports.append(("finite final", finite.final_integrity))
    return reports


def a_sqlite(runs: list[Any], finite: Any) -> CheckResult:
    result = CheckResult("sqlite_integrity")
    for label, report in _integrity_reports(runs, finite):
        result.checked += 1
        for failure in _integrity_failures(report, label):
            result.fail(failure)
    result.minimum = 1
    result.evidence = {"reports": result.checked}
    return result.settle()


def a_health(runs: list[Any], finite: Any) -> CheckResult:
    result = CheckResult("application_invariants")
    warnings: Counter[str] = Counter()
    for label, report in _integrity_reports(runs, finite):
        result.checked += 1
        if not report.get("health_issues") and report.get("health_ok") is None:
            result.fail(f"{label}: health check did not run")
        for issue in report.get("health_issues", []):
            if issue["level"] in ("error", "critical"):
                result.fail(f"{label}: {issue['code']} - {issue['message'][:160]}")
            else:
                warnings[issue["code"]] += 1
    result.minimum = 1
    result.evidence = {"reports": result.checked, "warnings": dict(warnings)}
    return result.settle()


def recognition_view(facts: Any, sha_to_content: dict[str, str]) -> dict[str, tuple[str, ...]]:
    """Per content: what recognition stored for its (first read) sheet."""
    view: dict[str, tuple[str, ...]] = {}
    for sheet in sorted(facts.sheets.values(), key=lambda s: s.scan_id):
        if sheet.status not in ("completed", "warning", "failed"):
            continue
        key = sha_to_content.get(sheet.sha)
        if key is None or key in view:
            continue
        view[key] = (sheet.status, sheet.outcome, sheet.identifier, sheet.set_code)
    return view


def a_finite(plan: CampaignPlan, run: Any, finite: Any, golden: dict[str, Any] | None,
             sha_to_content: dict[str, str]) -> CheckResult:
    result = CheckResult("finite_mode_regression")
    if golden is None:
        result.fail("the golden one-batch regression was not run")
    elif not golden.get("ok"):
        result.fail(
            f"golden one-batch regression: {golden.get('differences') or golden.get('error')}"
        )
    else:
        result.checked += int(golden.get("cells", 0)) + int(golden.get("results", 0))
    if finite is None:
        result.fail("the finite single-batch control was not run")
    else:
        if finite.error:
            result.fail(f"finite control failed: {finite.error}")
        end = finite.endgame
        for key in ("first_close", "second_close"):
            if not (end.get(key) or {}).get("closed"):
                result.fail(f"finite control {key}: {end.get(key)}")
        checked, failures, _extra = results_vs_truth(
            plan, {"first_close": end.get("first_downstream"),
                   "reopen": end.get("second_downstream")}, label="finite")
        result.checked += checked
        for failure in failures:
            result.fail(failure)
        session_view = recognition_view(run.final_facts, sha_to_content)
        finite_view = recognition_view(finite.final_facts, sha_to_content)
        differing = sorted(key for key in set(session_view) | set(finite_view)
                           if session_view.get(key) != finite_view.get(key))
        if differing:
            result.fail(f"finite and session recognition differ for {differing[:10]}")
        result.checked += len(finite_view)
        if len(finite.batches) < 1:
            result.fail("no finite batch")
    result.minimum = 1
    result.evidence = {"golden": golden, "finite_batches": len(finite.batches) if finite else 0,
                       "finite_seconds": round(finite.seconds, 1) if finite else None}
    return result.settle()


# ----------------------------------------------------------------------
# Crash matrix and endurance
# ----------------------------------------------------------------------
def _case(case_id: str, title: str) -> CheckResult:
    return CheckResult(case_id, title=title)


def crash_matrix(plan: CampaignPlan, run: Any, control: Any, index: RunIndex,
                 comparison: dict[str, Any]) -> list[CheckResult]:
    """Evaluate the crash cases (``ACCEPTANCE_CRITERIA.md`` §5.4) from one run's evidence."""
    titles = dict(CRASH_CASES)
    kills = run.kills
    forced = [k for k in kills if k.kind == "forced"]
    cases: dict[str, CheckResult] = {cid: _case(cid, title) for cid, title in CRASH_CASES}
    failed_planned = {key for key in reference.registered_contents(plan)
                      if not plan.content(key).registers}
    final_status = {item.scan_id: item.status for item in run.final_facts.sheets.values()}

    def retried(kill: Any) -> list[str]:
        issues = []
        for scan in kill.restart.get("down", {}).get("processing_ids", []):
            if final_status.get(scan) not in ("completed", "warning", "failed"):
                issues.append(f"in-flight sheet {scan} never completed after restart")
        return issues

    # 1 + 5: the clean close.
    closes = [k for k in kills if k.kind == "clean_close"]
    for kill in closes:
        c1 = cases["case_01_clean_close_during_scan"]
        c1.checked += 1
        c1.scale = (f"{kill.pre['committed']} committed, {kill.pre.get('remaining', 0)} still "
                    f"to read ({kill.pre['processing']} in a worker)")
        # Halfway through Scan: committed work behind it and work still to do
        # (unread sheets or files not yet registered) - "the rest resumable".
        if kill.pre["committed"] < 1 or kill.pre.get("remaining", 0) < 1:
            c1.fail("the close did not land halfway through scanning")
        if kill.processing_after:
            c1.fail(f"{kill.processing_after} sheet(s) left claimed by a clean close")
        down = kill.restart.get("down", {})
        failed = [scan for scan in down.get("failed_ids", [])
                  if index.scan_content.get(int(scan)) not in failed_planned]
        if failed:
            c1.fail(f"sheets marked failed by the close: {failed[:5]}")
        for failure in kill.restart.get("failures", []):
            c1.fail(failure)
        c1.evidence[kill.label] = {k: kill.restart.get(k) for k in ("ok", "returned")}
        c5 = cases["case_05_clean_close_during_resolve"]
        c5.checked += 1
        open_left = len(down.get("open_conflict_ids", []))
        decided = down.get("operator_events", 0)
        c5.scale = f"{decided} operator events committed, {open_left} conflicts open"
        if decided < 1 or open_left < 1:
            c5.fail("the close did not land halfway through Resolve")
        for failure in kill.restart.get("failures", []):
            if "audit" in failure or "resolved" in failure:
                c5.fail(failure)
    # 2, 3, 11, 14: forced kills.
    def percent_of(kill: Any) -> int:
        return int(str(kill.label).split("@")[1].split("%")[0])  # "kill@50%" or "kill@50%#2"

    # A point counts once a kill there landed on work in flight; a kill whose
    # work committed first is still a real kill and restart, and is checked
    # like the others, but proves nothing about interrupted sheets.
    percents = sorted({percent_of(k) for k in forced if k.processing_after >= 1})
    for point in sorted({percent_of(k) for k in forced} - set(percents)):
        c11_missing = f"kill@{point}%: no kill there landed with a sheet in flight"
        cases["case_11_resume_percentages"].fail(c11_missing)
    c2, c3, c11, c14 = (cases["case_02_forced_kill_during_scan"],
                        cases["case_03_sheet_interrupted_in_processing"],
                        cases["case_11_resume_percentages"],
                        cases["case_14_sealed_batch_interrupted"])
    for kill in forced:
        restart = kill.restart
        for case in (c2, c11):
            case.checked += 1
            for failure in restart.get("failures", []):
                case.fail(f"{kill.label}: {failure}")
            for failure in retried(kill):
                case.fail(f"{kill.label}: {failure}")
            if kill.committed_after < 1:
                case.fail(f"{kill.label}: nothing committed (vacuous)")
        c11.evidence[kill.label] = {"committed": kill.committed_after,
                                    "in_flight": kill.processing_after,
                                    "returned": restart.get("returned"),
                                    "ok": restart.get("ok")}
        if kill.processing_after:
            c3.checked += kill.processing_after
            if restart.get("returned", 0) != kill.processing_after:
                c3.fail(f"{kill.label}: {restart.get('returned')} of {kill.processing_after} "
                        "in-flight sheets returned to a retryable state")
            for failure in retried(kill):
                c3.fail(f"{kill.label}: {failure}")
        unfinished = restart.get("unfinished_sealed_batches", [])
        c14.checked += len(unfinished)
        for failure in restart.get("failures", []):
            if "batch" in failure:
                c14.fail(f"{kill.label}: {failure}")
        for batch_id in unfinished:
            if not run.final_facts.batches[batch_id].sealed:
                c14.fail(f"{kill.label}: batch {batch_id[:8]} no longer sealed")
            members = run.final_facts.batches[batch_id].members
            if any(final_status.get(scan) not in ("completed", "warning", "failed", "duplicate")
                   for scan in members):
                c14.fail(f"{kill.label}: batch {batch_id[:8]} never finished")
    c2.scale = "; ".join(
        f"{k.label}: {k.committed_after} committed / {k.processing_after} in flight"
        for k in forced
    ) or "no forced kill"
    c11.scale = f"kills at {percents} % of {index and len(set(index.scan_content.values()))} sheets"
    missing = sorted(set(REQUIRED_KILL_PERCENTS) - set(percents))
    if run.plan_mode == Mode.RELEASE.value and missing:
        c11.fail(f"resume points missing: {missing}")
    c11.minimum = len(REQUIRED_KILL_PERCENTS) if run.plan_mode == Mode.RELEASE.value else 1
    c2.minimum = 1
    c14.minimum = 1
    # 4: kill after a commit.
    c4 = cases["case_04_kill_after_commit"]
    for kill in (k for k in kills if k.kind == "after_commit"):
        c4.checked += 1
        paused = set(kill.trigger.get("paused", {}).get("scans") or [])
        committed = set(kill.restart.get("committed_ids", []))
        if not paused or not paused <= committed:
            c4.fail(f"the paused group {sorted(paused)[:5]} was not durable at the kill")
        later = set().union(*(scans for inc, scans in index.claims_by_incarnation.items()
                              if inc > kill.incarnation)) if index.claims_by_incarnation else set()
        if paused & later:
            c4.fail(f"committed sheets read again: {sorted(paused & later)[:5]}")
        for failure in kill.restart.get("failures", []):
            c4.fail(failure)
        c4.scale = (f"{len(paused)} sheet(s) in the committed group, "
                    f"{kill.committed_after} committed")
    # 6, 7, 8, 15: Resolve across restarts.
    c6 = cases["case_06_forced_kill_after_resolve_corrections"]
    for kill in (k for k in kills if k.kind == "operator"):
        c6.checked += 1
        decided = int(kill.trigger.get("paused", {}).get("decisions") or 0)
        events = kill.restart.get("down", {}).get("operator_events", 0)
        c6.scale = f"{decided} decisions in this incarnation, {events} operator audit events"
        if decided < 1:
            c6.fail("no decision committed before the kill")
        for failure in kill.restart.get("failures", []):
            c6.fail(failure)
    c7, c8, c15 = (cases["case_07_restart_retains_corrections"],
                   cases["case_08_unresolved_stay_unresolved"],
                   cases["case_15_resolve_reachable_after_reopen"])
    for kill in kills:
        down = kill.restart.get("down", {})
        c7.checked += len(down.get("resolved_conflict_ids", []))
        for failure in kill.restart.get("failures", []):
            if "resolved" in failure or "audit" in failure:
                c7.fail(f"{kill.label}: {failure}")
        view = kill.restart.get("resolve_view", {})
        expected_open = down.get("open_conflict_ids", [])
        c8.checked += 1
        if sorted(view.get("open_ids", [])) != sorted(expected_open):
            c8.fail(f"{kill.label}: Resolve's open queue {len(view.get('open_ids', []))} != "
                    f"open at the kill {len(expected_open)}")
        c15.checked += 1
        if not view or view.get("batches", 0) < 1:
            c15.fail(f"{kill.label}: Resolve had no session to show before Scan was visited")
        elif view.get("resolved", -1) != len(down.get("resolved_live_conflict_ids", [])):
            c15.fail(f"{kill.label}: Resolve showed {view.get('resolved')} decided conflicts, "
                     f"{len(down.get('resolved_live_conflict_ids', []))} were committed")
    # 9: repeated restarts.
    c9 = cases["case_09_repeated_restarts"]
    c9.checked = len(kills)
    c9.minimum = 3
    facts = run.final_facts
    if len({sid for sid, _s, _n in facts.sessions}) != 1:
        c9.fail(f"{len(facts.sessions)} sessions in the project")
    reprocessed = {run.reprocess.get("superseded_batch")} if run.reprocess else set()
    if facts.superseded_batches - reprocessed:
        c9.fail(f"unplanned superseded batch(es): {sorted(facts.superseded_batches - reprocessed)}")
    per_ledger = Counter(item.intake_file_id for item in facts.sheets.values()
                         if item.intake_file_id is not None
                         and item.batch_id not in facts.superseded_batches)
    doubled = [key for key, count in per_ledger.items() if count > 1]
    if doubled:
        c9.fail(f"ledger rows with more than one live sheet: {doubled[:5]}")
    identities = Counter((c.batch_id, c.scan_id, c.kind, c.zone, c.group) for c in facts.conflicts)
    if any(count > 1 for count in identities.values()):
        c9.fail("duplicate conflict identities")
    logged: Counter[tuple[int, Any]] = Counter()
    for item in run.coordinator_events:
        if item["event"] == "operator_decision" and item.get("conflict_id"):
            logged[(int(item["conflict_id"]), item["action"])] += 1
    decision_events: Counter[int] = Counter()
    for event in facts.audit:
        if (event.reviewer == OPERATOR and event.conflict_id
                and event.action in ("corrected", "accepted")):
            decision_events[event.conflict_id] += 1
    repeated = [cid for cid, count in decision_events.items() if count > 1]
    if repeated:
        c9.fail(f"operator decisions recorded twice: {repeated[:5]}")
    for kill in kills:
        for failure in kill.restart.get("failures", []):
            c9.fail(f"{kill.label}: {failure}")
    # 10: interrupted = uninterrupted.
    c10 = cases["case_10_results_after_interruption"]
    c10.checked = comparison.get("compared", 0)
    for failure in comparison.get("failures", []):
        c10.fail(failure)
    if control is None:
        c10.fail("no uninterrupted control run")
    # 12: integrity after every case.
    c12 = cases["case_12_integrity_after_every_case"]
    for kill in kills:
        c12.checked += 1
        for failure in _integrity_failures(kill.integrity, kill.label):
            c12.fail(failure)
        for issue in kill.integrity.get("health_issues", []):
            if issue["level"] in ("error", "critical"):
                c12.fail(f"{kill.label}: health {issue['code']}")
        for failure in kill.restart.get("failures", []):
            if "audit" in failure:
                c12.fail(f"{kill.label}: {failure}")
    # 13: kill before the duplicate pass.
    c13 = cases["case_13_kill_before_conflict_generation"]
    for kill in (k for k in kills if k.kind == "syncing_duplicates"):
        c13.checked += 1
        paused = kill.trigger.get("paused", {})
        scans = set(paused.get("scans") or [])
        added = kill.restart.get("added_conflicts", [])
        added_duplicate = [row for row in added if row[2] == "identifier_duplicate"]
        if not scans <= set(kill.restart.get("committed_ids", [])):
            c13.fail("the paused sheets were not committed at the kill")
        if not kill.restart.get("running_batches_at_kill"):
            c13.fail("no unit was left running (the sheet would have looked complete)")
        if not added_duplicate:
            c13.fail("recovery generated no duplicate-ID conflict (nothing was owed)")
        later = set().union(*(s for inc, s in index.claims_by_incarnation.items()
                              if inc > kill.incarnation)) if index.claims_by_incarnation else set()
        if scans & later:
            c13.fail(f"sheets read again: {sorted(scans & later)}")
        for failure in kill.restart.get("failures", []):
            c13.fail(failure)
        c13.scale = (f"{len(scans)} sheet(s) committed, {len(added_duplicate)} duplicate "
                     "conflict(s) created by recovery")
        c13.evidence["added_conflicts"] = added_duplicate
    for case in cases.values():
        case.settle()
        case.title = titles[case.name]
    return [cases[cid] for cid, _title in CRASH_CASES]


def compare_runs(plan: CampaignPlan, first: Any, second: Any, sha_to_content: dict[str, str],
                 *, label: str) -> dict[str, Any]:
    """Final authoritative outcomes of two runs of the same cohort: equal?"""
    failures: list[str] = []
    compared = 0
    if second is None:
        return {"compared": 0, "failures": [f"{label}: missing run"]}
    a = set(effective_contents_of(first.final_facts, first.final_population, sha_to_content))
    b = set(effective_contents_of(second.final_facts, second.final_population, sha_to_content))
    compared += len(a | b)
    if a != b:
        failures.append(f"{label}: effective populations differ ({len(a ^ b)} contents)")
    for phase in ("first_downstream", "second_downstream"):
        outcomes_a = first.endgame.get(phase) or {}
        outcomes_b = second.endgame.get(phase) or {}
        for code in plan.config.sets:
            x, y = outcomes_a.get(code), outcomes_b.get(code)
            if x is None or y is None:
                failures.append(f"{label} {phase} {code}: missing")
                continue
            for key in ("statuses_before", "statuses_after", "scored", "absent", "status"):
                compared += 1
                if x.get(key) != y.get(key):
                    failures.append(f"{label} {phase} {code}: {key} differs")
            if x.get("report") and y.get("report"):
                cells_x = _workbook_values(x["report"])
                cells_y = _workbook_values(y["report"])
                compared += len(cells_x)
                if cells_x != cells_y:
                    failures.append(f"{label} {phase} {code}: report cell values differ")
    return {"compared": compared, "failures": failures}


def _workbook_values(path: str) -> dict[str, list[list[str]]]:
    import openpyxl

    workbook = openpyxl.load_workbook(path)
    try:
        return {
            sheet.title: [[("" if value is None else str(value)) for value in row]
                          for row in sheet.iter_rows(values_only=True)]
            for sheet in workbook.worksheets if sheet.title not in ("Summary", "Processing Log")
        }
    finally:
        workbook.close()


def endurance(plan: CampaignPlan, run: Any, control: Any,
              comparison: dict[str, Any]) -> list[CheckResult]:
    """Evaluate the endurance cases (``ACCEPTANCE_CRITERIA.md`` §5.3) from one run's evidence."""
    titles = dict(ENDURANCE_CASES)
    out = {cid: CheckResult(cid, title=title) for cid, title in ENDURANCE_CASES}
    facts = run.final_facts
    batches = facts.session_batches()
    sealed = [b for b in batches if b.sealed]
    sources = Counter(b.source_id for b in batches if b.source_id)
    a = out["endurance_a_many_finite_batches"]
    a.checked = len(sealed)
    a.minimum = 10
    a.scale = f"{len(sealed)} sealed batches from {len(sources)} sources in one session"
    if len(sources) < min(3, plan.config.sources):
        a.fail(f"units from only {len(sources)} sources")
    if any(b.session_id != run.session_id for b in facts.batches.values()):
        a.fail("a batch outside the session")
    # B: continuous random intake.
    b = out["endurance_b_continuous_random_intake"]
    writes = sorted(float(item["write_completed_at"]) for item in run.writer_events
                    if item["event"] == "write_completed")
    gaps = [later - earlier for earlier, later in pairwise(writes)]
    duration = (writes[-1] - writes[0]) if len(writes) > 1 else 0.0
    during = [item for item in run.coordinator_events
              if item["event"] == "operator_decision" and writes and float(item["t"]) < writes[-1]]
    b.checked = len(writes)
    b.minimum = max(1, len(plan.main_arrivals) - 1)
    b.scale = (f"{len(writes)} files over {duration:.0f} s "
               f"({len(writes) / max(duration, 1e-9):.2f}/s); {len(during)} operator decisions "
               "while files were still arriving")
    if len(during) < 1:
        b.fail("no operator decision while intake continued")
    if gaps and max(gaps) < 2 * (sum(gaps) / len(gaps)):
        b.fail("arrivals look periodic (no idle gaps)")
    b.evidence = {"duration_seconds": round(duration, 1), "files": len(writes),
                  "max_gap_seconds": round(max(gaps), 2) if gaps else None,
                  "median_gap_seconds": round(sorted(gaps)[len(gaps) // 2], 3) if gaps else None,
                  "operator_decisions_during_intake": len(during),
                  "max_in_flight": run.resource.get("max_in_flight_seen"),
                  "max_writer_backlog": run.resource.get("max_writer_backlog")}
    # C: supersession / reprocessing.
    c = out["endurance_c_supersession_reprocess"]
    if run.reprocess:
        c.checked = 1
        old = run.reprocess.get("superseded_batch")
        new = run.reprocess.get("reprocess_batch")
        if old not in facts.superseded_batches:
            c.fail("the reprocessed batch is not superseded")
        effective = set(run.final_population.effective)
        if any(scan in effective for scan in facts.batches[old].members):
            c.fail("a superseded batch's sheet still counts")
        new_members = facts.batches[new].members if new in facts.batches else ()
        if not new_members or not all(scan in effective for scan in new_members):
            c.fail("the reprocess batch's sheets do not all count")
        c.scale = f"Reprocess All of a {len(new_members)}-sheet unit while intake continued"
    else:
        c.evidence["note"] = "no plain unit was available; covered by the targeted stress test"
    c.minimum = 1
    # D: repeated kills.
    d = out["endurance_d_repeated_kill_restart"]
    forced = [k for k in run.kills if k.kind != "clean_close"]
    d.checked = len(forced)
    d.minimum = 3
    d.scale = f"{len(forced)} real process kills, {len(run.kills)} restarts"
    for kill in run.kills:
        if kill.orphans:
            d.fail(f"{kill.label}: worker processes outlived the coordinator: {kill.orphans}")
        if not kill.restart.get("ok"):
            d.fail(f"{kill.label}: {kill.restart.get('failures', [])[:3]}")
    if run.endgame.get("leftover_workers"):
        d.fail(f"workers left after the final stop: {run.endgame['leftover_workers']}")
    # E: uninterrupted control.
    e = out["endurance_e_uninterrupted_control"]
    e.checked = comparison.get("compared", 0)
    for failure in comparison.get("failures", []):
        e.fail(failure)
    if control is None:
        e.fail("no uninterrupted control run")
    for case in out.values():
        case.settle()
        case.title = titles[case.name]
    if not run.reprocess:
        c.status = NOT_EXERCISED
    return [out[cid] for cid, _ in ENDURANCE_CASES]


# ----------------------------------------------------------------------
# Scale, workload and the verdict
# ----------------------------------------------------------------------
def workload(plan: CampaignPlan, run: Any) -> dict[str, Any]:
    """Counters proving each workload feature actually occurred."""
    decisions = Counter(item["action"] for item in run.coordinator_events
                        if item["event"] == "operator_decision")
    patterns = Counter(item.pattern.value for item in plan.arrivals)
    groups = reference.duplicate_groups(plan)
    names = Counter(item.name for item in plan.main_arrivals)
    copies = sum(len(places) - 1 for places in groups.values())
    cross_replacements = 0
    sources = {item.content: item.source for item in plan.main_arrivals}
    for task in plan.tasks:
        if (task.kind is TaskKind.CONFIRM_REPLACEMENT
                and sources.get(task.content) != sources.get(task.value)):
            cross_replacements += 1
    duplicate_id_conflicts = sum(
        1 for c in run.final_facts.conflicts if c.kind == "identifier_duplicate"
    )
    return {
        "sources": len({item.source for item in plan.main_arrivals}),
        "arrivals_written": sum(
            1 for item in run.writer_events if item["event"] == "write_completed"
        ),
        "arrivals_planned": len(plan.arrivals),
        "unique_contents": len({item.content for item in plan.main_arrivals}),
        "byte_copy_arrivals": copies,
        "sets": len(plan.config.sets),
        "candidates_on_lists": sum(1 for item in plan.candidates if item.on_roster),
        "partial_writes": sum(count for p, count in patterns.items() if p != "atomic"),
        "held_open_writes": patterns.get("held_open", 0),
        "renamed_writes": patterns.get("rename", 0),
        "long_pause_writes": patterns.get("long_pause", 0),
        "header_first_writes": patterns.get("header_first", 0),
        "cross_source_same_name": sum(1 for count in names.values() if count > 1),
        "source_outages": len(run.outages),
        "kills": sum(1 for k in run.kills if k.kind != "clean_close"),
        "clean_closes": sum(1 for k in run.kills if k.kind == "clean_close"),
        "restarts": len(run.kills),
        "offline_arrivals": None,
        "duplicate_id_conflicts": duplicate_id_conflicts,
        "operator_corrections": decisions.get("correct_id", 0),
        "duplicate_acceptances": decisions.get("accept_duplicate", 0),
        "quality_suggestions": sum(1 for _s, (decision, _d) in run.final_facts.quality.items()
                                   if decision == "rescan_required"),
        "confirmed_rejections": (
            decisions.get("confirm_rescan", 0) + decisions.get("reject_replacement", 0)
        ),
        "dismissed_suggestions": decisions.get("dismiss_suggestion", 0),
        "confirmed_replacements": decisions.get("confirm_replacement", 0),
        "cross_source_replacements": cross_replacements,
        "replacement_chains": sum(1 for item in plan.candidates if item.category is Category.CHAIN),
        "superseded_batches": len(run.final_facts.superseded_batches),
        "sealed_batches": sum(1 for b in run.final_facts.session_batches() if b.sealed),
        "operator_decisions": sum(decisions.values()),
        "real_images": True,
    }


REQUIRED_FEATURES = (
    "partial_writes", "held_open_writes", "cross_source_same_name", "byte_copy_arrivals",
    "source_outages", "kills", "restarts", "duplicate_id_conflicts", "operator_corrections",
    "quality_suggestions", "confirmed_rejections", "dismissed_suggestions",
    "cross_source_replacements", "replacement_chains", "sealed_batches",
)


def scale_check(
    mode: str, counters: dict[str, Any], assertions: list[CheckResult]
) -> dict[str, Any]:
    """Whether the *measured* campaign meets the release requirements."""
    reasons = []
    if counters["sources"] < RELEASE_MIN_SOURCES:
        reasons.append(f"{counters['sources']} sources < {RELEASE_MIN_SOURCES}")
    if counters["arrivals_written"] < RELEASE_MIN_ARRIVALS:
        reasons.append(f"{counters['arrivals_written']:,} arrivals < {RELEASE_MIN_ARRIVALS:,}")
    if counters["sets"] < RELEASE_MIN_SETS:
        reasons.append(f"{counters['sets']} sets < {RELEASE_MIN_SETS}")
    if not counters.get("real_images"):
        reasons.append("not real synthetic images")
    missing = [name for name in REQUIRED_FEATURES if not counters.get(name)]
    if missing:
        reasons.append(f"features not exercised: {missing}")
    if {item.name for item in assertions} != set(REQUIRED_ASSERTIONS):
        reasons.append("not every required assertion was evaluated")
    if mode != Mode.RELEASE.value:
        reasons.append(f"mode {mode!r} is not the release mode")
    return {"release_scale": not reasons, "reasons": reasons}


def verdict(assertions: list[CheckResult], crash: list[CheckResult],
            endurance_cases: list[CheckResult], scale: dict[str, Any], *, completed: bool) -> str:
    """The campaign verdict, from the machine-readable results only."""
    names = [item.name for item in assertions]
    if names != list(REQUIRED_ASSERTIONS) or not completed:
        return VERDICT_FAILED
    required_endurance = [c for c in endurance_cases
                          if c.name != "endurance_c_supersession_reprocess"
                          or c.status != NOT_EXERCISED]
    if not all(item.passed for item in [*assertions, *crash, *required_endurance]):
        return VERDICT_FAILED
    return VERDICT_QUALIFIED if scale["release_scale"] else VERDICT_SMALL


def evaluate_assertions(
    plan: CampaignPlan,
    run: Any,
    control: Any,
    finite: Any,
    golden: dict[str, Any] | None,
    *,
    sha_to_content: dict[str, str],
    template_path: Any,
    share_root: Any,
    final_checkpoint: dict[str, Any],
) -> list[CheckResult]:
    """Every required assertion, in registry order. Never fewer.

    Each is evaluated on its own: one that raises (malformed or missing
    evidence) fails with the exception named, and the others still run.
    """
    built: list[RunIndex] = []

    def index() -> RunIndex:
        if not built:
            built.append(RunIndex.build(run, sha_to_content))
        return built[0]

    runs = [item for item in (run, control) if item is not None]
    builders = {
        "stable_files_discovered_exactly_once": lambda: a_stable_files(plan, run, index()),
        "no_incomplete_file_processed": lambda: a_no_incomplete(plan, run, index()),
        "source_provenance_retained": lambda: a_provenance(plan, run, index(), share_root),
        "duplicate_content_identified": lambda: a_duplicates(plan, run, index()),
        "independent_filenames_do_not_collide": lambda: a_names(plan, run, index()),
        "batches_finite": lambda: a_batches(plan, run),
        "no_accepted_image_lost": lambda: a_no_lost(plan, run, index()),
        "no_completed_scan_rerecognised": lambda: a_not_reread(plan, run, index()),
        "offline_arrivals_discovered": lambda: a_offline(plan, run, index()),
        "conflict_counts_correct_as_population_grows": lambda: a_conflict_growth(plan, run),
        "rescan_relationships_survive_restart": lambda: a_rescans(plan, run, index()),
        "aggregate_counts_consistent": lambda: a_aggregate(plan, run, final_checkpoint),
        "session_results_match_ground_truth":
            lambda: a_ground_truth(plan, run, index(), template_path),
        "sqlite_integrity": lambda: a_sqlite(runs, finite),
        "application_invariants": lambda: a_health(runs, finite),
        "finite_mode_regression": lambda: a_finite(plan, run, finite, golden, sha_to_content),
    }
    results = []
    for name in REQUIRED_ASSERTIONS:
        try:
            results.append(builders[name]())
        except Exception as exc:  # an evaluator crash is a failed assertion, never a skip
            item = CheckResult(name)
            item.fail(f"evaluation raised {type(exc).__name__}: {exc}")
            results.append(item.settle())
    return results


__all__ = [
    "CRASH_CASES",
    "ENDURANCE_CASES",
    "REPORT_SCHEMA_VERSION",
    "REQUIRED_ASSERTIONS",
    "REQUIRED_FEATURES",
    "CheckResult",
    "RunIndex",
    "compare_recovery",
    "compare_runs",
    "crash_matrix",
    "endurance",
    "evaluate_assertions",
    "evaluate_checkpoint",
    "scale_check",
    "summarize_down_state",
    "verdict",
    "workload",
]
