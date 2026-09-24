"""Tests for the deterministic release script.

Scope:
    ``scripts/release.py`` - the tool that prepares a release without a
    person, or a model, making any of the decisions. What is asserted here is
    mostly refusal: the script's value is that it stops, and every check that
    stops it is a check somebody would otherwise have to remember.

    ===== ==========================================================
    Test  Behaviour
    ===== ==========================================================
    A     Version parsing accepts this project's spellings and no others.
    B     Versions order correctly, including across prerelease stages.
    C     The current version is read from the authoritative file.
    D     The metadata plan edits what it claims and nothing else.
    E     The repository preflight refuses an unsafe release.
    F     The command line, including --current-version.
    G     There is no way around any of the checks.
    H     The PowerShell entry point: help, and the no-argument case.
    I     The release workflow publishes only after every gate.
    ===== ==========================================================

Why these use real temporary Git repositories:
    The checks under test are almost all statements about Git state - which
    branch, how dirty, how far ahead, which tags exist. Mocking `git` would
    assert that the script calls the functions this file expects it to call,
    which is not the same thing as asserting it is safe. A temporary
    repository is cheap and tests the actual behaviour.

Why nothing here touches the network:
    Every repository is local, and the "remote" is a bare repository in the
    same temporary directory. No test pushes anything anywhere real, and no
    test creates a tag in the OMRFlow checkout it is running from.
"""

from __future__ import annotations

import itertools
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import release  # noqa: E402  - importable only after the path insert above

RELEASE_SCRIPT = REPOSITORY_ROOT / "scripts" / "release.py"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def run_git(repo: Path, *args: str) -> str:
    """Run Git in ``repo``, failing the test if it fails."""
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def make_repository(tmp_path: Path, version: str = "0.1.0-alpha.2") -> Path:
    """A miniature OMRFlow checkout with a bare 'origin' behind it.

    Carries only what the release script reads: the version module, a
    CITATION file and a README shaped like the real one. That is enough for
    every check under test, and it keeps each test's repository small.
    """
    work = tmp_path / "work"
    (work / "src" / "omr_scanner").mkdir(parents=True)
    (work / release.VERSION_FILE).write_text(
        '"""Version."""\n\nfrom typing import Final\n\n'
        f'__version__: Final = "{version}"\n',
        encoding="utf-8",
    )
    (work / "CITATION.cff").write_text(
        f"cff-version: 1.2.0\ntitle: OMRFlow\nversion: {version}\n"
        "date-released: '2026-01-01'\n",
        encoding="utf-8",
    )
    badge = version.replace("-", "--")
    (work / "README.md").write_text(
        f"[![Release](https://img.shields.io/badge/release-{badge}-AC1F24)](x)\n"
        f"Get `OMRFlow-{version}-Setup-x64.exe` from the releases page.\n"
        f"**Current release: `{version}`**\n",
        encoding="utf-8",
    )
    (work / "CHANGELOG.md").write_text(f"# Changelog\n\n## [{version}] - 2026-01-01\n", "utf-8")

    # scripts/release.py resolves the repository from its own location, so a
    # test repository needs its own copy: running the checkout's copy would
    # make every assertion about the real OMRFlow tree instead.
    (work / "scripts").mkdir()
    (work / "scripts" / "release.py").write_bytes(RELEASE_SCRIPT.read_bytes())

    origin = tmp_path / "origin.git"
    # --initial-branch=main on the bare repository too: its HEAD otherwise
    # points at 'master', and a clone of it silently checks nothing out.
    run_git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(origin))
    run_git(tmp_path, "init", "--quiet", "--initial-branch=main", str(work))
    run_git(work, "config", "user.email", "test@example.invalid")
    run_git(work, "config", "user.name", "Test")
    run_git(work, "add", "-A")
    run_git(work, "commit", "--quiet", "-m", "initial")
    run_git(work, "remote", "add", "origin", str(origin))
    run_git(work, "push", "--quiet", "-u", "origin", "main")
    return work


