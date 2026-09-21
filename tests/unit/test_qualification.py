"""Unit tests for the Phase 10 qualification harness (Phase 10, §30/§31/§44).

Everything here runs at a few sheets rather than 100,000, starts no
``benchmark_stress`` subprocess and kills nothing. The harness was written so
that the decision "did this run qualify?" is a pure function of measurements
already taken (:func:`~omr_scanner.evaluation.qualification.evaluate_assertions`),
and these tests exercise that function - plus the config, the durable state
machine, the evidence readers, the digest, the telemetry CSV and the reports -
by handing it synthetic measurements directly.

The tests that matter most are the ones guarding against a *vacuous* pass:
``semantic_digest`` ignoring timings but nothing else, and
``evaluate_assertions`` refusing to pass the recovery assertions against an
empty pre-kill set. Both are named and commented as such below.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import psutil
import pytest

from omr_scanner.database.models import ScanJobStatus
from omr_scanner.evaluation import qualification
from omr_scanner.tools import phase10_qualification

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = REPO_ROOT / "examples" / "templates" / "100_question_4_choice_example.omrt"


# ----------------------------------------------------------------------
# Builders for synthetic measurements
# ----------------------------------------------------------------------
def _result_json(**overrides: Any) -> str:
    """Return a plausible ``result_json`` payload with ``overrides`` applied."""
    payload: dict[str, Any] = {
        "outcome": "completed",
        "registration": {"aligned": True, "residual": 0.41},
        "warnings": [],
        "fields": {"roll": "0000001", "set": "A"},
        "answers": {"q1": "A", "q2": "C"},
        "timings": {"decode": 0.11, "recognise": 0.42},
        "elapsed_seconds": 0.53,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _clean_rows(count: int) -> dict[int, tuple[str, str]]:
    """Return ``count`` completed rows keyed by ``batch_index``."""
    return {
        index: (ScanJobStatus.COMPLETED.value, _result_json(fields={"roll": f"{index:07d}"}))
        for index in range(count)
    }


def _clean_integrity() -> dict[str, Any]:
    """Return the integrity report a healthy project produces."""
    return {
        "quick_check": ["ok"],
        "integrity_check": ["ok"],
        "foreign_key_check": [],
        "page_count": 512,
        "journal_mode": "wal",
        "health_ok": True,
        "health_issues": [],
    }


def _kill_evidence(*, orphan_pids: tuple[int, ...] = ()) -> qualification.KillEvidence:
    """Return kill evidence, optionally naming worker pids that survived."""
    return qualification.KillEvidence(
        requested_at="2026-09-21T00:00:00+00:00",
        committed_before_kill=3,
        worker_pids=(4101, 4102),
        coordinator_pid=4100,
        lock_file_left_behind=True,
        orphan_pids=orphan_pids,
        orphans_force_cleaned=orphan_pids,
        exit_code=None,
    )


def _config(output_dir: Path, **overrides: Any) -> qualification.QualificationConfig:
    """Return a tiny campaign config rooted at ``output_dir``."""
    defaults: dict[str, Any] = {
        "output_dir": output_dir,
        "template_path": TEMPLATE_PATH,
        "sheets": 5,
        "warmup_sheets": 0,
    }
    defaults.update(overrides)
    return qualification.QualificationConfig(**defaults)


def _release_config(output_dir: Path, **overrides: Any) -> qualification.QualificationConfig:
    """Return a config that satisfies every release-qualification condition.

    ``overrides`` is merged rather than passed alongside the release defaults,
    so a test can override any one of them - which is exactly what the
    "individually deficient" cases below do.
    """
    settings: dict[str, Any] = {
        "sheets": qualification.DEFAULT_SHEETS,
        "checkpoints": qualification.DEFAULT_CHECKPOINTS,
        "mode": "full",
    }
    settings.update(overrides)
    return _config(output_dir, **settings)


def _evaluate(
    config: qualification.QualificationConfig, **overrides: Any
) -> dict[str, qualification.Assertion]:
    """Evaluate the assertions for a clean run, then apply ``overrides``.

    Returns the assertions keyed by name, so a test can name exactly the one
    it is about without depending on their order.
    """
    arguments: dict[str, Any] = {
        "config": config,
        "final_rows": _clean_rows(config.sheets),
        "pre_kill_rows": None,
        "checkpoint_target": None,
        "resumed_submissions": [],
        "kill": None,
        "integrity": _clean_integrity(),
        "reference_digest": None,
        "exit_code": 0,
    }
    arguments.update(overrides)
    return {item.name: item for item in qualification.evaluate_assertions(**arguments)}


def _failed(results: dict[str, qualification.Assertion]) -> set[str]:
    """Return the names of every assertion that did not hold."""
    return {name for name, item in results.items() if not item.passed}


def _dead_pid() -> int:
    """Return a pid that no live process holds.

    Searched rather than hard-coded: "999999 is surely free" is the kind of
    assumption that makes a test flaky on someone else's machine.
    """
    for candidate in range(999_999, 900_000, -1):
        if not psutil.pid_exists(candidate):
            return candidate
    raise RuntimeError("every candidate pid is in use")  # pragma: no cover - absurd


def _write_telemetry(path: Path, samples: list[tuple[str, int]]) -> None:
    """Write a telemetry CSV holding ``(run_id, process_count)`` samples."""
    with qualification.TelemetryWriter(path) as writer:
        for run_id, count in samples:
            writer.write_row({"run_id": run_id, "process_count": count})


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
class TestQualificationConfig:
    def test_the_default_campaign_is_the_reference_plus_one_run_per_checkpoint(
        self, tmp_path: Path
    ) -> None:
        """The release campaign's shape is fixed: R0 then five kill runs."""
        assert _config(tmp_path).run_ids() == ("R0", "K01", "K25", "K50", "K75", "K99")

    def test_reference_mode_runs_only_the_uninterrupted_run(self, tmp_path: Path) -> None:
        """``reference`` mode is the single stress run, with no kills at all."""
        assert _config(tmp_path, mode="reference").run_ids() == ("R0",)

    def test_the_reference_run_has_no_checkpoint_and_a_kill_run_does(
        self, tmp_path: Path
    ) -> None:
        """``checkpoint_for`` is what distinguishes R0 from a K run."""
        config = _config(tmp_path)
        assert config.checkpoint_for("R0") is None
        assert config.checkpoint_for("K75") == 75

    @pytest.mark.parametrize(
        ("sheets", "percent", "expected"),
        [
            (100_000, 1, 1_000),
            (100_000, 25, 25_000),
            (100_000, 99, 99_000),
            (1_000, 50, 500),
        ],
    )
    def test_the_committed_target_is_the_requested_percentage_of_the_sheets(
        self, tmp_path: Path, sheets: int, percent: int, expected: int
    ) -> None:
        """The kill point is an absolute committed count, derived once."""
        assert _config(tmp_path, sheets=sheets).committed_target(percent) == expected

    def test_a_tiny_sheet_count_never_yields_a_committed_target_of_zero(
        self, tmp_path: Path
    ) -> None:
        """A target of 0 would be reached before anything was committed.

        Every recovery assertion would then pass against an empty set, which
        is the vacuous pass this harness exists to make impossible - so the
        floor of 1 matters even at harness-validation scale.
        """
        config = _config(tmp_path, sheets=10)
        assert config.committed_target(1) == 1
        assert all(config.committed_target(p) >= 1 for p in range(1, 100))

    def test_an_explicit_worker_count_is_used_verbatim(self, tmp_path: Path) -> None:
        """Every run must share one worker count, so it is resolved once."""
        assert _config(tmp_path, workers=3).resolved_workers() == 3

    def test_a_worker_count_of_zero_is_derived_from_the_cpu_count(
        self, tmp_path: Path
    ) -> None:
        """0 means "half the logical CPUs", and never fewer than one."""
        resolved = _config(tmp_path, workers=0).resolved_workers()
        assert resolved == max((os.cpu_count() or 2) // 2, 1)
        assert resolved >= 1

    def test_the_config_round_trips_through_json(self, tmp_path: Path) -> None:
        """``resume`` rebuilds the config from the state file, so this must hold.

        A field lost here would silently change the campaign a resume runs
        from the one the report describes.
        """
        original = qualification.QualificationConfig(
            output_dir=tmp_path / "campaign",
            template_path=TEMPLATE_PATH,
            sheets=1_234,
            seed=987_654,
            checkpoints=(2, 40, 88),
            workers=6,
            opencv_threads=2,
            worker_recycle_after=250,
            mode="progressive",
            warmup_sheets=17,
            retain_passed_projects=True,
        )
        restored = qualification.QualificationConfig.from_json(
            json.loads(json.dumps(original.to_json()))
        )
        assert restored == original
        assert isinstance(restored.checkpoints, tuple)
        assert isinstance(restored.output_dir, Path)
        assert isinstance(restored.template_path, Path)

    def test_the_serialised_config_records_the_resolved_worker_count(
        self, tmp_path: Path
    ) -> None:
        """The report has to state the worker count that was actually used."""
        payload = _config(tmp_path, workers=0).to_json()
        assert payload["resolved_workers"] == _config(tmp_path).resolved_workers()


# ----------------------------------------------------------------------
# Durable campaign state
# ----------------------------------------------------------------------
class TestQualificationState:
    def test_the_state_round_trips_through_disk(self, tmp_path: Path) -> None:
        """A resume reads this file and nothing else, so it must be complete."""
        path = tmp_path / qualification.STATE_FILE_NAME
        state = qualification.QualificationState(path, _config(tmp_path))
        state.overall_status = "failed"
        state.notes.append("K25 failed: lost_committed_recognition_results.")
        state.begin("R0", "R0: running")
        state.finish("R0", "passed", detail="15 assertion(s), 0 failed", data={"passed": True})
        state.save()

        restored = qualification.QualificationState.load(path)
        assert restored.overall_status == "failed"
        assert restored.notes == state.notes
        assert restored.config == state.config
        assert restored.stages["R0"].status == "passed"
        assert restored.stages["R0"].detail == "15 assertion(s), 0 failed"
        assert restored.stages["R0"].data == {"passed": True}
        assert restored.started_at == state.started_at

    def test_begin_marks_a_stage_running_on_disk_before_anything_else_happens(
        self, tmp_path: Path
    ) -> None:
        """Asserted against the file, not the object.

        The whole point of ``begin`` is that an orchestrator killed during a
        stage leaves that stage marked ``running`` on disk; an in-memory-only
        transition would prove nothing.
        """
        path = tmp_path / qualification.STATE_FILE_NAME
        state = qualification.QualificationState(path, _config(tmp_path))
        state.save()
        before = path.read_text(encoding="utf-8")

        state.begin("K50", "K50: running")

        after = json.loads(path.read_text(encoding="utf-8"))
        assert path.read_text(encoding="utf-8") != before
        assert after["stages"]["K50"]["status"] == "running"
        assert after["stages"]["K50"]["started_at"]
        assert after["stages"]["K50"]["finished_at"] == ""

    def test_finish_records_the_verdict_and_timestamp_on_disk(self, tmp_path: Path) -> None:
        """A verdict held only in memory would be lost by the next crash."""
        path = tmp_path / qualification.STATE_FILE_NAME
        state = qualification.QualificationState(path, _config(tmp_path))
        state.begin("K50")
        state.finish("K50", "failed", detail="orphan workers survived")

        stage = json.loads(path.read_text(encoding="utf-8"))["stages"]["K50"]
        assert stage["status"] == "failed"
        assert stage["finished_at"]
        assert stage["detail"] == "orphan workers survived"

    def test_next_pending_run_is_the_first_run_not_yet_passed(self, tmp_path: Path) -> None:
        """A resume must skip verified work and pick up exactly where it stopped."""
        path = tmp_path / qualification.STATE_FILE_NAME
        state = qualification.QualificationState(path, _config(tmp_path))
        assert state.next_pending_run() == "R0"

        state.finish("R0", "passed")
        state.finish("K01", "passed")
        assert state.next_pending_run() == "K25"

        # A failed stage is not a verified one: the resume must return to it.
        state.finish("K25", "failed")
        assert state.next_pending_run() == "K25"

    def test_next_pending_run_is_none_once_every_run_has_passed(self, tmp_path: Path) -> None:
        """``None`` is how the CLI knows to rebuild the report and stop."""
        path = tmp_path / qualification.STATE_FILE_NAME
        state = qualification.QualificationState(path, _config(tmp_path))
        for run_id in state.config.run_ids():
            state.finish(run_id, "passed")
        assert state.next_pending_run() is None

    def test_save_leaves_no_temporary_file_and_always_valid_json(
        self, tmp_path: Path
    ) -> None:
        """The GUI monitor polls this file while the campaign writes it.

        A half-written state file, or a stray ``.tmp`` sibling the monitor
        might pick up, would make the monitor show nonsense at exactly the
        moment something interesting happened.
        """
        path = tmp_path / qualification.STATE_FILE_NAME
        state = qualification.QualificationState(path, _config(tmp_path))
        for _ in range(5):
            state.save()

        assert list(tmp_path.glob("*.tmp")) == []
        assert json.loads(path.read_text(encoding="utf-8"))["schema"] == 1


# ----------------------------------------------------------------------
# The semantic digest
# ----------------------------------------------------------------------
class TestSemanticDigest:
    def test_timings_and_elapsed_seconds_do_not_change_the_digest(self) -> None:
        """The single most important property in this module.

        R0 and every K run process the same deterministic sheet, so the digest
        is what proves their *decisions* agree. But two runs of the same sheet
        legitimately take different amounts of time, and ``result_json``
        records that. If the digest covered ``timings``/``elapsed_seconds``,
        every K run would fail ``semantic_reference_match`` for a reason with
        nothing to do with correctness - and the temptation would then be to
        weaken the comparison rather than fix it.
        """
        decision = {
            "outcome": "warning",
            "registration": {"aligned": True, "residual": 0.4},
            "warnings": ["DOUBLE_MARK"],
            "fields": {"roll": "0041207"},
            "answers": {"q1": "A", "q2": "B"},
        }
        fast = json.dumps({**decision, "timings": {"recognise": 0.01}, "elapsed_seconds": 0.02})
        slow = json.dumps({**decision, "timings": {"recognise": 9.87}, "elapsed_seconds": 12.5})

        assert (
            qualification.semantic_digest({41_207: (ScanJobStatus.WARNING.value, fast)})[41_207]
            == qualification.semantic_digest({41_207: (ScanJobStatus.WARNING.value, slow)})[41_207]
        )

    @pytest.mark.parametrize(
        ("key", "changed"),
        [
            ("outcome", "failed"),
            ("registration", {"aligned": False, "residual": 9.9}),
            ("warnings", ["BLANK_ANSWER"]),
            ("fields", {"roll": "9999999", "set": "B"}),
            ("answers", {"q1": "D", "q2": "C"}),
        ],
    )
    def test_a_changed_decision_changes_the_digest(self, key: str, changed: Any) -> None:
        """Every substantive key is a decision the pipeline made about a sheet.

        Parametrised over :data:`SEMANTIC_RESULT_KEYS` itself, so a key
        quietly dropped from the digest fails here.
        """
        assert key in qualification.SEMANTIC_RESULT_KEYS
        baseline = qualification.semantic_digest({1: ("completed", _result_json())})
        mutated = qualification.semantic_digest({1: ("completed", _result_json(**{key: changed}))})
        assert baseline[1] != mutated[1]

    def test_the_row_status_is_part_of_the_digest(self) -> None:
        """A sheet re-decided from ``warning`` to ``completed`` must be visible."""
        payload = _result_json()
        assert (
            qualification.semantic_digest({1: (ScanJobStatus.WARNING.value, payload)})[1]
            != qualification.semantic_digest({1: (ScanJobStatus.COMPLETED.value, payload)})[1]
        )

    def test_json_key_ordering_does_not_change_the_digest(self) -> None:
        """Serialised in canonical form, so SQLAlchemy's key order is irrelevant."""
        forward = json.dumps({"outcome": "completed", "answers": {"q1": "A"}, "warnings": []})
        reversed_order = json.dumps(
            {"warnings": [], "answers": {"q1": "A"}, "outcome": "completed"}
        )
        assert (
            qualification.semantic_digest({3: ("completed", forward)})[3]
            == qualification.semantic_digest({3: ("completed", reversed_order)})[3]
        )

    def test_an_extra_non_semantic_key_does_not_change_the_digest(self) -> None:
        """Only the listed keys count; anything else is noise the report ignores."""
        base = qualification.semantic_digest({4: ("completed", _result_json())})
        noisy = qualification.semantic_digest(
            {4: ("completed", _result_json(worker_pid=1234, host="build-agent"))}
        )
        assert base[4] == noisy[4]

    @pytest.mark.parametrize("payload", ["{not json", "]]", "not json at all"])
    def test_unparseable_result_json_digests_as_unparseable_rather_than_raising(
        self, payload: str
    ) -> None:
        """A crash in the reporter would destroy the evidence it was reporting.

        An unparseable payload is itself a difference the comparison should
        report, so it gets a sentinel digest instead of an exception.
        """
        assert qualification.semantic_digest({9: ("completed", payload)}) == {9: "unparseable"}

    @pytest.mark.parametrize("payload", ["[1, 2, 3]", "42", '"a string"'])
    def test_valid_json_that_is_not_an_object_digests_as_unparseable(
        self, payload: str
    ) -> None:
        """A result that is not a mapping cannot hold any decision key."""
        assert qualification.semantic_digest({9: ("completed", payload)}) == {9: "unparseable"}

    def test_an_empty_result_json_digests_as_its_status_alone(self) -> None:
        """An empty payload is a normal, expected state - not a corruption.

        A row the pipeline has not reached yet has no result at all, so it is
        digested over its status alone rather than as ``"unparseable"``: that
        keeps ``pending`` distinguishable from ``completed`` in the
        comparison, and it never raises.
        """
        empty = qualification.semantic_digest({9: (ScanJobStatus.PENDING.value, "")})
        no_keys = qualification.semantic_digest({9: (ScanJobStatus.PENDING.value, "{}")})
        assert empty == no_keys
        assert empty[9] != "unparseable"

    def test_the_digest_is_keyed_by_batch_index(self) -> None:
        """``batch_index`` is the sheet's identity across two separate projects.

        ``scan_id`` is an autoincrement surrogate that means nothing between
        R0's project and K75's, so keying the digest by it would make the
        reference comparison compare unrelated sheets.
        """
        rows = {41_207: ("completed", _result_json()), 3: ("completed", _result_json())}
        assert set(qualification.semantic_digest(rows)) == {3, 41_207}


# ----------------------------------------------------------------------
# What "durably committed" means
# ----------------------------------------------------------------------
class TestCommittedIndexes:
    def test_terminal_statuses_are_taken_from_the_model_not_restated(self) -> None:
        """A change to the state machine must not leave this module weaker."""
        from_the_model = {
            status.value for status in ScanJobStatus if status.is_terminal
        }
        assert from_the_model == qualification.TERMINAL_STATUSES

    def test_only_terminal_rows_count_as_committed(self) -> None:
        """Expectation derived from the model, so the test cannot drift from it."""
        rows = {
            index: (status.value, _result_json())
            for index, status in enumerate(ScanJobStatus)
        }
        expected = {
            index for index, status in enumerate(ScanJobStatus) if status.is_terminal
        }
        assert qualification.committed_indexes(rows) == expected

    @pytest.mark.parametrize(
        "status",
        [
            ScanJobStatus.PENDING,
            ScanJobStatus.QUEUED,
            ScanJobStatus.PROCESSING,
            ScanJobStatus.CANCELLED,
        ],
    )
    def test_unfinished_work_is_not_committed(self, status: ScanJobStatus) -> None:
        """Counting an unfinished row as committed would fake recovery evidence."""
        assert not status.is_terminal
        assert qualification.committed_indexes({7: (status.value, "")}) == set()

    @pytest.mark.parametrize(
        "status", [ScanJobStatus.COMPLETED, ScanJobStatus.WARNING, ScanJobStatus.FAILED]
    )
    def test_finished_work_is_committed(self, status: ScanJobStatus) -> None:
        """A resume must keep all three, including a genuine ``failed`` result."""
        assert status.is_terminal
        assert qualification.committed_indexes({7: (status.value, _result_json())}) == {7}


# ----------------------------------------------------------------------
# The submission log
# ----------------------------------------------------------------------
class TestReadSubmissionLog:
    def test_a_missing_log_reads_as_no_submissions(self, tmp_path: Path) -> None:
        """A run killed before writing anything must not crash the reporter."""
        assert qualification.read_submission_log(tmp_path / "nothing.txt") == []

    def test_indices_come_back_in_submission_order(self, tmp_path: Path) -> None:
        """Order is the evidence: the no-reprocessing check is about what came back."""
        path = tmp_path / "submitted.txt"
        path.write_text("7\n3\n41207\n1\n", encoding="utf-8")
        assert qualification.read_submission_log(path) == [7, 3, 41_207, 1]

    def test_a_final_line_truncated_by_a_kill_is_tolerated(self, tmp_path: Path) -> None:
        """This log is written by a process the campaign deliberately kills.

        A partial final write is an expected state, not corruption, and the
        lines that did land are the measurement. Written as raw bytes so the
        truncation is genuine rather than a tidy string.
        """
        path = tmp_path / "submitted.txt"
        path.write_bytes(b"11\n12\n13\n14\x00\x00")
        assert qualification.read_submission_log(path) == [11, 12, 13]

    def test_blank_and_junk_lines_are_skipped(self, tmp_path: Path) -> None:
        """A log is evidence, so unreadable lines are dropped, never guessed at."""
        path = tmp_path / "submitted.txt"
        path.write_text("1\n\n  \n2\nnot-a-number\n\t3\t\n", encoding="utf-8")
        assert qualification.read_submission_log(path) == [1, 2, 3]


# ----------------------------------------------------------------------
# The release-blocking assertions
# ----------------------------------------------------------------------
class TestEvaluateAssertions:
    def test_the_emitted_assertion_names_are_exactly_the_release_blocking_tuple(
        self, tmp_path: Path
    ) -> None:
        """Catches a declared-but-never-measured assertion, and its converse.

        Adding a name to ``RELEASE_BLOCKING_ASSERTIONS`` without emitting it
        would let a run "satisfy" a criterion nobody checks; emitting one that
        is not in the tuple would leave it out of the report's own list.
        """
        expected = set(qualification.RELEASE_BLOCKING_ASSERTIONS)
        config = _config(tmp_path)
        assert set(_evaluate(config)) == expected
        assert (
            set(
                _evaluate(
                    config,
                    pre_kill_rows=_clean_rows(3),
                    checkpoint_target=3,
                    resumed_submissions=[3, 4],
                    kill=_kill_evidence(),
                )
            )
            == expected
        )
        assert len(qualification.RELEASE_BLOCKING_ASSERTIONS) == len(expected)

    def test_a_clean_reference_run_passes_everything(self, tmp_path: Path) -> None:
        """R0's shape: no kill, no reference to compare against, exit 0."""
        results = _evaluate(_config(tmp_path))
        assert _failed(results) == set()

    @pytest.mark.parametrize(
        "name",
        [
            "kill_reached_requested_checkpoint",
            "previously_completed_jobs_rescheduled_for_recognition",
            "lost_committed_recognition_results",
            "changed_previously_committed_results",
            "orphan_workers_after_forced_kill",
        ],
    )
    def test_the_kill_assertions_are_marked_not_applicable_for_the_reference_run(
        self, tmp_path: Path, name: str
    ) -> None:
        """R0 is never killed, so these are recorded as n/a rather than as passes.

        The report has to be able to show *why* they held, and "there was no
        kill" is a different statement from "recovery was verified".
        """
        assertion = _evaluate(_config(tmp_path))[name]
        assert assertion.passed
        assert assertion.observed == "not applicable"
        assert "n/a" in assertion.expected

    def test_a_wrong_final_row_count_fails_the_sheet_count_assertion(
        self, tmp_path: Path
    ) -> None:
        """100,000 sheets in means 100,000 terminal rows out, or it did not happen."""
        config = _config(tmp_path)
        results = _evaluate(config, final_rows=_clean_rows(config.sheets - 1))
        assert _failed(results) == {"logical_sheet_count"}
        assert results["logical_sheet_count"].observed.startswith("4 rows")

    def test_a_row_left_pending_fails_the_pending_assertion(self, tmp_path: Path) -> None:
        """A completed batch with an unprocessed sheet has silently lost work."""
        config = _config(tmp_path)
        rows = _clean_rows(config.sheets)
        rows[2] = (ScanJobStatus.PENDING.value, "")
        results = _evaluate(config, final_rows=rows)
        assert not results["unexpected_pending_after_completion"].passed
        assert results["unexpected_running_after_completion"].passed

    def test_a_row_left_processing_fails_the_running_assertion(self, tmp_path: Path) -> None:
        """A stale ``processing`` row means a reopen failed to reset it."""
        config = _config(tmp_path)
        rows = _clean_rows(config.sheets)
        rows[2] = (ScanJobStatus.PROCESSING.value, "")
        results = _evaluate(config, final_rows=rows)
        assert not results["unexpected_running_after_completion"].passed
        assert results["unexpected_pending_after_completion"].passed

    def test_an_empty_pre_kill_set_fails_rather_than_passing_vacuously(
        self, tmp_path: Path
    ) -> None:
        """Anti-vacuity, and this guards a real defect found during validation.

        A stale telemetry ``latest`` made the checkpoint condition true the
        instant a run started, so K50 was killed before it had committed a
        single sheet. Every "no committed result was lost / re-read / changed"
        assertion then passed - against an empty set. A vacuous pass is worse
        than a failure, because it looks like evidence.
        """
        config = _config(tmp_path)
        results = _evaluate(
            config,
            pre_kill_rows={},
            checkpoint_target=config.committed_target(25),
            resumed_submissions=[0, 1, 2, 3, 4],
            kill=_kill_evidence(),
        )
        assert not results["kill_reached_requested_checkpoint"].passed
        assert not results["previously_completed_jobs_rescheduled_for_recognition"].passed

    def test_a_kill_that_landed_near_the_checkpoint_passes(self, tmp_path: Path) -> None:
        """The floor is 80% of the requested target, not the target exactly.

        The kill is fired from outside the run against a moving committed
        count, so demanding an exact landing point would fail runs that
        recovered perfectly.
        """
        config = _config(tmp_path, sheets=1_000)
        pre = _clean_rows(410)
        results = _evaluate(
            config,
            final_rows=_clean_rows(1_000),
            pre_kill_rows=pre,
            checkpoint_target=500,
            resumed_submissions=list(range(410, 1_000)),
            kill=_kill_evidence(),
        )
        assert results["kill_reached_requested_checkpoint"].passed
        assert _failed(results) == set()

    def test_resubmitting_an_already_committed_sheet_fails_and_names_it(
        self, tmp_path: Path
    ) -> None:
        """Measured from the run's own submission log, not inferred from counts.

        Re-reading a sheet that was already committed is wasted work at best
        and a changed result at worst, so the offending index is named.
        """
        config = _config(tmp_path)
        results = _evaluate(
            config,
            pre_kill_rows=_clean_rows(3),
            checkpoint_target=3,
            resumed_submissions=[2, 3, 4],
            kill=_kill_evidence(),
        )
        offender = results["previously_completed_jobs_rescheduled_for_recognition"]
        assert _failed(results) == {"previously_completed_jobs_rescheduled_for_recognition"}
        assert "[2]" in offender.observed

    def test_a_resume_that_submitted_nothing_fails_the_same_assertion(
        self, tmp_path: Path
    ) -> None:
        """Otherwise "it re-read nothing" would pass by having done nothing."""
        config = _config(tmp_path)
        results = _evaluate(
            config,
            pre_kill_rows=_clean_rows(3),
            checkpoint_target=3,
            resumed_submissions=[],
            kill=_kill_evidence(),
        )
        assert not results["previously_completed_jobs_rescheduled_for_recognition"].passed

    def test_a_committed_sheet_missing_from_the_final_rows_is_a_loss(
        self, tmp_path: Path
    ) -> None:
        """The core durability promise: a resume never discards committed work."""
        config = _config(tmp_path)
        rows = _clean_rows(config.sheets)
        # Cancelled, not pending: non-terminal, so it drops out of the
        # committed set without also tripping the pending/processing checks.
        rows[1] = (ScanJobStatus.CANCELLED.value, "")
        results = _evaluate(
            config,
            final_rows=rows,
            pre_kill_rows=_clean_rows(3),
            checkpoint_target=3,
            resumed_submissions=[3, 4],
            kill=_kill_evidence(),
        )
        assert not results["lost_committed_recognition_results"].passed
        assert "[1]" in results["lost_committed_recognition_results"].observed

    def test_a_re_decided_pre_kill_result_fails_the_change_assertion(
        self, tmp_path: Path
    ) -> None:
        """A resume must not re-decide a sheet it had already committed."""
        config = _config(tmp_path)
        pre = _clean_rows(3)
        final = _clean_rows(config.sheets)
        final[0] = (ScanJobStatus.COMPLETED.value, _result_json(answers={"q1": "D", "q2": "D"}))
        results = _evaluate(
            config,
            final_rows=final,
            pre_kill_rows=pre,
            checkpoint_target=3,
            resumed_submissions=[3, 4],
            kill=_kill_evidence(),
        )
        assert _failed(results) == {"changed_previously_committed_results"}
        assert "[0]" in results["changed_previously_committed_results"].observed

    def test_an_orphaned_worker_fails_the_orphan_assertion(self, tmp_path: Path) -> None:
        """A worker outliving its killed coordinator is a release blocker.

        It is the exact defect the Phase 10 audit found in the field: a
        crashed run leaving recognition processes chewing a CPU each.
        """
        config = _config(tmp_path)
        results = _evaluate(
            config,
            pre_kill_rows=_clean_rows(3),
            checkpoint_target=3,
            resumed_submissions=[3, 4],
            kill=_kill_evidence(orphan_pids=(4101,)),
        )
        assert not results["orphan_workers_after_forced_kill"].passed
        assert "4101" in results["orphan_workers_after_forced_kill"].observed

    def test_missing_kill_evidence_fails_the_orphan_assertion(self, tmp_path: Path) -> None:
        """No evidence is not the same as no orphans."""
        config = _config(tmp_path)
        results = _evaluate(
            config,
            pre_kill_rows=_clean_rows(3),
            checkpoint_target=3,
            resumed_submissions=[3, 4],
            kill=None,
        )
        assert not results["orphan_workers_after_forced_kill"].passed

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("quick_check", ["*** in database main ***", "page 4 is never used"]),
            ("quick_check", []),
            ("integrity_check", ["row 3 missing from index ix_batch_scan_batch_status"]),
            ("integrity_check", []),
        ],
    )
    def test_a_failing_sqlite_structural_check_fails_its_assertion(
        self, tmp_path: Path, key: str, value: list[str]
    ) -> None:
        """Only the literal ``["ok"]`` counts - a missing result is not a pass."""
        results = _evaluate(_config(tmp_path), integrity=_clean_integrity() | {key: value})
        assert not results[key].passed

    def test_a_foreign_key_violation_fails_its_assertion(self, tmp_path: Path) -> None:
        """``PRAGMA integrity_check`` does not look at foreign keys, so this is separate."""
        integrity = _clean_integrity() | {
            "foreign_key_check": [["batch_scan", 17, "scan_batch", 0]]
        }
        results = _evaluate(_config(tmp_path), integrity=integrity)
        assert not results["foreign_key_check"].passed
        assert "1 violation(s)" in results["foreign_key_check"].observed

    def test_warning_level_health_issues_do_not_fail_the_campaign(
        self, tmp_path: Path
    ) -> None:
        """A stress project legitimately carries these two for its whole life.

        It has no verified answer key (the dataset's set codes are synthetic
        and nothing here scores anything) and no backup (it is a throwaway).
        ``HealthReport.is_ok`` is False for a mere warning, so asserting on
        ``is_ok`` would fail the qualification for the project not being a
        real examination project. Every warning is still reported.
        """
        integrity = _clean_integrity() | {
            "health_ok": False,
            "health_issues": [
                {
                    "level": "warning",
                    "code": "SETS_WITHOUT_A_VERIFIED_KEY",
                    "message": "1 recognised set(s) have no verified answer key: A",
                },
                {
                    "level": "warning",
                    "code": "NO_BACKUPS",
                    "message": "No project backup has ever been created.",
                },
            ],
        }
        results = _evaluate(_config(tmp_path), integrity=integrity)
        assert results["application_invariants"].passed
        # Reported, never swallowed.
        assert "SETS_WITHOUT_A_VERIFIED_KEY" in results["application_invariants"].observed
        assert "NO_BACKUPS" in results["application_invariants"].observed

    @pytest.mark.parametrize("level", ["error", "critical"])
    def test_an_error_level_health_issue_fails_the_campaign(
        self, tmp_path: Path, level: str
    ) -> None:
        """The warning exemption above must not become a blanket one."""
        integrity = _clean_integrity() | {
            "health_ok": False,
            "health_issues": [
                {"level": level, "code": "FOREIGN_KEY_VIOLATION", "message": "orphaned row"}
            ],
        }
        results = _evaluate(_config(tmp_path), integrity=integrity)
        assert not results["application_invariants"].passed
        assert "FOREIGN_KEY_VIOLATION" in results["application_invariants"].observed

    def test_a_run_matching_the_reference_digest_passes(self, tmp_path: Path) -> None:
        """Identical decisions for every sheet is what a K run has to show."""
        config = _config(tmp_path)
        rows = _clean_rows(config.sheets)
        results = _evaluate(
            config, final_rows=rows, reference_digest=qualification.semantic_digest(rows)
        )
        assert results["semantic_reference_match"].passed

    def test_the_reference_run_establishes_rather_than_matches_the_digest(
        self, tmp_path: Path
    ) -> None:
        """R0 has nothing to compare against; the report says exactly that."""
        assertion = _evaluate(_config(tmp_path), reference_digest=None)[
            "semantic_reference_match"
        ]
        assert assertion.passed
        assert assertion.observed == "reference established"

    def test_a_differing_sheet_fails_the_reference_match(self, tmp_path: Path) -> None:
        """One re-decided sheet out of 100,000 is still a release blocker."""
        config = _config(tmp_path)
        rows = _clean_rows(config.sheets)
        reference = qualification.semantic_digest(rows)
        reference[3] = "0" * 64
        results = _evaluate(config, final_rows=rows, reference_digest=reference)
        assert not results["semantic_reference_match"].passed
        assert "[3]" in results["semantic_reference_match"].observed

    def test_a_sheet_missing_relative_to_the_reference_fails_the_match(
        self, tmp_path: Path
    ) -> None:
        """The comparison is over the set of sheets as well as their contents."""
        config = _config(tmp_path)
        rows = _clean_rows(config.sheets)
        reference = qualification.semantic_digest(rows)
        reference[9_999] = "0" * 64
        results = _evaluate(config, final_rows=rows, reference_digest=reference)
        assert not results["semantic_reference_match"].passed
        assert "1 missing" in results["semantic_reference_match"].observed

    def test_an_unexpected_extra_sheet_fails_the_match(self, tmp_path: Path) -> None:
        """A sheet the reference never saw means the datasets were not the same."""
        config = _config(tmp_path)
        rows = _clean_rows(config.sheets)
        reference = qualification.semantic_digest(rows)
        del reference[4]
        results = _evaluate(config, final_rows=rows, reference_digest=reference)
        assert not results["semantic_reference_match"].passed
        assert "1 unexpected" in results["semantic_reference_match"].observed

    @pytest.mark.parametrize("exit_code", [1, 2, -1, 3221225477])
    def test_a_non_zero_exit_code_fails(self, tmp_path: Path, exit_code: int) -> None:
        """``-1`` is how the harness records a run that never exited at all."""
        results = _evaluate(_config(tmp_path), exit_code=exit_code)
        assert _failed(results) == {"exit_code"}
        assert results["exit_code"].observed == str(exit_code)


