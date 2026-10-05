"""The scripted operator: planned decisions through production services (revised phase 9).

Purpose:
    Act like the examination office's operator while scanning continues -
    correct Student IDs, answer duplicate-ID conflicts, confirm or dismiss
    suggested rescans, confirm replacements (including across scanners and
    along a chain), reject a bad rescan - and, once the session is finished,
    make the attendance decisions and produce the results. Every action is the
    application's own service call (``review_store``, ``quality_decisions``,
    ``scan_lifecycle``, ``reconciliation_store``, ``session_scope``); nothing is
    written to a table directly and no business rule is bypassed.

What the operator knows:
    What the *paper* says - the plan's ground truth (the written Student ID, the
    candidate a folded sheet belongs to, which rescan is whose) - and what the
    application *shows* - open conflicts, outstanding suggestions, possible
    rescans. It never consults recognition results it was not shown, and it
    decides nothing the plan did not schedule.

Restart safety:
    It acts only on items that are open *now* (an open conflict, an
    outstanding suggestion, an outstanding rescan case), so after a kill it
    simply finds whatever its last incarnation had not committed and does it -
    never twice, because a committed decision closes the item it answered.

Timing (:class:`~.cohort.When`):
    ``live`` items are decided a seeded delay after they first become visible,
    ``late`` items once the run is past :attr:`OperatorActor.late_progress`,
    ``final`` items only in the endgame (:meth:`OperatorActor.resolve_all`).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omr_scanner.evaluation.intake_qualification.cohort import (
    CampaignPlan,
    OperatorTask,
    TaskKind,
    When,
)
from omr_scanner.evaluation.intake_qualification.config import OPERATOR

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.domain.template import OmrTemplate

IDENTIFIER_TYPES = frozenset(
    {"identifier_blank", "identifier_multiple", "identifier_duplicate", "identifier_uncertain",
     "identifier_incomplete", "identifier_unreadable", "identifier_low_confidence"}
)


@dataclass
class OperatorStats:
    """What the operator has done in this incarnation."""

    decisions: int = 0
    by_kind: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    refused: list[str] = field(default_factory=list)
    unexpected: set[str] = field(default_factory=set)


class OperatorActor:
    """Planned decisions, made through the application's services.

    Args:
        database: The open, writable project (the application's own).
        scan_session_id: The session reviewed.
        plan: The campaign plan (the paper's truth and the schedule).
        sha_to_content: Content SHA-256 -> plan content key (how the operator
            recognises *which paper* a sheet is).
        log: Called with ``(event, fields)`` after each committed decision.
        late_progress: Fraction of the run after which ``late`` items are due.
        lock: Held around each decision (the coordinator's hold uses it).
    """

    def __init__(
        self,
        database: ProjectDatabase,
        scan_session_id: str,
        plan: CampaignPlan,
        sha_to_content: dict[str, str],
        *,
        log: Callable[..., Any] | None = None,
        late_progress: float = 0.6,
        lock: threading.Lock | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.database = database
        self.session_id = scan_session_id
        self.plan = plan
        self.sha_to_content = sha_to_content
        self.log = log or (lambda *_a, **_k: None)
        self.late_progress = late_progress
        self.lock = lock or threading.Lock()
        self.clock = clock
        self.stats = OperatorStats()
        self.tasks: dict[str, list[OperatorTask]] = defaultdict(list)
        for task in plan.tasks:
            if task.content:
                self.tasks[task.content].append(task)
        self._first_seen: dict[str, float] = {}
        self._scan_content: dict[int, str] = {}
        self._content_scans: dict[str, list[int]] = defaultdict(list)
        self._max_scan = 0
        self.pause_after: int = 0
        """When positive: block (``on_pause``) after this many decisions."""
        self.on_pause: Callable[[int], None] | None = None
        self._lists: dict[str, dict[str, bool]] = {}
        self._list_versions: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Which paper is which sheet
    # ------------------------------------------------------------------
    def refresh_scans(self) -> None:
        """Learn the content of every sheet registered since the last call."""
        from sqlalchemy import select

        from omr_scanner.database.models import BatchScan, ScanBatch

        with self.database.session() as session:
            rows = session.execute(
                select(BatchScan.scan_id, BatchScan.content_sha256)
                .join(ScanBatch, ScanBatch.batch_id == BatchScan.batch_id)
                .where(ScanBatch.scan_session_id == self.session_id)
                .where(BatchScan.scan_id > self._max_scan)
            ).all()
        for scan_id, sha in rows:
            key = self.sha_to_content.get(str(sha or ""))
            if key is not None:
                self._scan_content[int(scan_id)] = key
                self._content_scans[key].append(int(scan_id))
            self._max_scan = max(self._max_scan, int(scan_id))

    def content_of(self, scan_id: int) -> str | None:
        """The content key of ``scan_id``, if it is known."""
        return self._scan_content.get(scan_id)

    def scans_of(self, key: str) -> list[int]:
        """The scan ids known to carry content ``key``."""
        return list(self._content_scans.get(key, ()))

    def _any_batch(self) -> str | None:
        from omr_scanner.services import session_population

        batches = session_population.session_batch_ids(self.database, self.session_id)
        return batches[0] if batches else None

    # ------------------------------------------------------------------
    # Timing
    # ------------------------------------------------------------------
    def _due(self, task: OperatorTask, item: str, *, progress: float, final: bool) -> bool:
        if final:
            return True
        if task.when is When.FINAL:
            return False
        if task.when is When.LATE:
            return progress >= self.late_progress
        first = self._first_seen.setdefault(item, self.clock())
        return self.clock() - first >= task.delay

    def _task(self, key: str, *kinds: TaskKind) -> OperatorTask | None:
        for task in self.tasks.get(key, ()):
            if task.kind in kinds:
                return task
        return None

    def _decided(self, kind: str, **fields: Any) -> None:
        self.stats.decisions += 1
        self.stats.by_kind[kind] += 1
        self.log("operator_decision", action=kind, **fields)
        if self.pause_after and self.stats.decisions >= self.pause_after and self.on_pause:
            self.on_pause(self.stats.decisions)

    # ------------------------------------------------------------------
    # One pass over everything the application shows
    # ------------------------------------------------------------------
    def step(self, *, progress: float, final: bool = False, budget: int = 40) -> int:
        """Decide whatever is due now; at most ``budget`` decisions. Returns how many."""
        self.refresh_scans()
        made = 0
        made += self._conflicts(progress=progress, final=final, budget=budget - made)
        if made < budget:
            made += self._suggestions(progress=progress, final=final, budget=budget - made)
        if made < budget:
            made += self._rescans(progress=progress, final=final, budget=budget - made)
        return made

    def resolve_all(self, *, rounds: int = 50) -> int:
        """The endgame: every remaining planned review decision, until none is left."""
        total = 0
        for _ in range(rounds):
            made = self.step(progress=1.0, final=True, budget=10_000)
            total += made
            if not made:
                break
        return total

    # --- conflicts -----------------------------------------------------
    def _conflicts(self, *, progress: float, final: bool, budget: int) -> int:
        from omr_scanner.domain.review import ConflictState, ReasonCode
        from omr_scanner.services import review_store

        batch = self._any_batch()
        if batch is None or budget <= 0:
            return 0
        open_conflicts = review_store.list_conflicts(
            self.database, batch, session_wide=True,
            filters=review_store.ConflictFilter(states=(ConflictState.OPEN,)),
        )
        made = 0
        for record in open_conflicts:
            if made >= budget:
                break
            key = self.content_of(record.scan_id)
            kind = record.conflict_type.value
            if key is None:
                self.stats.unexpected.add(f"conflict {record.conflict_id} on unknown scan")
                continue
            if kind in IDENTIFIER_TYPES:
                task = (self._task(key, TaskKind.CORRECT_ID)
                        if kind != "identifier_duplicate"
                        else self._task(key, TaskKind.CORRECT_ID, TaskKind.ACCEPT_DUPLICATE))
                if task is None:
                    continue  # e.g. the victim of a wrong ID: its conflict withdraws itself
                if not self._due(task, f"conflict:{record.conflict_id}", progress=progress,
                                 final=final):
                    continue
                with self.lock:
                    if task.kind is TaskKind.ACCEPT_DUPLICATE:
                        review_store.accept_machine_value(
                            self.database, record.conflict_id, reviewer=OPERATOR
                        )
                        action = "accept_duplicate"
                        value = record.observation.value
                    else:
                        value = self._correction_value(task.value, record)
                        review_store.correct_value(
                            self.database, record.conflict_id, value=value, reviewer=OPERATOR,
                            reason=ReasonCode.WRONG_ID_ENTERED,
                        )
                        action = "correct_id"
                    self._decided(action, scan_id=record.scan_id, content=key,
                                  conflict_id=record.conflict_id, conflict_type=kind, value=value)
                made += 1
            elif kind == "registration_failed":
                task = self._task(key, TaskKind.ACKNOWLEDGE_UNREADABLE)
                if task is None:
                    continue  # a folded script: answered by its rejection
                if self._outstanding_suggestion(record.scan_id):
                    continue  # dismiss the suggestion first
                if not self._due(task, f"conflict:{record.conflict_id}", progress=progress,
                                 final=final):
                    continue
                with self.lock:
                    review_store.accept_machine_value(
                        self.database, record.conflict_id, reviewer=OPERATOR,
                        reason=ReasonCode.MACHINE_CONFIRMED,
                    )
                    self._decided("acknowledge_unreadable", scan_id=record.scan_id,
                                  content=key, conflict_id=record.conflict_id)
                made += 1
            else:
                self.stats.unexpected.add(f"{kind} on {key}")
        return made

    @staticmethod
    def _correction_value(roll: str, record: Any) -> str:
        """The whole written ID, or - for one position's conflict - that position's digit."""
        group = int(record.field.group_key)
        if group >= 0 and record.conflict_type.value != "identifier_duplicate":
            return roll[group]
        return roll

    def _outstanding_suggestion(self, scan_id: int) -> bool:
        from sqlalchemy import func, select

        from omr_scanner.database.models import BatchScan
        from omr_scanner.services import quality_decisions

        with self.database.session() as session:
            return bool(session.scalar(
                select(func.count()).select_from(BatchScan)
                .where(BatchScan.scan_id == scan_id)
                .where(quality_decisions.outstanding_clause(BatchScan.scan_id))
            ))

    # --- suggestions ---------------------------------------------------
    def _suggestions(self, *, progress: float, final: bool, budget: int) -> int:
        from omr_scanner.services import quality_decisions

        made = 0
        for item in quality_decisions.outstanding_suggestions(
            self.database, self.session_id, limit=10_000
        ):
            if made >= budget:
                break
            key = self.content_of(item.scan_id)
            if key is None:
                continue
            task = self._task(key, TaskKind.CONFIRM_RESCAN, TaskKind.DISMISS_SUGGESTION)
            if task is None:
                self.stats.unexpected.add(f"suggestion on {key}")
                continue
            if not self._due(task, f"suggestion:{item.scan_id}", progress=progress, final=final):
                continue
            with self.lock:
                if task.kind is TaskKind.CONFIRM_RESCAN:
                    quality_decisions.confirm_suggestion(
                        self.database, item.scan_id, reviewer=OPERATOR,
                        declared_candidate_id=task.value, declared_set_code=task.set_code,
                    )
                    action = "confirm_rescan"
                else:
                    quality_decisions.dismiss_suggestion(
                        self.database, item.scan_id, reviewer=OPERATOR,
                        note="A blank page fed by mistake; nothing to rescan.",
                    )
                    action = "dismiss_suggestion"
                self._decided(action, scan_id=item.scan_id, content=key)
            made += 1
        return made

    # --- rescans -------------------------------------------------------
    def _rescans(self, *, progress: float, final: bool, budget: int) -> int:
        from omr_scanner.domain.scan_lifecycle import LifecycleState, RejectionReason
        from omr_scanner.services import scan_lifecycle

        made = 0
        # A confirmed rescan that is itself bad is rejected again (a chain).
        for key, tasks in self.tasks.items():
            if made >= budget:
                break
            task = next((t for t in tasks if t.kind is TaskKind.REJECT_REPLACEMENT), None)
            if task is None:
                continue
            for scan_id in self.scans_of(key):
                if not self._is_confirmed_replacement(scan_id):
                    continue
                if scan_lifecycle.state_of(self.database, scan_id) is not LifecycleState.ACTIVE:
                    continue
                if not self._due(task, f"reject:{scan_id}", progress=progress, final=final):
                    continue
                with self.lock:
                    scan_lifecycle.reject_scan(
                        self.database, scan_id, reviewer=OPERATOR,
                        reason=RejectionReason.POOR_QUALITY,
                        note="The rescan is smudged; scan the paper again.",
                        declared_candidate_id=task.value, declared_set_code=task.set_code,
                    )
                    self._decided("reject_replacement", scan_id=scan_id, content=key)
                made += 1
        if made >= budget:
            return made
        wanted = {
            task.content: task.value
            for tasks in self.tasks.values() for task in tasks
            if task.kind is TaskKind.CONFIRM_REPLACEMENT
        }
        if not wanted or not self._awaiting_rescan():
            return made
        cases = scan_lifecycle.session_possible_rescans(self.database, self.session_id)
        for original, candidates in cases.items():
            if made >= budget:
                break
            original_key = self.content_of(original)
            if original_key is None or original_key not in wanted:
                continue
            target = wanted[original_key]
            match = next(
                (item for item in candidates if self.content_of(item.scan_id) == target), None
            )
            if match is None:
                continue
            key = original_key
            task = self._task(key, TaskKind.CONFIRM_REPLACEMENT)
            assert task is not None
            if not self._due(task, f"replace:{original}", progress=progress, final=final):
                continue
            with self.lock:
                scan_lifecycle.confirm_replacement(
                    self.database, original, match.scan_id, reviewer=OPERATOR
                )
                self._decided("confirm_replacement", scan_id=original, content=key,
                              replacement_scan_id=match.scan_id, replacement=target,
                              cross_source=bool(getattr(match, "source_label", "")))
            made += 1
        return made

    def _awaiting_rescan(self) -> bool:
        """Whether any sheet of the session is rejected and awaiting its rescan (cheap)."""
        from sqlalchemy import func, select

        from omr_scanner.database.models import ScanBatch, ScanRejection

        with self.database.session() as session:
            return bool(session.scalar(
                select(func.count()).select_from(ScanRejection)
                .join(ScanBatch, ScanBatch.batch_id == ScanRejection.batch_id)
                .where(ScanBatch.scan_session_id == self.session_id)
                .where(ScanRejection.state == "rejected_pending_rescan")
            ))

    def _is_confirmed_replacement(self, scan_id: int) -> bool:
        from sqlalchemy import func, select

        from omr_scanner.database.models import ScanRejection

        with self.database.session() as session:
            return bool(session.scalar(
                select(func.count()).select_from(ScanRejection)
                .where(ScanRejection.replacement_scan_id == scan_id)
            ))

    # ------------------------------------------------------------------
    # After closing: attendance decisions, scoring and reports
    # ------------------------------------------------------------------
    PHASES = ("first_close", "reopen")

    def downstream(
        self,
        *,
        phase: str,
        template: OmrTemplate,
        output_dir: Path,
        inputs: Path,
        project_name: str,
        final: bool = True,
    ) -> dict[str, Any]:
        """The office's work after closing: attendance, scoring, every set's report.

        Per set: reconcile the session against the set's attendance list (the
        statuses before any decision are recorded); if the office's planned
        corrections change the list, write the corrected list and assign it
        (``set_attendance.assign_attendance_workbook`` - the result template
        follows the list); set aside accidental second scans and dismiss
        scripts of nobody on the list (recorded against the list in use);
        reconcile again; score; generate the result workbook.

        Returns ``{set code: {...}}`` with the roster ids, both status maps,
        the decision count, the scoring counts and the generation outcome.
        """
        from omr_scanner.domain.reconciliation import ReconciliationReason
        from omr_scanner.services import (
            candidate_import,
            project_sets,
            reconciliation_store,
            report_store,
            session_scope,
            set_attendance,
        )

        from .project import write_attendance_workbook

        upto = self.PHASES[: self.PHASES.index(phase) + 1]
        self.refresh_scans()
        store = session_scope.store(self.database, self.session_id)
        outcomes: dict[str, Any] = {}
        sets = {item.code: item.set_id for item in project_sets.list_sets(self.database)}
        for code in self.plan.config.sets:
            set_id = sets[code]
            roster_id = report_store.resolve_set_sources(self.database, set_id).roster_id
            session_scope.reconcile(self.database, roster_id, self.session_id)
            before = {
                entry.candidate_id: entry.status.value
                for entry in session_scope.entries(self.database, roster_id, self.session_id)
            }
            wanted: dict[str, bool] = {}
            for task in self.plan.tasks:
                if task.set_code == code and task.phase in upto and task.kind in (
                    TaskKind.LIST_ABSENT, TaskKind.LIST_PRESENT
                ):
                    wanted[task.candidate] = task.kind is TaskKind.LIST_ABSENT
            current = self._lists.setdefault(code, {})
            decisions = 0
            new_list = False
            if wanted != current:
                version = self._list_versions.get(code, 1) + 1
                self._list_versions[code] = version
                path = write_attendance_workbook(
                    self.plan, code, inputs / f"attendance_set_{code}_v{version}.xlsx",
                    absent=wanted,
                )
                with self.lock:
                    assignment = set_attendance.assign_attendance_workbook(
                        self.database, set_id, path, candidate_import.read_roster(path),
                        imported_by=OPERATOR,
                    )
                roster_id = assignment.roster_id
                self._lists[code] = dict(wanted)
                new_list = True
                decisions += 1
                self._decided("assign_corrected_list", set_code=code, phase=phase,
                              version=version, roster_id=roster_id,
                              corrections=sorted(wanted.items()))
                session_scope.reconcile(self.database, roster_id, self.session_id)
            if new_list or phase == self.PHASES[0]:
                for task in self.plan.tasks:
                    if task.set_code != code or task.phase not in upto:
                        continue
                    with self.lock:
                        if task.kind is TaskKind.EXCLUDE_SCRIPT:
                            reconciliation_store.set_script_excluded(
                                self.database, roster_id, store,
                                self._effective_scan(task.content), excluded=True,
                                operator=OPERATOR,
                                reason=ReconciliationReason.ACCIDENTAL_RESCAN,
                            )
                        elif task.kind is TaskKind.DISMISS_UNKNOWN:
                            reconciliation_store.dismiss_entry(
                                self.database, roster_id, store, task.candidate,
                                operator=OPERATOR, reason=ReconciliationReason.ROSTER_ERROR,
                                reason_text="Not a candidate of this examination.",
                            )
                        else:
                            continue
                        decisions += 1
                        self._decided(f"reconcile_{task.kind.value}", candidate=task.candidate,
                                      set_code=code, phase=phase, roster_id=roster_id)
                session_scope.reconcile(self.database, roster_id, self.session_id)
            after = {
                entry.candidate_id: entry.status.value
                for entry in session_scope.entries(self.database, roster_id, self.session_id)
            }
            counts = session_scope.score(
                self.database, roster_id, self.session_id, template, computed_by=OPERATOR
            )
            outcome = session_scope.generate_for_set(
                self.database, set_id, self.session_id, template, project_name=project_name,
                output_dir=output_dir, computed_by=OPERATOR, final=final,
            )
            outcomes[code] = {
                "set_id": set_id,
                "roster_id": roster_id,
                "list_version": self._list_versions.get(code, 1),
                "statuses_before": before,
                "statuses_after": after,
                "decisions": decisions,
                "scored": counts.scored,
                "absent": counts.absent,
                "blocked": counts.blocked,
                "stale": counts.stale,
                "status": outcome.status,
                "report_id": outcome.report_id,
                "report": str(outcome.output_path) if outcome.output_path else "",
                "warnings": list(outcome.warnings),
            }
        return outcomes

    def release_held(self, names: set[tuple[str, str]]) -> int:
        """Release held files the plan expects - ``(source label, file name)`` - after a reopen.

        ``intake_decisions.decide_file`` with *release*: the file is re-read,
        re-verified and registered into the reopened session by the engine.
        """
        from omr_scanner.services import intake_decisions

        released = 0
        for item in intake_decisions.pending_decisions(self.database, self.session_id):
            label = item.source_label.removeprefix("Scanner ").strip()
            if (label, item.file_name) not in names:
                self.stats.unexpected.add(f"waiting file {item.file_name} ({item.state.value})")
                continue
            if intake_decisions.FileDecision.RELEASE not in item.options:
                continue
            with self.lock:
                intake_decisions.decide_file(
                    self.database, item.intake_file_id, intake_decisions.FileDecision.RELEASE,
                    reviewer=OPERATOR, note="The missing script was found after closing.",
                )
                self._decided("release_held", intake_file_id=item.intake_file_id,
                              file_name=item.file_name, source=label)
            released += 1
        return released

    def _effective_scan(self, key: str) -> int:
        from omr_scanner.services import session_population

        population = session_population.session_population(self.database, self.session_id)
        for scan_id in self.scans_of(key):
            if scan_id in population.effective:
                return scan_id
        raise LookupError(f"content {key} has no effective sheet")


__all__ = ["IDENTIFIER_TYPES", "OperatorActor", "OperatorStats"]