# ----------------------------------------------------------------------
# A - parsing
# ----------------------------------------------------------------------
class TestAVersionParsing:
    @pytest.mark.parametrize(
        "text", ["0.1.0", "0.1.0-alpha.1", "0.1.0-beta.2", "0.1.0-rc.3", "1.2.3", "0.1.10"]
    )
    def test_the_spellings_this_project_releases_are_accepted(self, text: str):
        assert str(release.parse_version(text)) == text

    @pytest.mark.parametrize(
        "text",
        [
            "0.1",  # not three components
            "alpha.1",  # no release number
            "0.1.0-alpha",  # a stage with no number
            "0.1.0-alpha.x",  # a stage with a non-number
            "0.1.0-dev.1",  # a stage this project does not use
            "0.1.0-alpha1",  # the PEP 440 spelling, not this project's
            "",
            "0.1.0.1",
            "01.1.0",  # a leading zero is not a version component
        ],
    )
    def test_anything_else_is_refused(self, text: str):
        with pytest.raises(release.ReleaseError, match="not a valid OMRFlow version"):
            release.parse_version(text)

    def test_a_leading_v_is_accepted_and_dropped(self):
        """Typing the tag instead of the version is the obvious slip."""
        assert str(release.parse_version("v0.1.0-alpha.3")) == "0.1.0-alpha.3"

    def test_the_tag_is_the_version_with_a_v(self):
        assert release.parse_version("0.1.0-alpha.3").tag == "v0.1.0-alpha.3"

    @pytest.mark.parametrize(
        ("text", "prerelease"),
        [
            ("0.1.0-alpha.1", True),
            ("0.1.0-beta.1", True),
            ("0.1.0-rc.1", True),
            ("0.1.0", False),
            ("1.0.0", False),
        ],
    )
    def test_only_a_final_release_is_not_a_prerelease(self, text: str, prerelease: bool):
        """What decides the GitHub pre-release flag."""
        assert release.parse_version(text).is_prerelease is prerelease


# ----------------------------------------------------------------------
# B - ordering
# ----------------------------------------------------------------------
class TestBVersionOrdering:
    def test_the_release_order_is_the_sort_order(self):
        """The whole ordering, end to end.

        Lexically, 'rc' sorts before 'alpha' and '0.1.0' before its own
        prereleases. Both are wrong, and both would let a release go
        backwards.
        """
        ordered = [
            "0.1.0-alpha.1",
            "0.1.0-alpha.2",
            "0.1.0-beta.1",
            "0.1.0-rc.1",
            "0.1.0",
            "0.1.1",
            "0.2.0",
            "1.0.0",
        ]
        versions = [release.parse_version(text) for text in ordered]
        assert sorted(versions, key=lambda v: v.sort_key) == versions
        for earlier, later in itertools.pairwise(versions):
            assert earlier < later
            assert later > earlier

    def test_alpha_10_comes_after_alpha_9(self):
        """The string comparison that made this worth testing."""
        assert release.parse_version("0.1.0-alpha.9") < release.parse_version(
            "0.1.0-alpha.10"
        )

    def test_the_same_version_is_neither_earlier_nor_later(self):
        one = release.parse_version("0.1.0-alpha.2")
        two = release.parse_version("0.1.0-alpha.2")
        assert one == two
        assert not one < two
        assert not one > two


# ----------------------------------------------------------------------
# C - current version
# ----------------------------------------------------------------------
class TestCCurrentVersion:
    def test_it_is_read_from_the_authoritative_file(self, tmp_path: Path):
        repo = make_repository(tmp_path, "0.4.2-beta.7")
        assert str(release.get_current_version(repo)) == "0.4.2-beta.7"

    def test_reading_it_changes_nothing(self, tmp_path: Path):
        """A query must be a query. release.ps1 calls this on every run."""
        repo = make_repository(tmp_path)
        before = {
            path: path.read_bytes() for path in sorted(repo.rglob("*")) if path.is_file()
        }
        release.get_current_version(repo)
        after = {
            path: path.read_bytes() for path in sorted(repo.rglob("*")) if path.is_file()
        }
        assert after == before
        assert run_git(repo, "status", "--porcelain") == ""

    def test_the_real_repository_reports_its_own_version(self):
        """Against the actual checkout, not a fixture."""
        current = release.get_current_version(REPOSITORY_ROOT)
        from omr_scanner._version import __version__

        assert str(current) == __version__

    def test_a_missing_file_is_an_error(self, tmp_path: Path):
        with pytest.raises(release.ReleaseError, match="does not exist"):
            release.get_current_version(tmp_path)

    def test_a_file_with_no_version_is_an_error(self, tmp_path: Path):
        repo = make_repository(tmp_path)
        (repo / release.VERSION_FILE).write_text('"""Nothing here."""\n', encoding="utf-8")
        with pytest.raises(release.ReleaseError, match="No __version__ assignment"):
            release.get_current_version(repo)

    def test_two_declarations_are_an_error(self, tmp_path: Path):
        """Two declarations are the defect, not a tie to break.

        Whichever one this script picked, something else would read the other.
        """
        repo = make_repository(tmp_path)
        (repo / release.VERSION_FILE).write_text(
            '__version__ = "0.1.0"\n__version__ = "0.2.0"\n', encoding="utf-8"
        )
        with pytest.raises(release.ReleaseError, match="assigns __version__ 2 times"):
            release.get_current_version(repo)

    def test_a_computed_version_is_an_error(self, tmp_path: Path):
        """A computed version is refused.

        It cannot be read without executing the module, and reading a release
        script's input must never run it.
        """
        repo = make_repository(tmp_path)
        (repo / release.VERSION_FILE).write_text(
            '__version__ = "0.1.0" + ".dev"\n', encoding="utf-8"
        )
        with pytest.raises(release.ReleaseError, match="not a plain string literal"):
            release.get_current_version(repo)

    def test_a_malformed_version_is_an_error(self, tmp_path: Path):
        repo = make_repository(tmp_path)
        (repo / release.VERSION_FILE).write_text('__version__ = "not-a-version"\n', "utf-8")
        with pytest.raises(release.ReleaseError, match="not a valid OMRFlow version"):
            release.get_current_version(repo)

    def test_unparsable_python_is_an_error(self, tmp_path: Path):
        repo = make_repository(tmp_path)
        (repo / release.VERSION_FILE).write_text("def (\n", encoding="utf-8")
        with pytest.raises(release.ReleaseError, match="Cannot read the current version"):
            release.get_current_version(repo)