# ----------------------------------------------------------------------
# Scope: what a passing campaign is allowed to claim
# ----------------------------------------------------------------------
class TestReleaseQualificationScope:
    def test_the_full_campaign_at_full_scale_qualifies(self, tmp_path: Path) -> None:
        """All three conditions together, and none of them is negotiable."""
        assert qualification.is_release_qualification(_release_config(tmp_path))

    @pytest.mark.parametrize(
        ("overrides", "expected_reason"),
        [
            ({"sheets": 1_000}, "1,000 sheets per run, not 100,000"),
            ({"mode": "reference"}, "'reference' mode"),
            ({"mode": "progressive"}, "'progressive' mode"),
            ({"checkpoints": (1, 25, 50, 75)}, "99%"),
            ({"checkpoints": (50,)}, "1%, 25%, 75%, 99%"),
        ],
    )
    def test_an_individually_deficient_campaign_does_not_qualify(
        self, tmp_path: Path, overrides: dict[str, Any], expected_reason: str
    ) -> None:
        """The caveat has to name the specific reason, not just say "smaller".

        A convenient reduced run must not be citable later as the Phase 10
        qualification, so the report states exactly what was missing.
        """
        config = _release_config(tmp_path, **overrides)
        assert not qualification.is_release_qualification(config)
        caveat = qualification.qualification_scope_caveat(config)
        assert "NOT the Phase 10 release qualification" in caveat
        assert expected_reason in caveat

    def test_more_sheets_than_mandated_still_qualifies(self, tmp_path: Path) -> None:
        """The threshold is a minimum: 200,000 sheets is not a deficiency."""
        assert qualification.is_release_qualification(
            _release_config(tmp_path, sheets=200_000)
        )

    def test_an_extra_checkpoint_still_qualifies(self, tmp_path: Path) -> None:
        """The five mandated checkpoints are required, not exhaustive."""
        assert qualification.is_release_qualification(
            _release_config(tmp_path, checkpoints=(1, 10, 25, 50, 75, 99))
        )


