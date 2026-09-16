"""The repository-local `qtguitesting` Claude skill is intact.

Why this test exists:
    A skill is documentation plus scripts, and both rot silently: `SKILL.md` can
    point at a reference that was renamed, a script can import a page attribute
    that has been refactored away, and nothing notices until an agent follows the
    instructions and finds them wrong. Since the skill's whole purpose is to make
    GUI work reliable, a broken skill is worse than none.

    These checks are deliberately structural - files exist, scripts parse and
    expose their entry points, references are actually referenced. They do not
    run the GUI; `tests/gui/` does that.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPOSITORY_ROOT / ".claude" / "skills" / "qtguitesting"

EXPECTED_SCRIPTS = (
    "_harness.py",
    "capture_gui_states.py",
    "compare_gui_images.py",
    "dump_gui_geometry.py",
    "run_gui_smoke_tests.py",
)

EXPECTED_REFERENCES = (
    "omrflow_gui_test_scenarios.md",
    "qt_coordinate_systems.md",
)


def _skill_text() -> str:
    return (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")


class TestSkillLayout:
    def test_the_skill_directory_exists(self):
        assert SKILL_ROOT.is_dir(), f"Expected the skill at {SKILL_ROOT}"

    def test_it_is_repository_local_not_a_user_skill(self):
        assert SKILL_ROOT.is_relative_to(REPOSITORY_ROOT / ".claude")

    def test_skill_md_exists(self):
        assert (SKILL_ROOT / "SKILL.md").is_file()

    @pytest.mark.parametrize("name", EXPECTED_SCRIPTS)
    def test_each_script_exists(self, name: str):
        assert (SKILL_ROOT / "scripts" / name).is_file()

    @pytest.mark.parametrize("name", EXPECTED_REFERENCES)
    def test_each_reference_exists(self, name: str):
        assert (SKILL_ROOT / "references" / name).is_file()


class TestSkillFrontmatter:
    def test_it_declares_a_name_and_a_description(self):
        text = _skill_text()
        assert text.startswith("---\n")
        header = text.split("---", 2)[1]
        assert "name: qtguitesting" in header
        assert "description:" in header

    def test_the_description_says_when_to_use_it(self):
        header = _skill_text().split("---", 2)[1].lower()
        for cue in ("pyside6", "geometry", "screenshot"):
            assert cue in header, f"the description should mention {cue}"


class TestSkillContent:
    def test_skill_md_stays_short_enough_to_load_cheaply(self):
        """Detail belongs in `references/`; `SKILL.md` is the procedure."""
        lines = _skill_text().splitlines()
        assert len(lines) < 250, f"SKILL.md is {len(lines)} lines - move detail to references/"

    def test_the_references_are_the_longer_documents(self):
        skill_size = (SKILL_ROOT / "SKILL.md").stat().st_size
        reference_size = sum(
            (SKILL_ROOT / "references" / name).stat().st_size for name in EXPECTED_REFERENCES
        )
        assert reference_size > skill_size

    @pytest.mark.parametrize("name", EXPECTED_REFERENCES)
    def test_skill_md_points_at_each_reference(self, name: str):
        assert name in _skill_text()

    @pytest.mark.parametrize("name", EXPECTED_SCRIPTS)
    def test_skill_md_or_a_reference_mentions_each_runnable_script(self, name: str):
        if name.startswith("_"):
            pytest.skip("shared plumbing, not a command a user runs")
        corpus = _skill_text() + "".join(
            (SKILL_ROOT / "references" / reference).read_text(encoding="utf-8")
            for reference in EXPECTED_REFERENCES
        )
        assert name in corpus

    def test_it_states_that_screenshots_are_not_proof(self):
        """The one claim the skill must not lose - see the brief's §19."""
        text = _skill_text().lower()
        assert "screenshot similarity is not proof" in text

    def test_it_names_the_real_sample_image(self):
        assert "examples/ECE-0000.png" in _skill_text()

    def test_it_forbids_absolute_screen_coordinates(self):
        assert "absolute screen coordinates" in _skill_text().lower()