# ----------------------------------------------------------------------
# D - metadata
# ----------------------------------------------------------------------
class TestDMetadataUpdates:
    def test_it_updates_every_intentional_duplicate(self, tmp_path: Path):
        repo = make_repository(tmp_path, "0.1.0-alpha.2")
        target = release.parse_version("0.1.0-alpha.3")
        plan = release.plan_metadata_updates(
            repo, release.get_current_version(repo), target, date(2026, 9, 24)
        )
        release.apply_replacements(repo, plan)

        assert str(release.get_current_version(repo)) == "0.1.0-alpha.3"
        citation = (repo / "CITATION.cff").read_text(encoding="utf-8")
        assert "version: 0.1.0-alpha.3" in citation
        assert "date-released: '2026-09-24'" in citation
        readme = (repo / "README.md").read_text(encoding="utf-8")
        # shields.io escapes a hyphen as '--', so the badge carries a
        # different spelling of the same version and needs its own edit.
        assert "badge/release-0.1.0--alpha.3-AC1F24" in readme
        assert "OMRFlow-0.1.0-alpha.3-Setup-x64.exe" in readme
        assert "**Current release: `0.1.0-alpha.3`**" in readme
        assert "0.1.0-alpha.2" not in readme

    def test_it_leaves_everything_else_alone(self, tmp_path: Path):
        repo = make_repository(tmp_path)
        (repo / "CHANGELOG.md").write_text("# Changelog\n\nUntouched.\n", encoding="utf-8")
        plan = release.plan_metadata_updates(
            repo,
            release.get_current_version(repo),
            release.parse_version("0.1.0-alpha.3"),
            date(2026, 9, 24),
        )
        release.apply_replacements(repo, plan)
        assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == (
            "# Changelog\n\nUntouched.\n"
        )

    def test_a_drifted_file_aborts_rather_than_being_edited(self, tmp_path: Path):
        """The check that stops a rewritten README being half-edited."""
        repo = make_repository(tmp_path)
        (repo / "README.md").write_text("Someone reworded this entirely.\n", encoding="utf-8")
        plan = release.plan_metadata_updates(
            repo,
            release.get_current_version(repo),
            release.parse_version("0.1.0-alpha.3"),
            date(2026, 9, 24),
        )
        with pytest.raises(release.ReleaseError, match="does not contain the expected text"):
            release.apply_replacements(repo, plan)

    def test_a_second_version_declaration_aborts(self, tmp_path: Path):
        repo = make_repository(tmp_path)
        path = repo / release.VERSION_FILE
        path.write_text(
            path.read_text(encoding="utf-8")
            + '\n# __version__: Final = "0.1.0-alpha.2"\n',
            encoding="utf-8",
        )
        plan = [
            release.Replacement(
                release.VERSION_FILE,
                '__version__: Final = "0.1.0-alpha.2"',
                '__version__: Final = "0.1.0-alpha.3"',
            )
        ]
        with pytest.raises(release.ReleaseError, match="contains 2 occurrence"):
            release.apply_replacements(repo, plan)

    def test_nothing_is_written_when_verification_fails(self, tmp_path: Path):
        """Verify first, then write.

        A plan that is wrong about its third file must not have already
        edited the first two.
        """
        repo = make_repository(tmp_path)
        before = (repo / release.VERSION_FILE).read_text(encoding="utf-8")
        plan = [
            release.Replacement(
                release.VERSION_FILE,
                '__version__: Final = "0.1.0-alpha.2"',
                '__version__: Final = "0.1.0-alpha.3"',
            ),
            release.Replacement(Path("README.md"), "absent", "present"),
        ]
        with pytest.raises(release.ReleaseError):
            release.apply_replacements(repo, plan)
        assert (repo / release.VERSION_FILE).read_text(encoding="utf-8") == before

    def test_a_same_day_rerelease_does_not_report_a_date_change(self, tmp_path: Path):
        """An edit whose text is unchanged is not an edit.

        The dry run must not claim it is one.
        """
        repo = make_repository(tmp_path)
        (repo / "CITATION.cff").write_text(
            "version: 0.1.0-alpha.2\ndate-released: '2026-09-24'\n", encoding="utf-8"
        )
        plan = release.plan_metadata_updates(
            repo,
            release.get_current_version(repo),
            release.parse_version("0.1.0-alpha.3"),
            date(2026, 9, 24),
        )
        assert all(item.old != item.new for item in plan)
        assert not any("date-released" in item.old for item in plan)