# ----------------------------------------------------------------------
# Estimates and formatting
# ----------------------------------------------------------------------
class TestEstimates:
    def test_the_runtime_estimate_grows_with_the_sheet_count(self, tmp_path: Path) -> None:
        """An operator decides whether to start overnight on this number."""
        config = _release_config(tmp_path)
        assert qualification.estimate_runtime_seconds(
            replace(config, sheets=10_000)
        ) < qualification.estimate_runtime_seconds(replace(config, sheets=100_000))

    def test_the_runtime_estimate_grows_with_the_run_count(self, tmp_path: Path) -> None:
        """Six runs plus their re-done work cannot estimate shorter than one."""
        config = _release_config(tmp_path)
        assert qualification.estimate_runtime_seconds(
            replace(config, mode="reference")
        ) < qualification.estimate_runtime_seconds(config)

    def test_the_disk_estimate_grows_with_the_sheet_count(self, tmp_path: Path) -> None:
        """Per-run size is the measured growth per sheet, with a safety factor."""
        config = _release_config(tmp_path)
        small, _ = qualification.estimate_disk(replace(config, sheets=10_000))
        large, _ = qualification.estimate_disk(config)
        assert small < large

    def test_retaining_passed_projects_raises_the_estimated_peak(
        self, tmp_path: Path
    ) -> None:
        """Peak, not total: reclaiming a passed project is what keeps it at two."""
        config = _release_config(tmp_path)
        _per_run, reclaimed = qualification.estimate_disk(config)
        _per_run_kept, retained = qualification.estimate_disk(
            replace(config, retain_passed_projects=True)
        )
        assert retained > reclaimed

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "0 s"),
            (45, "45 s"),
            (89.4, "89 s"),
            (300, "5 min"),
            (3600, "60 min"),
            (5400, "1 h 30 min"),
            (36_000, "10 h 00 min"),
        ],
    )
    def test_format_duration(self, seconds: float, expected: str) -> None:
        """Rendered for a human deciding whether to start a multi-hour run."""
        assert qualification.format_duration(seconds) == expected

    @pytest.mark.parametrize(
        ("count", "expected"),
        [
            (0, "0 B"),
            (512, "512 B"),
            (1024, "1.0 KB"),
            (1536, "1.5 KB"),
            (1024**2, "1.0 MB"),
            (1024**3, "1.0 GB"),
            (1024**4, "1.0 TB"),
            (2 * 1024**4, "2.0 TB"),
        ],
    )
    def test_format_bytes(self, count: float, expected: str) -> None:
        """TB is the last unit, so a huge value is still rendered, never looped past."""
        assert qualification.format_bytes(count) == expected


