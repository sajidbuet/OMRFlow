"""Tests for the application's version and release maturity.

Scope:
    The version is the one fact every release artifact, the installer, the
    About dialog, the diagnostic bundle and every bug report depend on. These
    tests pin that it is written down once, that it is simultaneously a valid
    Semantic Versioning and PEP 440 string, and that the release channel is
    *derived* from it rather than configured beside it.

    ===== ==========================================================
    Test  Behaviour
    ===== ==========================================================
    A     The version is accessible and correctly formatted.
    B     It is valid under both SemVer and PEP 440.
    C     The release channel is derived, not declared.
    D     One source of truth: the package and the metadata agree.
    E     Tag, artifact name and build identifier.
    F     A prerelease build says so, everywhere it identifies itself.
    ===== ==========================================================

Why the packaging metadata is checked against the module:
    ``pyproject.toml`` reads the version out of ``_version.py``. If someone
    reintroduces a static ``version = "..."`` there, everything keeps working
    right up until the two drift - and then the installer, the wheel and the
    About dialog disagree about which build a tester is running. The
    disagreement is the defect, so it is what gets asserted.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.version import InvalidVersion, Version

import omr_scanner
from omr_scanner import _version
from omr_scanner._version import (
    ALPHA_NOTICE,
    IS_PRERELEASE,
    RELEASE_CHANNEL,
    RELEASE_VERSION,
    ReleaseChannel,
    __version__,
    artifact_version,
    build_identifier,
    release_tag,
    version_info,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+(?P<build>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
"""The official Semantic Versioning 2.0.0 regular expression, from semver.org."""


class TestAAccessible:
    def test_the_version_is_importable_from_the_package_root(self):
        """Where every caller already looks for it."""
        assert omr_scanner.__version__ == __version__

    def test_the_version_is_a_non_empty_string(self):
        assert isinstance(__version__, str)
        assert __version__.strip() == __version__
        assert __version__

    def test_the_release_version_is_the_version_without_its_prerelease(self):
        """Asserted as a property, never as a literal.

        This test used to pin the exact version string. That made every
        release edit a test in order to pass its own gates - and since
        `scripts/release.ps1` runs those gates *after* bumping the version,
        the bump aborted itself. A version-independent assertion checks the
        same relationship without being a release blocker.
        """
        assert __version__.startswith(RELEASE_VERSION)
        assert re.fullmatch(r"\d+\.\d+\.\d+", RELEASE_VERSION), RELEASE_VERSION


class TestBValidUnderBothSchemes:
    def test_it_is_valid_semantic_versioning(self):
        """The spelling used for the Git tag, the installer and the GUI."""
        assert SEMVER.match(__version__), __version__

    def test_it_is_valid_pep_440(self):
        """The spelling Python packaging needs.

        The same string satisfies both, which is the whole reason one
        constant can serve the release process and the wheel. If a future
        version broke this - `0.1.0-alpha1` would - the build would fail
        somewhere far less obvious than here.
        """
        try:
            parsed = Version(__version__)
        except InvalidVersion as exc:  # pragma: no cover - the assertion is the point
            pytest.fail(f"{__version__} is not a PEP 440 version: {exc}")
        assert parsed.is_prerelease

    def test_pep_440_normalisation_is_what_packaging_will_use(self):
        """Derived from the current version rather than pinned to one.

        `-alpha.N` normalises to `aN`, `-beta.N` to `bN`, `-rc.N` to `rcN`.
        Computing the expectation keeps this a real check of what packaging
        does, without making it something a release has to edit.
        """
        suffixes = {"alpha": "a", "beta": "b", "rc": "rc"}
        expected = RELEASE_VERSION
        match = re.search(r"-(alpha|beta|rc)\.(\d+)$", __version__)
        if match:
            expected += suffixes[match.group(1)] + match.group(2)
        assert str(Version(__version__)) == expected

    def test_the_semver_and_pep_440_readings_agree_on_the_release(self):
        parsed = Version(__version__)
        assert f"{parsed.major}.{parsed.minor}.{parsed.micro}" == RELEASE_VERSION


class TestCChannelIsDerived:
    def test_this_build_is_on_the_alpha_channel(self):
        assert RELEASE_CHANNEL is ReleaseChannel.ALPHA
        assert RELEASE_CHANNEL.value == "Alpha"

    def test_an_alpha_build_is_a_prerelease(self):
        assert IS_PRERELEASE is True
        assert RELEASE_CHANNEL.is_prerelease is True

    @pytest.mark.parametrize(
        ("version", "channel"),
        [
            ("0.1.0-alpha.1", ReleaseChannel.ALPHA),
            ("0.1.0a1", ReleaseChannel.ALPHA),
            ("0.1.0-beta.2", ReleaseChannel.BETA),
            ("0.1.0b2", ReleaseChannel.BETA),
            ("1.0.0-rc.1", ReleaseChannel.RELEASE_CANDIDATE),
            ("1.0.0rc1", ReleaseChannel.RELEASE_CANDIDATE),
            ("1.0.0", ReleaseChannel.STABLE),
            ("2.3.4", ReleaseChannel.STABLE),
        ],
    )
    def test_the_channel_follows_the_version_string(
        self, version: str, channel: ReleaseChannel
    ):
        """The maturity claim cannot be set independently of the version.

        This is what stops a build carrying a prerelease version while
        presenting itself as stable, which is the specific mistake the
        release process is designed to make impossible.
        """
        assert _version._parse(version)[1] is channel

    def test_only_the_stable_channel_is_not_a_prerelease(self):
        for channel in ReleaseChannel:
            assert channel.is_prerelease is (channel is not ReleaseChannel.STABLE)

    def test_an_unrecognisable_version_is_rejected_rather_than_assumed_stable(self):
        """Failing loudly beats quietly promoting a typo to a stable release."""
        for bad in ("", "1.0", "v1.0.0", "not-a-version", "1.0.0-unknown.1"):
            with pytest.raises(ValueError, match="not a recognised OMRFlow version"):
                _version._parse(bad)


class TestDSingleSourceOfTruth:
    def test_pyproject_declares_the_version_dynamically(self):
        """A static version here is how metadata comes to disagree with code."""
        data = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        assert "version" in data["project"].get("dynamic", []), (
            "pyproject.toml must take the version from _version.py, not restate it"
        )
        assert "version" not in data["project"]

    def test_pyproject_points_at_the_version_module(self):
        data = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        attr = data["tool"]["setuptools"]["dynamic"]["version"]["attr"]
        assert attr == "omr_scanner._version.__version__"

    def test_the_installed_metadata_matches_the_module(self):
        """What `pip` reports and what the application reports are one build.

        Compared after PEP 440 normalisation, because packaging stores
        `0.1.0a1` for the SemVer `0.1.0-alpha.1` - the same version, spelled
        for a different audience.
        """
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as metadata_version

        try:
            installed = metadata_version("omrflow")
        except PackageNotFoundError:  # pragma: no cover - only when not installed
            pytest.skip("omrflow is not installed in this environment")
        assert Version(installed) == Version(__version__), (
            f"installed metadata says {installed}, the package says "
            f"{__version__}. An editable install caches its metadata at "
            "install time, so reinstall after changing the version: "
            "`pip install -e .`"
        )

    def test_the_version_is_not_hard_coded_anywhere_else_in_the_package(self):
        """One place to edit for a release.

        Scans the package for the literal version string. Only `_version.py`
        may contain it; anything else is a copy that a release will forget.
        """
        package = REPOSITORY_ROOT / "src" / "omr_scanner"
        offenders = [
            path.relative_to(REPOSITORY_ROOT).as_posix()
            for path in package.rglob("*.py")
            if path.name != "_version.py"
            and __version__ in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], offenders

    def test_the_cli_reports_the_same_version(self):
        """`omrflow --version` is what a support request will be asked for."""
        completed = subprocess.run(
            [sys.executable, "-m", "omr_scanner.main", "--version"],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPOSITORY_ROOT,
        )
        assert __version__ in completed.stdout + completed.stderr


class TestETagAndArtifactNames:
    def test_the_release_tag_is_the_version_prefixed_with_v(self):
        assert release_tag() == f"v{__version__}"
        assert release_tag("1.2.3") == "v1.2.3"

    def test_the_artifact_version_is_the_semver_spelling(self):
        """The installer filename must be readable, not normalised."""
        assert artifact_version() == __version__

    def test_the_build_identifier_starts_with_the_version(self):
        assert build_identifier().startswith(__version__)

    def test_the_build_identifier_adds_a_local_part_when_a_commit_is_known(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """So a downloaded installer is traceable back to a source revision."""
        monkeypatch.setenv(_version.BUILD_COMMIT_ENVIRONMENT_VARIABLE, "deadbee")
        assert build_identifier() == f"{__version__}+deadbee"
        assert Version(build_identifier()).local == "deadbee"

    def test_the_build_identifier_degrades_to_the_bare_version(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """A packaged build has no repository to ask, and must still start."""
        monkeypatch.delenv(_version.BUILD_COMMIT_ENVIRONMENT_VARIABLE, raising=False)
        monkeypatch.setattr(_version, "_git_commit", lambda *_args: None)
        assert build_identifier() == __version__

    def test_a_missing_git_checkout_yields_no_commit(self, tmp_path: Path):
        assert _version._git_commit(tmp_path) is None


class TestFPrereleaseIdentifiesItself:
    def test_version_info_carries_everything_a_bug_report_needs(self):
        info = version_info()
        assert info["version"] == __version__
        assert info["release_version"] == RELEASE_VERSION
        assert info["release_channel"] == "Alpha"
        assert info["is_prerelease"] == "true"
        assert info["build"].startswith(__version__)
        assert all(isinstance(value, str) for value in info.values())

    def test_the_alpha_notice_states_the_fact_and_the_instruction(self):
        """Two things: qualification is incomplete, and verify results."""
        assert "Alpha" in ALPHA_NOTICE
        assert "qualification" in ALPHA_NOTICE
        assert "verify" in ALPHA_NOTICE.lower()

    def test_the_packaging_classifier_matches_the_channel(self):
        """A stable classifier on an Alpha build is a false maturity claim."""
        data = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        classifiers = data["project"]["classifiers"]
        assert "Development Status :: 3 - Alpha" in classifiers
        assert not any("5 - Production/Stable" in item for item in classifiers)