# ----------------------------------------------------------------------
# E - preflight
# ----------------------------------------------------------------------
class TestEPreflight:
    def test_a_clean_synchronized_repository_passes(self, tmp_path: Path):
        repo = make_repository(tmp_path)
        release.check_remote(repo)
        assert release.check_branch(repo) == "main"
        release.check_clean_tree(repo)
        release.check_synchronized(repo)
        release.check_tag_absent(repo, "v0.1.0-alpha.3")

    def test_another_branch_is_refused(self, tmp_path: Path):
        repo = make_repository(tmp_path)
        run_git(repo, "switch", "--quiet", "-c", "feature/something")
        with pytest.raises(release.ReleaseError, match="Releases are made from 'main'"):
            release.check_branch(repo)

    def test_a_modified_file_is_refused_and_named(self, tmp_path: Path):
        repo = make_repository(tmp_path)
        (repo / "README.md").write_text("changed\n", encoding="utf-8")
        with pytest.raises(release.ReleaseError, match="working tree is not clean") as caught:
            release.check_clean_tree(repo)
        assert "README.md" in str(caught.value)

    def test_an_untracked_file_is_refused(self, tmp_path: Path):
        """It would otherwise be swept into the release commit."""
        repo = make_repository(tmp_path)
        (repo / "stray.txt").write_text("x\n", encoding="utf-8")
        with pytest.raises(release.ReleaseError, match="working tree is not clean") as caught:
            release.check_clean_tree(repo)
        assert "stray.txt" in str(caught.value)

    def test_being_ahead_of_the_remote_is_refused(self, tmp_path: Path):
        """Unpushed local work blocks a release.

        It must be cut from what is published, not from work nobody else has
        seen.
        """
        repo = make_repository(tmp_path)
        (repo / "extra.txt").write_text("x\n", encoding="utf-8")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "--quiet", "-m", "local only")
        with pytest.raises(release.ReleaseError, match="ahead of origin/main"):
            release.check_synchronized(repo)

    def test_being_behind_the_remote_is_refused(self, tmp_path: Path):
        repo = make_repository(tmp_path)
        run_git(repo, "reset", "--hard", "--quiet", "HEAD~0")
        # Move origin forward from a second clone, leaving this one behind.
        other = tmp_path / "other"
        run_git(tmp_path, "clone", "--quiet", str(tmp_path / "origin.git"), str(other))
        run_git(other, "config", "user.email", "test@example.invalid")
        run_git(other, "config", "user.name", "Test")
        (other / "new.txt").write_text("x\n", encoding="utf-8")
        run_git(other, "add", "-A")
        run_git(other, "commit", "--quiet", "-m", "remote move")
        run_git(other, "push", "--quiet")
        run_git(repo, "fetch", "--quiet", "origin")
        with pytest.raises(release.ReleaseError, match="behind origin/main"):
            release.check_synchronized(repo)

    def test_an_existing_local_tag_is_refused(self, tmp_path: Path):
        repo = make_repository(tmp_path)
        run_git(repo, "tag", "-a", "v0.1.0-alpha.3", "-m", "x")
        with pytest.raises(release.ReleaseError, match="already exists locally"):
            release.check_tag_absent(repo, "v0.1.0-alpha.3")

    def test_an_existing_remote_tag_is_refused(self, tmp_path: Path):
        """The important half: a tag deleted locally is still published."""
        repo = make_repository(tmp_path)
        run_git(repo, "tag", "-a", "v0.1.0-alpha.3", "-m", "x")
        run_git(repo, "push", "--quiet", "origin", "v0.1.0-alpha.3")
        run_git(repo, "tag", "-d", "v0.1.0-alpha.3")
        with pytest.raises(release.ReleaseError, match="already exists on origin"):
            release.check_tag_absent(repo, "v0.1.0-alpha.3")

    def test_changed_paths_keeps_the_first_character_of_the_first_path(
        self, tmp_path: Path
    ):
        """The defect that stopped the first end-to-end release run.

        ``git status --porcelain`` writes two status columns then a space, so
        an unstaged modification begins with a blank column. Stripping the
        whole output - which is right for every other Git command - removes
        that leading space from the *first* line only, and the path then
        starts one character late: 'CITATION.cff' came back as 'ITATION.cff',
        the file list did not match, and the release refused to commit.

        Alphabetical order matters to the test: the first line is the only
        one affected, so the file under test has to sort first.
        """
        repo = make_repository(tmp_path)
        for name in ("CITATION.cff", "README.md"):
            path = repo / name
            path.write_text(path.read_text(encoding="utf-8") + "\n# edited\n", "utf-8")

        assert release.changed_paths(repo) == ["CITATION.cff", "README.md"]

    def test_changed_paths_reports_staged_and_untracked_alike(self, tmp_path: Path):
        repo = make_repository(tmp_path)
        (repo / "README.md").write_text("staged\n", encoding="utf-8")
        run_git(repo, "add", "README.md")
        (repo / "untracked.txt").write_text("x\n", encoding="utf-8")
        assert release.changed_paths(repo) == ["README.md", "untracked.txt"]

    def test_changed_paths_is_empty_for_a_clean_tree(self, tmp_path: Path):
        assert release.changed_paths(make_repository(tmp_path)) == []

    def test_a_missing_remote_is_refused(self, tmp_path: Path):
        repo = make_repository(tmp_path)
        run_git(repo, "remote", "remove", "origin")
        with pytest.raises(release.ReleaseError, match="No 'origin' remote"):
            release.check_remote(repo)

    @pytest.mark.parametrize("requested", ["0.1.0-alpha.1", "0.1.0-alpha.2", "0.0.9"])
    def test_a_release_may_not_go_backwards_or_sideways(self, requested: str):
        current = release.parse_version("0.1.0-alpha.2")
        with pytest.raises(release.ReleaseError):
            release._validate_forward(current, release.parse_version(requested))

    @pytest.mark.parametrize("requested", ["0.1.0-alpha.3", "0.1.0-beta.1", "0.1.0", "0.2.0"])
    def test_a_forward_release_is_allowed(self, requested: str):
        current = release.parse_version("0.1.0-alpha.2")
        release._validate_forward(current, release.parse_version(requested))