# ----------------------------------------------------------------------
# Preflight
# ----------------------------------------------------------------------
def _preflight_config(tmp_path: Path, **overrides: Any) -> qualification.QualificationConfig:
    """Return a preflight-able config small enough to check determinism cheaply.

    Four sheets means the determinism check renders indices 0, 2 and 3 twice -
    six real renders, a fraction of a second - while exercising exactly the
    code path a 100,000-sheet campaign runs before committing to hours.
    """
    return _config(
        tmp_path / "campaign",
        sheets=4,
        checkpoints=(50,),
        mode="reference",
        **overrides,
    )


class TestPreflight:
    def test_a_viable_campaign_passes_preflight(self, tmp_path: Path) -> None:
        """Against the real shipped template and a real output directory."""
        report = qualification.preflight(_preflight_config(tmp_path))
        checks = {check.name: check for check in report.checks}
        assert report.passed
        assert checks["deterministic generator"].passed
        assert checks["template readable"].passed
        assert checks["output directory writable"].passed
        assert report.runs == ("R0",)
        assert report.estimated_peak_disk_bytes > 0

    def test_the_determinism_check_is_what_makes_the_comparison_meaningful(
        self, tmp_path: Path
    ) -> None:
        """If the generator is not deterministic, R0 is not a reference at all.

        Proven by rendering the same indices twice from two independently
        built specs and comparing bytes, before hours are spent.
        """
        report = qualification.preflight(_preflight_config(tmp_path))
        check = next(c for c in report.checks if c.name == "deterministic generator")
        assert check.passed
        assert check.blocking
        assert "byte for byte" in check.detail

    def test_a_missing_template_fails_preflight(self, tmp_path: Path) -> None:
        """A campaign that cannot read its template cannot start."""
        config = _preflight_config(tmp_path, template_path=tmp_path / "absent.omrt")
        report = qualification.preflight(config)
        checks = {check.name: check for check in report.checks}
        assert not checks["template readable"].passed
        assert checks["template readable"].blocking
        assert not report.passed

    def test_this_processs_own_lock_is_not_a_competing_campaign(
        self, tmp_path: Path
    ) -> None:
        """Regression test: ``execute_campaign`` takes the lock before preflight.

        An earlier version of this check asked "does a lock file exist?"
        rather than "is another campaign using this directory?", which made
        ``run`` unable to start at all - it always found its own lock.
        """
        config = _preflight_config(tmp_path)
        config.output_dir.mkdir(parents=True, exist_ok=True)
        (config.output_dir / qualification.LOCK_NAME).write_text(
            json.dumps({"pid": os.getpid(), "started_at": "2026-09-21T00:00:00+00:00"}),
            encoding="utf-8",
        )
        report = qualification.preflight(config)
        check = next(
            c for c in report.checks if c.name == "no competing campaign in this directory"
        )
        assert check.passed
        assert report.passed

    def test_a_lock_left_behind_by_a_dead_orchestrator_is_not_a_competing_campaign(
        self, tmp_path: Path
    ) -> None:
        """A stale lock is a leftover to clean up, not a reason to refuse."""
        config = _preflight_config(tmp_path)
        config.output_dir.mkdir(parents=True, exist_ok=True)
        (config.output_dir / qualification.LOCK_NAME).write_text(
            json.dumps({"pid": _dead_pid(), "started_at": "2026-09-21T00:00:00+00:00"}),
            encoding="utf-8",
        )
        report = qualification.preflight(config)
        check = next(
            c for c in report.checks if c.name == "no competing campaign in this directory"
        )
        assert check.passed
        assert report.passed

    def test_a_live_foreign_lock_does_block_a_second_campaign(self, tmp_path: Path) -> None:
        """Two campaigns sharing a directory would overwrite each other's evidence.

        ``psutil``'s own pid is a live process that is not this one, which is
        the condition the check is actually about.
        """
        config = _preflight_config(tmp_path)
        config.output_dir.mkdir(parents=True, exist_ok=True)
        holder = psutil.Process(os.getpid()).ppid()
        if holder in (0, os.getpid()) or not psutil.pid_exists(holder):
            pytest.skip("no live foreign pid available to impersonate a second campaign")
        (config.output_dir / qualification.LOCK_NAME).write_text(
            json.dumps({"pid": holder, "started_at": "2026-09-21T00:00:00+00:00"}),
            encoding="utf-8",
        )
        report = qualification.preflight(config)
        check = next(
            c for c in report.checks if c.name == "no competing campaign in this directory"
        )
        assert not check.passed
        assert not report.passed

    def test_an_existing_campaign_is_reported_without_blocking(self, tmp_path: Path) -> None:
        """"Use resume" is advice, not a refusal - ``run --restart`` is allowed."""
        config = _preflight_config(tmp_path)
        config.output_dir.mkdir(parents=True, exist_ok=True)
        (config.output_dir / qualification.STATE_FILE_NAME).write_text("{}", encoding="utf-8")
        report = qualification.preflight(config)
        check = next(c for c in report.checks if c.name == "previous qualification")
        assert not check.blocking
        assert "resume" in check.detail
        assert report.passed