class TestReferencedPathsExist:
    """Every repository path the skill quotes must actually be there."""

    @pytest.mark.parametrize(
        "relative",
        [
            "examples/ECE-0000.png",
            "tests/gui/conftest.py",
            "docs/TESTING.md",
            "tests/gui/test_template_designer_region_geometry.py",
            "tests/unit/test_question_region_container.py",
            "tests/integration/test_orientation_marker_detection.py",
            "tests/gui/test_template_designer_canvas_panning.py",
            "tests/integration/test_template_service.py",
        ],
    )
    def test_a_path_the_skill_names_exists(self, relative: str):
        corpus = _skill_text() + "".join(
            (SKILL_ROOT / "references" / name).read_text(encoding="utf-8")
            for name in EXPECTED_REFERENCES
        )
        assert relative in corpus, f"{relative} is no longer mentioned by the skill"
        assert (REPOSITORY_ROOT / relative).exists(), f"{relative} does not exist"


class TestScriptsAreValidPython:
    @pytest.mark.parametrize("name", EXPECTED_SCRIPTS)
    def test_the_script_parses(self, name: str):
        path = SKILL_ROOT / "scripts" / name
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    @pytest.mark.parametrize("name", EXPECTED_SCRIPTS)
    def test_the_script_has_a_module_docstring(self, name: str):
        path = SKILL_ROOT / "scripts" / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert ast.get_docstring(tree), f"{name} has no module docstring"

    @pytest.mark.parametrize(
        "name",
        [n for n in EXPECTED_SCRIPTS if not n.startswith("_")],
    )
    def test_the_runnable_script_defines_main_and_a_guard(self, name: str):
        source = (SKILL_ROOT / "scripts" / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        assert "main" in functions, f"{name} has no main()"
        assert '__name__ == "__main__"' in source


class TestHarnessResolvesTheRepositoryRoot:
    def test_the_harness_finds_the_repository_without_a_working_directory(self):
        """The path anchor must be the file's own location, not the CWD.

        A relative ``open("examples/ECE-0000.png")`` works from the repository
        root and nowhere else; the skill's own rules forbid it, so the harness
        must not contain one.
        """
        source = (SKILL_ROOT / "scripts" / "_harness.py").read_text(encoding="utf-8")
        assert "Path(__file__).resolve().parents[" in source
        assert 'open("examples' not in source

    def test_the_computed_repository_root_is_this_repository(self):
        harness = SKILL_ROOT / "scripts" / "_harness.py"
        # The constant says parents[4]; verify that arithmetic against reality
        # rather than trusting the comment beside it.
        assert harness.resolve().parents[4] == REPOSITORY_ROOT


class TestGeneratedArtefactsAreIgnored:
    def test_test_output_is_gitignored(self):
        """Screenshots and dumps are regenerated, never committed baselines."""
        ignored = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "test-output" in ignored


class TestSampleAssetIsIntact:
    def test_the_sample_sheet_is_present_and_unmodified_in_size(self):
        """A guard against the sample being cropped, resized or replaced."""
        import cv2

        image = cv2.imread(str(REPOSITORY_ROOT / "examples" / "ECE-0000.png"))
        assert image is not None
        assert image.shape[:2] == (3508, 2480)

    def test_no_source_file_refers_to_the_sample_in_executable_code(self):
        """``src/`` must never *depend* on the sample image.

        Checked against string literals in real code, not against prose: a
        comment or docstring citing ``examples/ECE-0000.png`` as the real-world
        case that exposed a bug is useful documentation and breaks nothing. A
        string constant naming it is a runtime dependency on a test fixture.
        """
        offenders: list[str] = []
        for path in (REPOSITORY_ROOT / "src").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            docstrings = {
                id(ast.get_docstring(node, clean=False))
                for node in ast.walk(tree)
                if isinstance(
                    node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
                )
            }
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and "ECE-0000" in node.value
                    and id(node.value) not in docstrings
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
        assert not offenders, f"sample-specific string literals in {offenders}"

    def test_no_source_file_hard_codes_the_samples_measured_geometry(self):
        """The sample's own numbers must not leak into the algorithms.

        The orientation dash is at ``(158, 309, 86, 44)`` on a ``2480 x 3508``
        page. Those numbers belong in *tests*, as ground truth for an assertion -
        never in ``src/``, where they would be a detector that only works on one
        sheet.
        """
        forbidden = {158, 309, 2480, 3508}
        offenders: list[str] = []
        for path in (REPOSITORY_ROOT / "src").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, int)
                    and not isinstance(node.value, bool)
                    and node.value in forbidden
                ):
                    offenders.append(f"{path.name}:{node.lineno} -> {node.value}")
        assert not offenders, f"sample-specific constants in {offenders}"