# ----------------------------------------------------------------------
# F - the command line
# ----------------------------------------------------------------------
class TestFCommandLine:
    def run_script(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        """Invoke release.py as a separate process, the way release.ps1 does."""
        return subprocess.run(
            [sys.executable, str(RELEASE_SCRIPT), *args],
            capture_output=True,
            text=True,
            cwd=str(cwd or REPOSITORY_ROOT),
            check=False,
        )

    def test_current_version_prints_only_the_version(self):
        """release.ps1 captures this, so anything else on stdout breaks it."""
        from omr_scanner._version import __version__

        done = self.run_script("--current-version")
        assert done.returncode == 0
        assert done.stdout.strip() == __version__
        assert "\n" not in done.stdout.strip()

    def test_suggest_next_version_prints_only_a_version(self):
        done = self.run_script("--suggest-next-version")
        assert done.returncode == 0
        release.parse_version(done.stdout.strip())

    def test_the_suggestion_is_after_the_current_version(self):
        current = release.get_current_version(REPOSITORY_ROOT)
        assert release._suggest_next(current) > current

    @pytest.mark.parametrize(
        ("current", "expected"),
        [
            ("0.1.0-alpha.2", "0.1.0-alpha.3"),
            ("0.1.0-beta.9", "0.1.0-beta.10"),
            ("0.1.0-rc.1", "0.1.0-rc.2"),
            ("0.1.0", "0.1.1"),
        ],
    )
    def test_the_suggestion_continues_the_current_series(self, current: str, expected: str):
        assert str(release._suggest_next(release.parse_version(current))) == expected

    def test_no_arguments_reports_the_version_and_refuses(self):
        """The no-argument contract: informative, and not a release."""
        from omr_scanner._version import __version__

        done = self.run_script()
        assert done.returncode == 2
        assert f"Current repository version: {__version__}" in done.stdout
        assert "A release version is required" in done.stderr
        assert "--version" in done.stdout

    def test_a_malformed_version_is_refused_before_anything_happens(self):
        done = self.run_script("--version", "0.1.0-alpha")
        assert done.returncode == 1
        assert "not a valid OMRFlow version" in done.stderr

    def test_a_query_works_from_any_directory(self, tmp_path: Path):
        """The script is found by path, so the working directory is irrelevant."""
        from omr_scanner._version import __version__

        done = self.run_script("--current-version", cwd=tmp_path)
        assert done.returncode == 0
        assert done.stdout.strip() == __version__

    def test_a_dry_run_changes_nothing(self, tmp_path: Path):
        """End to end, in a real repository, against the real script."""
        repo = make_repository(tmp_path)
        before = run_git(repo, "rev-parse", "HEAD")
        snapshot = {
            path.relative_to(repo): path.read_bytes()
            for path in sorted(repo.rglob("*"))
            if path.is_file() and ".git" not in path.parts
        }
        done = subprocess.run(
            [
                sys.executable,
                str(repo / "scripts" / "release.py"),
                "--version",
                "0.1.0-alpha.3",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            cwd=str(repo),
            check=False,
        )
        assert done.returncode == 0, done.stderr
        assert "DRY RUN COMPLETE" in done.stdout
        assert "Current version   : 0.1.0-alpha.2" in done.stdout
        assert "Requested version : 0.1.0-alpha.3" in done.stdout
        assert "Target tag        : v0.1.0-alpha.3" in done.stdout

        after = {
            path.relative_to(repo): path.read_bytes()
            for path in sorted(repo.rglob("*"))
            if path.is_file() and ".git" not in path.parts
        }
        assert after == snapshot
        assert run_git(repo, "rev-parse", "HEAD") == before
        assert run_git(repo, "tag", "--list") == ""
        assert run_git(repo, "status", "--porcelain") == ""

    def test_a_dry_run_on_a_dirty_tree_still_refuses(self, tmp_path: Path):
        """Dry run is not a way to preview a release you could not make."""
        repo = make_repository(tmp_path)
        (repo / "stray.txt").write_text("x\n", encoding="utf-8")
        done = subprocess.run(
            [
                sys.executable,
                str(repo / "scripts" / "release.py"),
                "--version",
                "0.1.0-alpha.3",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            cwd=str(repo),
            check=False,
        )
        assert done.returncode == 1
        assert "working tree is not clean" in done.stderr


# ----------------------------------------------------------------------
# G - the release script offers no way around its own checks
# ----------------------------------------------------------------------
class TestGNoBypasses:
    @pytest.mark.parametrize(
        "bypass", ["--force", "--skip-tests", "--allow-dirty", "--no-verify", "-f"]
    )
    def test_there_is_no_bypass_flag(self, bypass: str):
        """Every one of these exists to make a failing release succeed."""
        done = subprocess.run(
            [sys.executable, str(RELEASE_SCRIPT), bypass],
            capture_output=True,
            text=True,
            cwd=str(REPOSITORY_ROOT),
            check=False,
        )
        assert done.returncode != 0
        assert "unrecognized arguments" in done.stderr or "invalid" in done.stderr

    def test_no_git_call_can_rewrite_published_history(self):
        """Read off the actual call sites, not the prose.

        Searching the file for "--force" matches this script's own
        documentation about not having one. Parsing instead gives the
        arguments really handed to Git, which is the thing that would rewrite
        a published tag if it ever changed.
        """
        import ast as ast_module

        tree = ast_module.parse(RELEASE_SCRIPT.read_text(encoding="utf-8"))
        subcommands: set[str] = set()
        for node in ast_module.walk(tree):
            if not isinstance(node, ast_module.Call):
                continue
            if not (isinstance(node.func, ast_module.Name) and node.func.id == "git"):
                continue
            literals = [
                argument.value
                for argument in node.args
                if isinstance(argument, ast_module.Constant)
                and isinstance(argument.value, str)
            ]
            assert literals, "a git call with no literal subcommand"
            subcommands.add(literals[0])
            for argument in literals:
                assert argument not in {"-f", "--force", "--force-with-lease", "--delete"}, (
                    f"git {literals[0]} would be given {argument}"
                )

        # An allowlist, so a newly added destructive subcommand has to be
        # added here deliberately rather than slipping in.
        assert subcommands <= {
            "rev-parse",
            "status",
            "remote",
            "rev-list",
            "tag",
            "ls-remote",
            "fetch",
            "add",
            "commit",
            "push",
        }, subcommands

    def test_the_gates_are_the_ones_ci_runs(self):
        """A release that ran a smaller suite than CI proves less than CI."""
        names = [name for name, _ in release.RELEASE_GATES]
        assert names == ["Lint", "Types", "Tests"]
        commands = [" ".join(command[1:]) for _, command in release.RELEASE_GATES]
        assert commands == [
            "-m ruff check src tests tools scripts",
            "-m mypy src/omr_scanner",
            "-m pytest -q",
        ]

    def test_the_gates_match_the_ci_workflow_exactly(self):
        """Pinned against ci.yml, not just asserted in isolation.

        The release runs these locally and CI runs them again on the tag. If
        the two drift, a release passes here and fails there - or, worse,
        passes there having checked less.
        """
        yaml = pytest.importorskip("yaml", reason="PyYAML is in the dev extra")
        ci = yaml.safe_load(
            (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
        )
        steps = ci["jobs"]["lint-and-types"]["steps"]
        ci_commands = {str(step.get("run", "")).strip() for step in steps}
        local = {" ".join(command[1:]).replace("-m ", "", 1) for _, command in
                 release.RELEASE_GATES}
        assert "ruff check src tests tools scripts" in ci_commands
        assert "mypy src/omr_scanner" in ci_commands
        assert {"ruff check src tests tools scripts", "mypy src/omr_scanner"} <= local


# ----------------------------------------------------------------------
# H - the PowerShell entry point
# ----------------------------------------------------------------------
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")

WRAPPER = REPOSITORY_ROOT / "scripts" / "release.ps1"


@pytest.mark.skipif(POWERSHELL is None, reason="no PowerShell on this machine")
class TestHPowerShellEntryPoint:
    """The wrapper's own contract.

    Only the invocations that cannot start a release are exercised: help, the
    no-argument case, and a bad argument. None of them touches Git, so these
    are safe to run against the real checkout in whatever state it is in.
    """

    def run_wrapper(self, *args: str) -> subprocess.CompletedProcess:
        """Invoke release.ps1 the way a person at a prompt would.

        ``stdin`` is closed deliberately: if the script ever declared -Version
        mandatory, PowerShell would stop and prompt for it, and a closed stdin
        turns that hang into a failure this test can see.
        """
        assert POWERSHELL is not None
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(WRAPPER), *args],
            capture_output=True,
            text=True,
            cwd=str(REPOSITORY_ROOT),
            stdin=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )

    def test_no_arguments_shows_the_version_and_refuses(self):
        from omr_scanner._version import __version__

        done = self.run_wrapper()
        combined = done.stdout + done.stderr
        assert done.returncode == 2
        assert "OMRFlow release automation" in combined
        assert f"Current repository version: {__version__}" in combined
        assert "A release version is required" in combined
        assert ".\\scripts\\release.ps1 -Version" in combined

    def test_no_arguments_does_not_make_powershell_prompt(self):
        """PowerShell must never prompt for the missing version.

        A mandatory parameter would produce PowerShell's own prompt, which is
        a worse message than the script's and hangs a non-interactive caller.
        """
        done = self.run_wrapper()
        combined = done.stdout + done.stderr
        assert "Supply values for the following parameters" not in combined
        assert "cmdlet release.ps1" not in combined

    @pytest.mark.parametrize("form", ["-Help", "-h", "--help"])
    def test_every_help_spelling_works_and_shows_the_version(self, form: str):
        """Every help spelling works.

        All three, because PowerShell handles none of them the same way:
        -Help is a switch, -h is its alias, and --help is not a parameter at
        all until the script goes looking for it.
        """
        from omr_scanner._version import __version__

        done = self.run_wrapper(form)
        combined = done.stdout + done.stderr
        assert done.returncode == 0, combined
        assert f"Current repository version: {__version__}" in combined
        assert "Usage:" in combined
        assert "-DryRun" in combined

    def test_an_unrecognised_argument_is_named(self):
        done = self.run_wrapper("--publish-everything")
        combined = done.stdout + done.stderr
        assert done.returncode == 2
        assert "--publish-everything" in combined
        assert "Usage:" in combined

    def test_it_reports_the_same_version_as_the_python_script(self):
        """The two must not be able to disagree; the wrapper asks the script."""
        done = self.run_wrapper("--help")
        current = str(release.get_current_version(REPOSITORY_ROOT))
        assert f"Current repository version: {current}" in done.stdout + done.stderr


# ----------------------------------------------------------------------
# I - the release workflow
# ----------------------------------------------------------------------
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    """``.github/workflows/release.yml``, parsed."""
    yaml = pytest.importorskip("yaml", reason="PyYAML is in the dev extra")
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


class TestIReleaseWorkflow:
    """What the workflow must guarantee about publication.

    These are the properties that decide whether a bad build can reach
    somebody's machine. They are asserted on the parsed workflow because the
    file is edited rarely and by hand, which is exactly when a `needs:` goes
    missing without anyone noticing until a release is already out.
    """

    def test_it_is_valid_yaml_with_the_expected_jobs(self, workflow: dict):
        assert workflow["name"] == "Release"
        assert set(workflow["jobs"]) == {"validate", "lint", "test", "package", "publish"}

    def test_it_triggers_on_version_tags(self, workflow: dict):
        # PyYAML reads the bare key `on` as the boolean True.
        triggers = workflow.get("on") or workflow[True]
        assert list(triggers["push"]["tags"]) == ["v*"]

    def test_publication_waits_for_every_gate(self, workflow: dict):
        """The whole design in one assertion.

        Nothing is published until the tag, the linters, both test platforms
        and the installer have all passed.
        """
        assert set(workflow["jobs"]["publish"]["needs"]) == {
            "validate",
            "lint",
            "test",
            "package",
        }

    def test_the_installer_is_built_only_after_the_tests(self, workflow: dict):
        assert set(workflow["jobs"]["package"]["needs"]) == {"validate", "lint", "test"}

    def test_both_platforms_are_tested(self, workflow: dict):
        """Both platforms gate the release.

        Ubuntu has caught defects Windows did not; a Windows-only release
        gate would have published them.
        """
        matrix = workflow["jobs"]["test"]["strategy"]["matrix"]["os"]
        assert set(matrix) == {"windows-latest", "ubuntu-latest"}

    def test_the_suite_is_not_narrowed_for_the_release(self, workflow: dict):
        """A release that ran fewer tests than CI proves less than CI."""
        steps = workflow["jobs"]["test"]["steps"]
        commands = [str(step.get("run", "")) for step in steps]
        assert any(command.strip() == "pytest -q" for command in commands), commands

    def test_only_the_publishing_job_may_write(self, workflow: dict):
        """Least privilege.

        A compromised or simply broken build step cannot create or alter a
        release.
        """
        assert workflow["permissions"] == {"contents": "read"}
        assert workflow["jobs"]["publish"]["permissions"] == {"contents": "write"}
        for name in ("validate", "lint", "test", "package"):
            assert "permissions" not in workflow["jobs"][name], name

    def test_the_release_is_published_not_drafted(self, workflow: dict):
        """The release is published, not drafted.

        Zenodo archives on publication. A draft would silently skip the
        archival this project depends on.
        """
        step = self._publish_step(workflow)
        assert step["with"]["draft"] is False

    def test_release_notes_are_generated_rather_than_written(self, workflow: dict):
        """Notes come from the history.

        No routine release may depend on somebody - or something - writing
        prose.
        """
        step = self._publish_step(workflow)
        assert step["with"]["generate_release_notes"] is True

    def test_the_prerelease_flag_is_derived_from_the_version(self, workflow: dict):
        step = self._publish_step(workflow)
        assert "is_prerelease" in str(step["with"]["prerelease"])
        assert "is_prerelease" in str(step["with"]["make_latest"])

    def test_missing_artifacts_fail_the_publication(self, workflow: dict):
        step = self._publish_step(workflow)
        assert step["with"]["fail_on_unmatched_files"] is True
        assert "SHA256SUMS.txt" in step["with"]["files"]

    def test_one_release_per_tag(self, workflow: dict):
        concurrency = workflow["concurrency"]
        assert "github.ref" in concurrency["group"]
        # A release must never be cancelled halfway through publishing.
        assert concurrency["cancel-in-progress"] is False

    def test_an_existing_release_is_never_overwritten(self, workflow: dict):
        """An existing release is never overwritten.

        Checked twice: before the build, and again immediately before
        publishing, because the window between them is minutes long.
        """
        text = WORKFLOW.read_text(encoding="utf-8")
        assert text.count("gh release view") == 2

    def test_no_step_swallows_its_own_failure(self, workflow: dict):
        """No step swallows its own failure.

        ``continue-on-error`` on a release gate is the same as not having the
        gate.
        """
        for name, job in workflow["jobs"].items():
            assert "continue-on-error" not in job, name
            for step in job.get("steps", []):
                assert "continue-on-error" not in step, (name, step.get("name"))

    def _publish_step(self, workflow: dict) -> dict:
        """The step that creates the GitHub release."""
        for step in workflow["jobs"]["publish"]["steps"]:
            if str(step.get("uses", "")).startswith("softprops/action-gh-release"):
                return step
        raise AssertionError("no release-publishing step found")