# ----------------------------------------------------------------------
# Telemetry
# ----------------------------------------------------------------------
class TestTelemetryWriter:
    def test_the_header_is_exactly_the_declared_fields_and_rows_keep_their_order(
        self, tmp_path: Path
    ) -> None:
        """The CSV is the campaign's raw measurement record; its schema is fixed."""
        path = tmp_path / "telemetry.csv"
        with qualification.TelemetryWriter(path) as writer:
            writer.write_row({"run_id": "R0", "attempt": 1, "committed": 10})
            writer.write_row({"run_id": "K25", "attempt": 2, "committed": 20})

        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            assert tuple(reader.fieldnames or ()) == qualification.TelemetryWriter.FIELDS
            rows = list(reader)
        assert [row["run_id"] for row in rows] == ["R0", "K25"]
        assert [row["committed"] for row in rows] == ["10", "20"]

    def test_a_field_absent_from_the_row_is_written_as_empty(self, tmp_path: Path) -> None:
        """A sampler that could not read one figure must not lose the whole sample."""
        path = tmp_path / "telemetry.csv"
        with qualification.TelemetryWriter(path) as writer:
            writer.write_row({"run_id": "R0"})

        with path.open(newline="", encoding="utf-8") as handle:
            row = next(iter(csv.DictReader(handle)))
        assert row["run_id"] == "R0"
        assert row["timestamp"] == ""
        assert row["tree_memory_mb"] == ""

    def test_appending_to_an_existing_file_does_not_repeat_the_header(
        self, tmp_path: Path
    ) -> None:
        """One telemetry.csv per campaign, reopened by every run within it."""
        path = tmp_path / "telemetry.csv"
        with qualification.TelemetryWriter(path) as writer:
            writer.write_row({"run_id": "R0"})
        with qualification.TelemetryWriter(path) as writer:
            writer.write_row({"run_id": "K01"})

        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith("timestamp,run_id")
        assert sum(1 for line in lines if line.startswith("timestamp,run_id")) == 1
        assert len([line for line in lines if line.strip()]) == 3

    def test_begin_run_resets_the_per_run_peaks(self, tmp_path: Path) -> None:
        """Peaks are per run, spanning both the killed and the resumed attempt."""
        writer = qualification.TelemetryWriter(tmp_path / "telemetry.csv")
        try:
            writer.peak_tree_memory_mb = 8_192.0
            writer.peak_tree_cpu_percent = 780.0
            writer.sample_count = 412
            writer.begin_run()
            assert writer.peak_tree_memory_mb == 0.0
            assert writer.peak_tree_cpu_percent == 0.0
            assert writer.sample_count == 0
        finally:
            writer.close()

    def test_start_clears_the_previous_runs_progress_counts(self, tmp_path: Path) -> None:
        """Regression test for a real defect, and it is load-bearing, not tidiness.

        ``latest`` is what ``_wait_for_checkpoint`` reads to decide when a run
        has committed enough sheets to be worth killing. A stale ``latest``
        left over from the previous run made the checkpoint condition true the
        instant the next run started, so the kill fired before anything had
        been committed - and then every recovery assertion passed vacuously
        against an empty committed set.
        """
        writer = qualification.TelemetryWriter(tmp_path / "telemetry.csv")
        try:
            writer.latest = {"_committed": 5_000, "_total": 5_000}
            writer.start(
                run_id="K50",
                attempt=1,
                coordinator_pid=os.getpid(),
                db_path=tmp_path / "absent" / "database.sqlite",
            )
            assert writer.latest == {}
        finally:
            writer.stop()
            writer.close()


