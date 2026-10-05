"""Unit tests for the revised phase 9 intake qualification harness.

Nothing here starts a writer or a coordinator process or kills anything: the
harness was written so that "did this campaign qualify?" is a function of
evidence already gathered (:mod:`omr_scanner.evaluation.intake_qualification.assertions`),
and these tests hand that function synthetic evidence directly - plus the
plan, the reference computation, the evidence logs, the report and the CLI.

The tests that matter most guard the verdict: every registered assertion is
always emitted; a failure, an unexercised case or a missing assertion can never
end in a passing verdict; a small campaign can never be ``QUALIFIED``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omr_scanner.evaluation.intake_qualification import (
    assertions,
    cohort,
    config,
    evidence,
    reference,
    report,
)
from omr_scanner.evaluation.intake_qualification.assertions import CheckResult
from omr_scanner.services.template_service import load_template
from omr_scanner.tools import intake_qualification as cli

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "examples" / "templates" / "synthetic_answer_sheet.omrt"

SECTION_5_2 = [
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
]
"""ACCEPTANCE_CRITERIA.md §5.2, copied here on purpose: the registry must match it."""


@pytest.fixture(scope="module")
def template():
    return load_template(TEMPLATE)


@pytest.fixture(scope="module")
def plan(template):
    return cohort.plan_campaign(config.self_test_config(), template)


def passing(name: str) -> CheckResult:
    item = CheckResult(name, checked=5, minimum=1)
    return item.settle()


def all_passing(names: list[str] | tuple[str, ...]) -> list[CheckResult]:
    return [passing(name) for name in names]


RELEASE_COUNTERS: dict[str, Any] = {
    "sources": 3, "arrivals_written": 10_188, "sets": 4, "real_images": True,
    **dict.fromkeys(assertions.REQUIRED_FEATURES, 1),
}


# ----------------------------------------------------------------------
# The registry
# ----------------------------------------------------------------------
class TestRegistry:
    def test_the_registry_is_exactly_the_acceptance_list(self):
        assert list(assertions.REQUIRED_ASSERTIONS) == SECTION_5_2
        assert len(set(assertions.REQUIRED_ASSERTIONS)) == 16

    def test_crash_matrix_and_endurance_ids_are_stable_and_complete(self):
        ids = [case_id for case_id, _title in assertions.CRASH_CASES]
        assert len(ids) == 15 and len(set(ids)) == 15
        assert [int(case_id.split("_")[1]) for case_id in ids] == list(range(1, 16))
        assert [case_id for case_id, _ in assertions.ENDURANCE_CASES] == [
            "endurance_a_many_finite_batches",
            "endurance_b_continuous_random_intake",
            "endurance_c_supersession_reprocess",
            "endurance_d_repeated_kill_restart",
            "endurance_e_uninterrupted_control",
        ]

    def test_the_evaluator_emits_every_assertion_even_when_evaluation_breaks(self, plan):
        broken = SimpleNamespace(writer_events=[], coordinator_events=[], final_facts=None)
        results = assertions.evaluate_assertions(
            plan, broken, None, None, None, sha_to_content={}, template_path=TEMPLATE,
            share_root=Path(), final_checkpoint={"label": "final", "aggregate": {"ok": False}},
        )
        assert [item.name for item in results] == SECTION_5_2
        assert all(item.status == assertions.FAIL for item in results)
        assert all(item.failures for item in results)


# ----------------------------------------------------------------------
# One result: never a vacuous pass
# ----------------------------------------------------------------------
class TestCheckResult:
    def test_unexercised_is_not_a_pass(self):
        item = CheckResult("x", checked=0, minimum=1).settle()
        assert item.status == assertions.NOT_EXERCISED and not item.passed

    def test_a_failure_is_a_failure_whatever_was_checked(self):
        item = CheckResult("x", checked=1000, minimum=1)
        item.fail("one mismatch")
        assert item.settle().status == assertions.FAIL

    def test_failures_are_counted_beyond_the_listed_ones(self):
        item = CheckResult("x", checked=1)
        for number in range(assertions.MAX_LISTED + 7):
            item.fail(f"m{number}")
        assert len(item.failures) == assertions.MAX_LISTED
        assert item.failure_count == assertions.MAX_LISTED + 7


# ----------------------------------------------------------------------
# The verdict
# ----------------------------------------------------------------------
class TestVerdict:
    def scale(self, mode: str, **counters: Any) -> dict[str, Any]:
        values = {**RELEASE_COUNTERS, **counters}
        return assertions.scale_check(mode, values, all_passing(SECTION_5_2))

    def crash(self) -> list[CheckResult]:
        return all_passing([case for case, _ in assertions.CRASH_CASES])

    def endurance(self) -> list[CheckResult]:
        return all_passing([case for case, _ in assertions.ENDURANCE_CASES])

    def test_full_scale_and_everything_passing_qualifies(self):
        verdict = assertions.verdict(all_passing(SECTION_5_2), self.crash(), self.endurance(),
                                     self.scale("release"), completed=True)
        assert verdict == config.VERDICT_QUALIFIED

    def test_a_small_campaign_never_qualifies(self):
        verdict = assertions.verdict(all_passing(SECTION_5_2), self.crash(), self.endurance(),
                                     self.scale("self_test"), completed=True)
        assert verdict == config.VERDICT_SMALL

    @pytest.mark.parametrize("counters", [
        {"arrivals_written": 9_999}, {"sources": 2}, {"sets": 1}, {"real_images": False},
        {"source_outages": 0}, {"kills": 0}, {"replacement_chains": 0},
        {"dismissed_suggestions": 0},
    ])
    def test_release_mode_below_any_measured_requirement_does_not_qualify(self, counters):
        scale = self.scale("release", **counters)
        assert not scale["release_scale"] and scale["reasons"]
        verdict = assertions.verdict(all_passing(SECTION_5_2), self.crash(), self.endurance(),
                                     scale, completed=True)
        assert verdict == config.VERDICT_SMALL

    def test_a_planted_assertion_failure_fails_the_campaign(self):
        results = all_passing(SECTION_5_2)
        results[7].fail("a committed sheet was read again")
        results[7].settle()
        verdict = assertions.verdict(results, self.crash(), self.endurance(),
                                     self.scale("release"), completed=True)
        assert verdict == config.VERDICT_FAILED

    def test_an_unexercised_assertion_fails_the_campaign(self):
        results = all_passing(SECTION_5_2)
        results[8] = CheckResult(results[8].name, checked=0, minimum=1).settle()
        assert assertions.verdict(results, self.crash(), self.endurance(),
                                  self.scale("release"), completed=True) == config.VERDICT_FAILED

    def test_a_missing_assertion_fails_the_campaign(self):
        results = all_passing(SECTION_5_2[:-1])
        assert assertions.verdict(results, self.crash(), self.endurance(),
                                  self.scale("release"), completed=True) == config.VERDICT_FAILED

    def test_a_failed_crash_case_fails_the_campaign(self):
        crash = self.crash()
        crash[12].fail("no duplicate conflict was owed")
        crash[12].settle()
        assert assertions.verdict(all_passing(SECTION_5_2), crash, self.endurance(),
                                  self.scale("release"), completed=True) == config.VERDICT_FAILED

    def test_an_incomplete_campaign_never_passes(self):
        assert assertions.verdict(all_passing(SECTION_5_2), self.crash(), self.endurance(),
                                  self.scale("release"), completed=False) == config.VERDICT_FAILED

    def test_only_the_reprocess_endurance_case_may_be_left_to_the_stress_suite(self):
        endurance = self.endurance()
        endurance[2] = CheckResult(endurance[2].name, checked=0).settle()
        assert assertions.verdict(all_passing(SECTION_5_2), self.crash(), endurance,
                                  self.scale("release"), completed=True) == config.VERDICT_QUALIFIED
        endurance[3] = CheckResult(endurance[3].name, checked=0).settle()
        assert assertions.verdict(all_passing(SECTION_5_2), self.crash(), endurance,
                                  self.scale("release"), completed=True) == config.VERDICT_FAILED


# ----------------------------------------------------------------------
# The plan
# ----------------------------------------------------------------------
class TestPlan:
    def test_the_plan_is_deterministic_in_its_seeds(self, template, plan):
        again = cohort.plan_campaign(config.self_test_config(), template)
        assert again.digest() == plan.digest()
        other = cohort.plan_campaign(config.self_test_config(seed=7), template)
        assert other.digest() != plan.digest()
        retimed = cohort.plan_campaign(config.self_test_config(timing_seed=9), template)
        assert [c.key for c in retimed.contents] == [c.key for c in plan.contents]
        assert [a.at for a in retimed.arrivals] != [a.at for a in plan.arrivals]

    def test_the_self_test_plants_every_case(self, plan):
        categories = {item.category for item in plan.candidates}
        assert set(cohort.Category) <= categories
        kinds = {item.kind for item in plan.contents}
        assert set(cohort.ContentKind) <= kinds
        patterns = {item.pattern for item in plan.arrivals}
        assert set(cohort.WritePattern) <= patterns
        groups = reference.duplicate_groups(plan)
        sources = [{source for source, _name in places} for places in groups.values()]
        assert any(len(item) == 1 for item in sources), "a copy within one source"
        assert any(len(item) > 1 for item in sources), "a copy across sources"
        names = {}
        for item in plan.main_arrivals:
            names.setdefault(item.name, set()).add(item.source)
        assert any(len(item) == 3 for item in names.values())
        assert len(plan.late_arrivals) == 1

    def test_some_decisions_are_left_for_the_endgame(self, plan):
        final = {task.kind for task in plan.tasks if task.when is cohort.When.FINAL}
        assert cohort.TaskKind.CORRECT_ID in final or cohort.TaskKind.ACCEPT_DUPLICATE in final

    def test_a_release_plan_writes_at_least_ten_thousand_files(self, template):
        plan = cohort.plan_campaign(config.release_config(), template)
        assert len(plan.main_arrivals) >= config.RELEASE_MIN_ARRIVALS
        assert len({a.source for a in plan.main_arrivals}) >= 3

    def test_a_release_plan_below_scale_is_refused(self, template):
        with pytest.raises(ValueError, match="at least 10,000"):
            cohort.plan_campaign(config.release_config(candidates_per_set=500), template)

    def test_retiming_keeps_the_cohort_and_the_order(self, plan):
        retimed = cohort.retime(plan, 99, 20.0)
        assert [a.content for a in retimed.arrivals] == [a.content for a in plan.arrivals]
        order = sorted(plan.main_arrivals, key=lambda a: (a.at, a.seq))
        reorder = sorted(retimed.main_arrivals, key=lambda a: (a.at, a.seq))
        assert [a.seq for a in order] == [a.seq for a in reorder]
        assert max(a.at for a in retimed.main_arrivals) <= 20.0


class TestConfig:
    def test_round_trip(self):
        original = config.release_config(workers=3)
        assert config.CampaignConfig.from_json(json.loads(original.dumps())) == original

    def test_an_unknown_key_is_refused(self):
        data = config.self_test_config().to_json()
        data["kill_everything"] = True
        with pytest.raises(ValueError, match="unknown"):
            config.CampaignConfig.from_json(data)


# ----------------------------------------------------------------------
# The reference
# ----------------------------------------------------------------------
class TestReferenceScore:
    def test_every_rule(self, plan):
        code = plan.config.sets[1]  # has a withdrawn question
        key = plan.keys[code]
        withdrawn = set(plan.scoring.wrong_questions[code])
        assert withdrawn == {7}
        answers = list(key)
        answers[0] = ""  # blank: 0
        answers[1] = "A-B"  # multiple: -1/4
        answers[2] = next(label for label in "ABCD" if label != key[2])  # wrong: -1/4
        answers[6] = ""  # withdrawn question: full credit even when blank
        mark = reference.reference_score(plan, code, tuple(answers))
        expected = Fraction(20 - 3 - 1) - Fraction(2, 4) + Fraction(1)  # 16 right + withdrawn
        assert mark.final == expected
        assert (mark.blank, mark.multiple, mark.incorrect, mark.wrong_question) == (1, 1, 1, 1)

    def test_the_total_is_clamped_once(self, plan):
        code = plan.config.sets[0]
        key = plan.keys[code]
        wrong = tuple(next(label for label in "ABCD" if label != item) for item in key)
        mark = reference.reference_score(plan, code, wrong)
        assert mark.raw == -Fraction(20, 4) and mark.final == 0

    def test_ranks_tie_as_rank_eq_does(self, plan):
        results = reference.expected_results(plan, plan.config.sets[0], "reopen")
        scored = [item for item in results if not item.absent]
        for item in scored:
            assert item.rank == 1 + sum(1 for other in scored if other.mark.final > item.mark.final)

    def test_the_merit_formula_is_evaluated_like_excel(self):
        formula = ('=IF(OR(UPPER(TRIM(D5))="ABSENT",UPPER(TRIM(D5))="ABS"),"---",'
                   'IF(ISNUMBER(D5),RANK.EQ(D5,$D$4:$D$8,0),""))')
        column = {4: 10.0, 5: 7.5, 6: "ABSENT", 7: 10.0, 8: 3}
        assert reference.evaluate_rank(formula, column) == 3
        assert reference.evaluate_rank(formula.replace("D5", "D6"), column) is None

    def test_list_corrections_apply_in_phase_order(self, plan):
        late = next(item for item in plan.candidates if item.category is cohort.Category.LATE_FOUND)
        assert reference.attendance_list(plan, late.set_code, "first_close")[late.roll] is True
        assert reference.attendance_list(plan, late.set_code, "reopen")[late.roll] is False
        before = reference.expected_statuses_before(plan, late.set_code, "first_close")
        assert before[late.roll] == "present_without_script"


class TestConflictReference:
    def test_a_wrong_id_raises_a_duplicate_until_corrected(self, plan):
        writer = next(c for c in plan.contents if c.id_defect is cohort.IdDefect.WRONG)
        victim = next(c for c in plan.contents if c.kind is cohort.ContentKind.SCRIPT
                      and c.candidate == writer.bubbled_roll)
        facts = reference.CommittedFacts(
            sheets={1: writer.key, 2: victim.key}, corrected=frozenset(),
            accepted_duplicate=frozenset(), acknowledged=frozenset(), rejected=frozenset(),
        )
        assert reference.expected_open_conflicts(plan, facts) == {
            (writer.key, "identifier_duplicate"), (victim.key, "identifier_duplicate")}
        corrected = replace(facts, corrected=frozenset({1}))
        assert reference.expected_open_conflicts(plan, corrected) == set()
        alone = replace(facts, sheets={1: writer.key})
        assert reference.expected_open_conflicts(plan, alone) == set()

    def test_sheet_conflicts_close_when_decided(self, plan):
        blank = next(c for c in plan.contents if c.id_defect is cohort.IdDefect.BLANK)
        page = next(c for c in plan.contents if c.kind is cohort.ContentKind.BLANK_PAGE)
        facts = reference.CommittedFacts(
            sheets={1: blank.key, 2: page.key}, corrected=frozenset(),
            accepted_duplicate=frozenset(), acknowledged=frozenset(), rejected=frozenset(),
        )
        assert reference.expected_open_conflicts(plan, facts) == {
            (blank.key, "identifier_blank"), (page.key, "registration_failed")}
        decided = replace(facts, corrected=frozenset({1}), acknowledged=frozenset({2}))
        assert reference.expected_open_conflicts(plan, decided) == set()
        rejected = replace(facts, rejected=frozenset({1, 2}))
        assert reference.expected_open_conflicts(plan, rejected) == set()


# ----------------------------------------------------------------------
# Recovery comparison (synthetic facts)
# ----------------------------------------------------------------------
def facts_with(**changes: Any) -> Any:
    from omr_scanner.evaluation.intake_qualification.inspect import (
        AuditRow,
        BatchRow,
        ConflictRow,
        Facts,
        SheetRow,
    )

    def sheet(scan: int, status: str, attempts: int = 1) -> SheetRow:
        return SheetRow(scan, "b1", status, f"sha{scan}", scan, attempts, "100001", "A",
                        f"{scan}.png", "", "complete")

    statuses = changes.pop("statuses", {1: "completed", 2: "processing", 3: "pending"})
    facts = Facts(session_id="S")
    facts.sessions = [("S", "open", 0)]
    facts.sheets = {scan: sheet(scan, status) for scan, status in statuses.items()}
    facts.batches = {"b1": BatchRow("b1", "S", "scan", "running", True, "src", len(statuses),
                                    tuple(sorted(statuses)), None)}
    facts.conflicts = [ConflictRow(10, 1, "b1", "identifier_blank", "resolved", "roll", -1)]
    facts.audit = [AuditRow(1, "detected", "", 1, 10, "conflict", "10", "b1"),
                   AuditRow(2, "corrected", config.OPERATOR, 1, 10, "conflict", "10", "b1")]
    for key, value in changes.items():
        setattr(facts, key, value)
    return facts


class TestCompareRecovery:
    def test_a_clean_recovery(self):
        down = facts_with()
        up = facts_with(statuses={1: "completed", 2: "pending", 3: "pending"})
        result = assertions.compare_recovery(down, up)
        assert result["ok"], result["failures"]
        assert result["returned"] == 1 and result["in_flight_at_kill"] == 1

    def test_a_committed_sheet_read_again_is_caught(self):
        down = facts_with()
        up = facts_with(statuses={1: "pending", 2: "pending", 3: "pending"})
        assert any("committed sheet 1" in item
                   for item in assertions.compare_recovery(down, up)["failures"])

    def test_an_in_flight_sheet_marked_failed_is_caught(self):
        down = facts_with()
        up = facts_with(statuses={1: "completed", 2: "failed", 3: "pending"})
        assert any("not returned" in item
                   for item in assertions.compare_recovery(down, up)["failures"])

    def test_a_new_session_or_batch_is_caught(self):
        from omr_scanner.evaluation.intake_qualification.inspect import BatchRow

        down = facts_with()
        up = facts_with(statuses={1: "completed", 2: "pending", 3: "pending"})
        up.batches["b2"] = BatchRow("b2", "S", "scan", "new", True, "src", 0, (), None)
        up.sessions = [("S", "open", 0), ("T", "open", 0)]
        failures = assertions.compare_recovery(down, up)["failures"]
        assert any("created batch" in item for item in failures)
        assert any("sessions changed" in item for item in failures)

    def test_rewritten_or_added_operator_history_is_caught(self):
        from omr_scanner.evaluation.intake_qualification.inspect import AuditRow

        down = facts_with()
        up = facts_with(statuses={1: "completed", 2: "pending", 3: "pending"})
        up.audit = [*up.audit, AuditRow(3, "corrected", config.OPERATOR, 1, 10, "c", "10", "b1")]
        assert any("operator audit" in item
                   for item in assertions.compare_recovery(down, up)["failures"])
        up.audit = up.audit[1:]
        assert any("prefix" in item for item in assertions.compare_recovery(down, up)["failures"])


# ----------------------------------------------------------------------
# Integrity and health evidence
# ----------------------------------------------------------------------
class TestIntegrityEvidence:
    def run(self, report_: dict[str, Any]) -> Any:
        return SimpleNamespace(name="r", kills=[], final_integrity=report_)

    def test_clean(self):
        clean = {"quick_check": ["ok"], "integrity_check": ["ok"], "foreign_key_check": [],
                 "health_ok": True, "health_issues": []}
        assert assertions.a_sqlite([self.run(clean)], None).passed
        assert assertions.a_health([self.run(clean)], None).passed

    def test_a_corrupt_database_fails(self):
        corrupt = {"quick_check": ["*** in database main ***"], "integrity_check": ["ok"],
                   "foreign_key_check": [["t", 1, "p", 0]], "health_issues": []}
        result = assertions.a_sqlite([self.run(corrupt)], None)
        assert result.status == assertions.FAIL and len(result.failures) == 2

    def test_a_health_error_fails_and_a_warning_is_recorded(self):
        issues = {"quick_check": ["ok"], "integrity_check": ["ok"], "foreign_key_check": [],
                  "health_ok": False, "health_issues": [
                      {"level": "error", "code": "X", "message": "broken"},
                      {"level": "warning", "code": "W", "message": "odd"}]}
        result = assertions.a_health([self.run(issues)], None)
        assert result.status == assertions.FAIL
        assert result.evidence["warnings"] == {"W": 1}


# ----------------------------------------------------------------------
# Evidence logs
# ----------------------------------------------------------------------
class TestEvidenceLogs:
    def test_a_torn_last_line_is_skipped_and_read_later(self, tmp_path):
        path = tmp_path / "log.jsonl"
        log = evidence.EvidenceLog(path, campaign_id="C", role="test")
        log.write("one")
        tail = evidence.LogTail(path, campaign_id="C")
        assert [item["event"] for item in tail.read_new()] == ["one"]
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"event": "two", "campaign": "C"')
        assert tail.read_new() == []
        with path.open("a", encoding="utf-8") as handle:
            handle.write("}\n")
        assert [item["event"] for item in tail.read_new()] == ["two"]
        log.close()

    def test_stale_evidence_of_another_campaign_is_refused(self, tmp_path):
        path = tmp_path / "log.jsonl"
        log = evidence.EvidenceLog(path, campaign_id="OLD", role="test")
        log.write("x")
        log.close()
        with pytest.raises(evidence.StaleEvidenceError):
            evidence.read_log(path, campaign_id="NEW")
        with pytest.raises(evidence.StaleEvidenceError):
            evidence.LogTail(path, campaign_id="NEW").read_new()


# ----------------------------------------------------------------------
# Reports and the command line
# ----------------------------------------------------------------------
def payload(verdict: str) -> dict[str, Any]:
    return {
        "schema_version": assertions.REPORT_SCHEMA_VERSION,
        "campaign_id": "p9-test",
        "mode": "self_test",
        "verdict": verdict,
        "assertions": [item.to_json() for item in all_passing(SECTION_5_2)],
        "crash_matrix": [], "endurance": [], "scale": {"release_scale": False, "reasons": ["x"]},
        "environment": {"commit": "abc"}, "error": {}, "not_proven": ["SMB"], "proves": "p",
    }


class TestReports:
    def test_the_json_and_markdown_name_every_assertion(self, tmp_path):
        json_path, md_path = report.write_reports(tmp_path, payload(config.VERDICT_SMALL))
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["schema_version"] == 1
        assert [item["name"] for item in data["assertions"]] == SECTION_5_2
        assert set(data["assertions"][0]) >= {"name", "status", "checked", "minimum",
                                              "failures", "evidence"}
        text = md_path.read_text(encoding="utf-8")
        for name in SECTION_5_2:
            assert f"`{name}`" in text
        assert config.VERDICT_SMALL in text and "does **not** prove" in text


class TestCommandLine:
    @pytest.fixture
    def fake_run(self, monkeypatch, tmp_path):
        state: dict[str, Any] = {}

        def run_campaign(
            config_, output, *, template_path, progress
        ) -> tuple[Path, dict[str, Any]]:
            state["config"] = config_
            return tmp_path, payload(state["verdict"])

        monkeypatch.setattr(
            "omr_scanner.evaluation.intake_qualification.campaign.run_campaign", run_campaign
        )
        return state

    def test_a_mode_is_required(self):
        with pytest.raises(SystemExit) as raised:
            cli.main(["--output", "x"])
        assert raised.value.code == 2

    @pytest.mark.parametrize(("flag", "verdict", "code"), [
        ("--self-test", config.VERDICT_SMALL, 0),
        ("--self-test", config.VERDICT_FAILED, 1),
        ("--release-scale", config.VERDICT_QUALIFIED, 0),
        ("--release-scale", config.VERDICT_SMALL, 3),
        ("--release-scale", config.VERDICT_FAILED, 1),
    ])
    def test_exit_codes(self, fake_run, flag, verdict, code, tmp_path):
        fake_run["verdict"] = verdict
        assert cli.main([flag, "--output", str(tmp_path), "--template", str(TEMPLATE)]) == code
        mode = fake_run["config"].mode
        assert mode is (config.Mode.RELEASE if flag == "--release-scale" else config.Mode.SELF_TEST)

    def test_a_custom_configuration_is_never_the_release_mode(self, fake_run, tmp_path):
        fake_run["verdict"] = config.VERDICT_SMALL
        custom = tmp_path / "c.json"
        custom.write_text(config.release_config().dumps(), encoding="utf-8")
        assert cli.main(["--config", str(custom), "--output", str(tmp_path),
                         "--template", str(TEMPLATE)]) == 0
        assert fake_run["config"].mode is config.Mode.CUSTOM

    def test_a_malformed_configuration_is_refused(self, tmp_path):
        bad = tmp_path / "c.json"
        bad.write_text('{"kill_everything": true}', encoding="utf-8")
        assert cli.main(["--config", str(bad), "--output", str(tmp_path),
                         "--template", str(TEMPLATE)]) == 2


def test_the_qualification_template_is_the_crash_harness_sheet(template):
    """The committed template must stay the geometry the expectations were measured on."""
    from tests.crash.harness import template as harness_template

    from omr_scanner.services import batch_store

    assert (batch_store.BatchIdentity.of(template).geometry_fingerprint
            == batch_store.BatchIdentity.of(harness_template()).geometry_fingerprint)