class TestWorkerRecyclingEvidence:
    def test_a_missing_telemetry_file_is_reported_not_raised(self, tmp_path: Path) -> None:
        """Reported as an observation with its evidence, never as a bare claim."""
        evidence = qualification.worker_recycling_evidence(tmp_path / "absent.csv", "R0")
        assert evidence["observed"] is False
        assert evidence["detail"] == "no telemetry file"

    def test_samples_for_another_run_are_ignored(self, tmp_path: Path) -> None:
        """One CSV holds every run, so the rows must be filtered by run id."""
        path = tmp_path / "telemetry.csv"
        _write_telemetry(path, [("K25", 9), ("K25", 2), ("K25", 9)])
        evidence = qualification.worker_recycling_evidence(path, "R0")
        assert evidence["observed"] is False
        assert evidence["detail"] == "no samples for R0"

    def test_a_dipping_process_count_is_recycling_being_observed(
        self, tmp_path: Path
    ) -> None:
        """Recycling shows up from outside as the pool shrinking and growing back."""
        path = tmp_path / "telemetry.csv"
        _write_telemetry(
            path,
            [("R0", 9), ("R0", 9), ("R0", 3), ("R0", 9), ("R0", 4), ("K01", 1)],
        )
        evidence = qualification.worker_recycling_evidence(path, "R0")
        assert evidence["observed"] is True
        assert evidence["pool_size_decreases_observed"] == 2
        assert evidence["samples"] == 5
        assert evidence["min_process_count"] == 3
        assert evidence["max_process_count"] == 9

    def test_a_flat_process_count_is_not_recycling(self, tmp_path: Path) -> None:
        """No dip means no evidence, and that is what gets reported."""
        path = tmp_path / "telemetry.csv"
        _write_telemetry(path, [("R0", 9), ("R0", 9), ("R0", 9)])
        evidence = qualification.worker_recycling_evidence(path, "R0")
        assert evidence["observed"] is False
        assert evidence["pool_size_decreases_observed"] == 0

    def test_the_samplers_priming_round_is_excluded(self, tmp_path: Path) -> None:
        """A leading zero is ``cpu_percent()`` being primed, not an empty pool.

        Counting it would report a spurious "the pool shrank to nothing" and,
        worse, a spurious recycling observation on a run where the pool never
        moved.
        """
        path = tmp_path / "telemetry.csv"
        _write_telemetry(path, [("R0", 0), ("R0", 9), ("R0", 9), ("R0", 9)])
        evidence = qualification.worker_recycling_evidence(path, "R0")
        assert evidence["observed"] is False
        assert evidence["samples"] == 3
        assert evidence["min_process_count"] == 9

    def test_unreadable_process_counts_are_skipped(self, tmp_path: Path) -> None:
        """A truncated CSV row must not crash the report that describes it."""
        path = tmp_path / "telemetry.csv"
        with qualification.TelemetryWriter(path) as writer:
            writer.write_row({"run_id": "R0", "process_count": 9})
            writer.write_row({"run_id": "R0"})
            writer.write_row({"run_id": "R0", "process_count": 4})
        evidence = qualification.worker_recycling_evidence(path, "R0")
        assert evidence["samples"] == 2
        assert evidence["pool_size_decreases_observed"] == 1


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------
def _run_data(
    run_id: str,
    checkpoint: int | None,
    *,
    passed: bool = True,
    assertions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the stage ``data`` payload ``execute_run`` would have stored."""
    items = assertions if assertions is not None else [
        {
            "name": name,
            "passed": True,
            "expected": "expected",
            "observed": "observed",
        }
        for name in qualification.RELEASE_BLOCKING_ASSERTIONS
    ]
    return {
        "run_id": run_id,
        "checkpoint_percent": checkpoint,
        "passed": passed,
        "assertions": items,
        "kill": None,
        "exit_code": 0,
        "elapsed_seconds": 123.5,
        "sheets_per_second": 11.24,
        "peak_tree_memory_mb": 4_096.0,
        "database_bytes": 2_117_812_736,
        "integrity": _clean_integrity(),
        "at_scale": {"load_summary": {"seconds": 0.02}},
        "configuration": {"batch_id": "batch-1"},
        "immutability": {"source_paths_examined": 5, "all_virtual_for_this_seed": True},
        "recycling": {"observed": True, "detail": "the tree shrank 3 time(s)"},
        "submitted_first_attempt": 3,
        "submitted_after_restart": 2,
        "notes": [],
        "failed_assertions": [item["name"] for item in items if not item["passed"]],
    }


def _state_with_runs(
    config: qualification.QualificationConfig,
    runs: dict[str, dict[str, Any]],
    *,
    overall_status: str,
) -> qualification.QualificationState:
    """Return a saved campaign state whose stages hold ``runs``."""
    state = qualification.QualificationState(
        config.output_dir / qualification.STATE_FILE_NAME, config
    )
    state.overall_status = overall_status
    for run_id, data in runs.items():
        state.finish(
            run_id, "passed" if data["passed"] else "failed", detail=run_id, data=data
        )
    return state


class TestBuildReports:
    def test_a_reduced_scale_pass_renders_the_scope_caveat(self, tmp_path: Path) -> None:
        """A validation run must not be citable as the release qualification."""
        config = _config(tmp_path, sheets=100, mode="reference")
        config.output_dir.mkdir(parents=True, exist_ok=True)
        state = _state_with_runs(
            config,
            {"R0": _run_data("R0", None)},
            overall_status="passed_not_qualification",
        )

        json_path, md_path = qualification.build_reports(state)
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        markdown = md_path.read_text(encoding="utf-8")

        assert json_path.is_file()
        assert md_path.is_file()
        assert payload["qualified"] is False
        assert payload["all_runs_passed"] is True
        assert payload["is_release_qualification"] is False
        assert payload["release_blocking_assertions"] == list(
            qualification.RELEASE_BLOCKING_ASSERTIONS
        )
        assert "## Result: ALL RUNS PASSED - NOT THE RELEASE QUALIFICATION" in markdown
        assert "Do not cite this report as the Phase 10 qualification" in markdown
        assert "R0" in markdown
        assert "## What this does and does not establish" in markdown

    def test_a_full_scale_pass_renders_the_qualified_headline(self, tmp_path: Path) -> None:
        """The only shape entitled to say "QUALIFIED"."""
        config = _release_config(tmp_path)
        config.output_dir.mkdir(parents=True, exist_ok=True)
        state = _state_with_runs(
            config,
            {
                run_id: _run_data(run_id, config.checkpoint_for(run_id))
                for run_id in config.run_ids()
            },
            overall_status="qualified",
        )

        json_path, md_path = qualification.build_reports(state)
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        markdown = md_path.read_text(encoding="utf-8")

        assert payload["qualified"] is True
        assert payload["is_release_qualification"] is True
        assert "## Result: QUALIFIED" in markdown
        assert "Do not cite this report" not in markdown
        assert "## What this does and does not establish" in markdown
        for run_id in config.run_ids():
            assert f"### {run_id}" in markdown

    def test_a_failed_run_is_rendered_as_a_failure_not_a_warning(
        self, tmp_path: Path
    ) -> None:
        """Nothing is downgraded to reach a conclusion - see the module docstring."""
        config = _config(tmp_path, sheets=100, mode="reference")
        config.output_dir.mkdir(parents=True, exist_ok=True)
        failing = [
            {
                "name": "lost_committed_recognition_results",
                "passed": False,
                "expected": "every pre-kill committed sheet still committed",
                "observed": "3 lost (first few: [7, 8, 9])",
            },
            {
                "name": "exit_code",
                "passed": True,
                "expected": "the final run exits 0",
                "observed": "0",
            },
        ]
        state = _state_with_runs(
            config,
            {"R0": _run_data("R0", None, passed=False, assertions=failing)},
            overall_status="failed",
        )

        json_path, md_path = qualification.build_reports(state)
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        markdown = md_path.read_text(encoding="utf-8")

        assert payload["all_runs_passed"] is False
        assert payload["qualified"] is False
        assert "| `lost_committed_recognition_results` | **FAIL**" in markdown
        assert "| `exit_code` | PASS" in markdown
        assert "did **not** pass" in markdown


# ----------------------------------------------------------------------
# Status, as the GUI monitor and the CLI read it
# ----------------------------------------------------------------------
class TestStopRequest:
    """The one thing the GUI monitor writes into a campaign's directory.

    An operator stop has to be distinguishable from every kind of failure and
    from the campaign's own deliberate kills, so these tests pin the three
    properties that make it so: it is only read between runs, it is consumed
    when honoured, and it is reported as its own status.
    """

    def test_no_sentinel_means_no_stop_was_asked_for(self, tmp_path: Path) -> None:
        """The overwhelmingly common case, checked once per run."""
        assert qualification.stop_requested(tmp_path) is False
        assert qualification.consume_stop_request(tmp_path) is False

    def test_the_sentinel_is_seen_without_being_consumed(self, tmp_path: Path) -> None:
        """The monitor shows "stopping after this run" without changing anything.

        :func:`stop_requested` is the read the GUI performs on a timer. If it
        consumed the sentinel, merely watching a campaign would cancel the
        operator's request.
        """
        (tmp_path / qualification.STOP_REQUEST_FILE_NAME).write_text("x", encoding="utf-8")
        assert qualification.stop_requested(tmp_path) is True
        assert qualification.stop_requested(tmp_path) is True
        assert (tmp_path / qualification.STOP_REQUEST_FILE_NAME).is_file()

    def test_consuming_the_sentinel_removes_it(self, tmp_path: Path) -> None:
        """Leaving it behind would make the next ``resume`` stop immediately.

        Which looks exactly like a campaign that cannot make progress - so the
        sentinel is taken as it is honoured, not merely read.
        """
        (tmp_path / qualification.STOP_REQUEST_FILE_NAME).write_text("x", encoding="utf-8")
        assert qualification.consume_stop_request(tmp_path) is True
        assert not (tmp_path / qualification.STOP_REQUEST_FILE_NAME).exists()
        assert qualification.consume_stop_request(tmp_path) is False

    def test_a_stopped_campaign_is_neither_qualified_nor_failed(
        self, tmp_path: Path
    ) -> None:
        """Its report must not claim either of the two outcomes that matter."""
        state = qualification.QualificationState(
            tmp_path / qualification.STATE_FILE_NAME, _config(tmp_path)
        )
        state.stages["R0"] = qualification.StageRecord(status="passed")
        state.overall_status = "stopped"
        state.save()

        json_path, md_path = qualification.build_reports(state)
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        markdown = md_path.read_text(encoding="utf-8")

        assert payload["qualified"] is False
        assert "STOPPED AT THE OPERATOR'S REQUEST" in markdown
        assert "NOT A FAILURE" in markdown

    def test_load_status_reports_a_pending_stop(self, tmp_path: Path) -> None:
        """So the monitor can say "stopping after the current run"."""
        qualification.QualificationState(
            tmp_path / qualification.STATE_FILE_NAME, _config(tmp_path)
        ).save()
        assert qualification.load_status(tmp_path)["stop_requested"] is False
        (tmp_path / qualification.STOP_REQUEST_FILE_NAME).write_text("x", encoding="utf-8")
        assert qualification.load_status(tmp_path)["stop_requested"] is True


class TestLoadStatus:
    def test_a_missing_campaign_is_reported_as_absent(self, tmp_path: Path) -> None:
        """Polled on a timer by the GUI, so "nothing there" is a normal answer."""
        status = qualification.load_status(tmp_path / "nowhere")
        assert status["exists"] is False
        assert status["output_dir"] == str(tmp_path / "nowhere")

    def test_a_corrupt_state_file_is_reported_not_raised(self, tmp_path: Path) -> None:
        """This runs while the campaign writes the file it is reading.

        A half-written or damaged state file must never let the monitor
        interfere with, or crash while watching, the thing it is watching.
        """
        (tmp_path / qualification.STATE_FILE_NAME).write_text(
            '{"config": {"output', encoding="utf-8"
        )
        status = qualification.load_status(tmp_path)
        assert status["exists"] is True
        assert status["readable"] is False
        assert status["error"]

    def test_a_lock_naming_a_live_process_means_the_orchestrator_is_running(
        self, tmp_path: Path
    ) -> None:
        """``run`` and ``resume`` both refuse to start a second campaign on this."""
        config = _config(tmp_path)
        qualification.QualificationState(
            tmp_path / qualification.STATE_FILE_NAME, config
        ).save()
        (tmp_path / qualification.LOCK_NAME).write_text(
            json.dumps({"pid": os.getpid()}), encoding="utf-8"
        )
        status = qualification.load_status(tmp_path)
        assert status["orchestrator_running"] is True
        assert status["orchestrator_pid"] == os.getpid()

    def test_a_lock_naming_a_dead_process_is_stale(self, tmp_path: Path) -> None:
        """A campaign whose orchestrator died is resumable, not running."""
        config = _config(tmp_path)
        qualification.QualificationState(
            tmp_path / qualification.STATE_FILE_NAME, config
        ).save()
        dead = _dead_pid()
        (tmp_path / qualification.LOCK_NAME).write_text(
            json.dumps({"pid": dead}), encoding="utf-8"
        )
        status = qualification.load_status(tmp_path)
        assert status["orchestrator_running"] is False
        assert status["orchestrator_pid"] == dead

    def test_a_readable_state_carries_the_paths_the_monitor_needs(
        self, tmp_path: Path
    ) -> None:
        """The GUI follows telemetry.csv and the summary without knowing their names."""
        config = _config(tmp_path)
        qualification.QualificationState(
            tmp_path / qualification.STATE_FILE_NAME, config
        ).save()
        status = qualification.load_status(tmp_path)
        assert status["readable"] is True
        assert status["orchestrator_running"] is False
        assert status["telemetry_csv"] == str(tmp_path / "telemetry.csv")
        assert status["summary_markdown"] == str(tmp_path / qualification.SUMMARY_MD_NAME)


# ----------------------------------------------------------------------
# CLI argument parsing
# ----------------------------------------------------------------------
class TestPercentList:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1,25,50", (1, 25, 50)),
            ("50,25,1", (1, 25, 50)),
            ("25,25,25", (25,)),
            (" 1 , 99 ", (1, 99)),
            ("1,,25", (1, 25)),
            ("7", (7,)),
        ],
    )
    def test_checkpoints_are_sorted_and_de_duplicated(
        self, text: str, expected: tuple[int, ...]
    ) -> None:
        """Each checkpoint gets its own project, so a repeat would run twice."""
        assert phase10_qualification._percent_list(text) == expected

    @pytest.mark.parametrize("text", ["0", "100", "abc", "-5", "101", "", " , ", "1.5"])
    def test_nonsense_checkpoints_are_rejected_before_the_campaign_starts(
        self, text: str
    ) -> None:
        """A kill at 0% has nothing committed to protect; one at 100% is not a kill.

        Rejected by the parser so the mistake costs a second rather than
        hours, and so a campaign can never record a checkpoint it could not
        have honoured.
        """
        with pytest.raises(qualification_argparse_error()):
            phase10_qualification._percent_list(text)


def qualification_argparse_error() -> type[Exception]:
    """Return the exception type ``_percent_list`` raises for bad input."""
    import argparse

    return argparse.ArgumentTypeError


class TestCommandLine:
    @pytest.mark.parametrize("command", ["preflight", "run", "resume", "status", "report"])
    def test_every_command_parses_with_its_required_arguments(
        self, command: str, tmp_path: Path
    ) -> None:
        """All five commands are documented as usable; none may be unreachable."""
        arguments = phase10_qualification.build_parser().parse_args(
            [command, "--output-dir", str(tmp_path)]
        )
        assert arguments.command == command
        assert arguments.output_dir == tmp_path

    @pytest.mark.parametrize("command", ["preflight", "run", "resume", "status", "report"])
    def test_a_missing_output_directory_is_an_error(self, command: str) -> None:
        """The output directory is the one thing a campaign cannot be given a default for."""
        with pytest.raises(SystemExit):
            phase10_qualification.build_parser().parse_args([command])

    def test_no_command_at_all_is_an_error(self) -> None:
        """A bare invocation must not silently do something."""
        with pytest.raises(SystemExit):
            phase10_qualification.build_parser().parse_args([])

    def test_run_accepts_restart(self, tmp_path: Path) -> None:
        """Starting over is deliberate and explicit, never the default."""
        arguments = phase10_qualification.build_parser().parse_args(
            ["run", "--output-dir", str(tmp_path), "--restart"]
        )
        assert arguments.restart is True

        without = phase10_qualification.build_parser().parse_args(
            ["run", "--output-dir", str(tmp_path)]
        )
        assert without.restart is False

    def test_run_defaults_to_the_release_campaign(self, tmp_path: Path) -> None:
        """Typing the command with no tuning must give the qualification itself."""
        arguments = phase10_qualification.build_parser().parse_args(
            ["run", "--output-dir", str(tmp_path)]
        )
        assert arguments.sheets == qualification.DEFAULT_SHEETS
        assert tuple(arguments.checkpoints) == qualification.DEFAULT_CHECKPOINTS
        assert arguments.mode == "full"
        assert arguments.seed == qualification.QUALIFICATION_SEED

    def test_status_against_a_missing_campaign_is_a_bad_argument(
        self, tmp_path: Path
    ) -> None:
        """Exit 2, not 1: nothing failed a release assertion, the path was wrong."""
        arguments = phase10_qualification.build_parser().parse_args(
            ["status", "--output-dir", str(tmp_path / "nowhere")]
        )
        assert (
            phase10_qualification._command_status(arguments)
            == phase10_qualification.EXIT_BAD_ARGS
        )

    def test_report_against_a_missing_state_file_is_a_bad_argument(
        self, tmp_path: Path
    ) -> None:
        """Rebuilding a report needs a campaign to rebuild it from."""
        arguments = phase10_qualification.build_parser().parse_args(
            ["report", "--output-dir", str(tmp_path / "nowhere")]
        )
        assert (
            phase10_qualification._command_report(arguments)
            == phase10_qualification.EXIT_BAD_ARGS
        )
