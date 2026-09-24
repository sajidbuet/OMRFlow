"""Prepare an OMRFlow release: check, bump, validate, commit, tag, push.

Purpose:
    Make a routine release a single deterministic command. Everything that
    used to be a checklist item a person (or a model) carried out by hand is
    done here, in a fixed order, with every check failing closed.

    This script's job ends at a pushed tag. It never builds an installer and
    never creates a GitHub Release: `.github/workflows/release.yml` does
    that, from the tagged commit, on a clean runner. Splitting it there is
    what makes the published artifact reproducible from the tag alone rather
    than a property of whoever's laptop ran the release.

Responsibilities:
    * :func:`get_current_version` - the authoritative reading of the version
      the repository currently declares. Every other caller, including
      ``release.ps1``, goes through this so that two answers cannot disagree.
    * :class:`Version` - parsing, ordering and the tag/prerelease spellings.
    * The preflight checks, the metadata edits, the release gates, and the
      commit/tag/push.

What does NOT belong here:
    * Building, packaging, checksumming or publishing. Those live in
      ``scripts/release/*.ps1`` and are driven by the release workflow.
    * Any call to a model, a changelog service or anything else
      non-deterministic. A routine release must produce the same result
      every time from the same inputs.

Why the version is read with `ast` rather than imported:
    Importing ``omr_scanner`` would require the package to be installed and
    would execute module code. The version has to be readable in a bare
    checkout, before any environment is set up, and reading a release script's
    input must never run the code it is reading. :mod:`ast` parses the literal
    and nothing else.

Why there is no --force, --skip-tests or --allow-dirty:
    Every one of them exists to make a failing release succeed. The failures
    this script reports are the ones worth stopping for, and a release that
    needed a bypass is a release that should not be published.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

# ----------------------------------------------------------------------
# Version
# ----------------------------------------------------------------------
VERSION_PATTERN: Final = re.compile(
    r"^(?P<major>0|[1-9]\d*)"
    r"\.(?P<minor>0|[1-9]\d*)"
    r"\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<stage>alpha|beta|rc)\.(?P<number>0|[1-9]\d*))?$"
)
"""The versions OMRFlow releases, and no others.

Deliberately narrower than SemVer: this project spells prereleases
``-alpha.N``, and accepting ``-alpha1`` or ``-dev`` here would let a tag
through that ``_version.py`` then refuses, or - worse - one that PEP 440
normalises to something the wheel and the installer disagree about.
"""

_STAGE_ORDER: Final = {"alpha": 0, "beta": 1, "rc": 2}
"""Prerelease stages, in release order. A final release sorts above all."""

_FINAL_RANK: Final = 3


@dataclass(frozen=True, order=False)
class Version:
    """One OMRFlow version, parsed into its comparable parts.

    Attributes:
        major: First component.
        minor: Second component.
        patch: Third component.
        stage: ``"alpha"``, ``"beta"``, ``"rc"``, or ``None`` for a final
            release.
        number: The prerelease number, or ``None`` for a final release.
    """

    major: int
    minor: int
    patch: int
    stage: str | None
    number: int | None

    def __str__(self) -> str:
        """The canonical spelling, e.g. ``0.1.0-alpha.2``."""
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base if self.stage is None else f"{base}-{self.stage}.{self.number}"

    @property
    def tag(self) -> str:
        """The Git tag for this version: ``v`` followed by the version."""
        return f"v{self}"

    @property
    def is_prerelease(self) -> bool:
        """Whether a GitHub release for this version must be a pre-release."""
        return self.stage is not None

    @property
    def sort_key(self) -> tuple[int, int, int, int, int]:
        """A tuple that orders versions correctly.

        The fourth element ranks the stage so that every prerelease sorts
        below the final release of the same number - ``0.1.0-rc.1`` before
        ``0.1.0`` - which a lexical comparison of the strings gets backwards.
        """
        rank = _FINAL_RANK if self.stage is None else _STAGE_ORDER[self.stage]
        return (self.major, self.minor, self.patch, rank, self.number or 0)

    def __lt__(self, other: Version) -> bool:
        """Order by :attr:`sort_key`."""
        return self.sort_key < other.sort_key

    def __le__(self, other: Version) -> bool:
        """Order by :attr:`sort_key`."""
        return self.sort_key <= other.sort_key

    def __gt__(self, other: Version) -> bool:
        """Order by :attr:`sort_key`."""
        return self.sort_key > other.sort_key

    def __ge__(self, other: Version) -> bool:
        """Order by :attr:`sort_key`."""
        return self.sort_key >= other.sort_key


class ReleaseError(Exception):
    """A release cannot proceed. The message is shown to the operator."""


def parse_version(text: str) -> Version:
    """Parse ``text`` into a :class:`Version`.

    Args:
        text: A version, with or without a leading ``v``. The ``v`` is
            accepted because the Git tag carries one and typing the tag is a
            natural mistake; it is stripped so that only one spelling ever
            reaches the rest of the script.

    Returns:
        The parsed version.

    Raises:
        ReleaseError: ``text`` is not a version this project releases.
    """
    candidate = text.strip()
    if candidate.startswith(("v", "V")):
        candidate = candidate[1:]
    match = VERSION_PATTERN.match(candidate)
    if match is None:
        raise ReleaseError(
            f"'{text}' is not a valid OMRFlow version.\n"
            "\n"
            "Expected MAJOR.MINOR.PATCH, optionally followed by a prerelease:\n"
            "  0.1.0            a final release\n"
            "  0.1.0-alpha.3    an alpha\n"
            "  0.1.0-beta.1     a beta\n"
            "  0.1.0-rc.1       a release candidate"
        )
    stage = match.group("stage")
    return Version(
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        stage=stage,
        number=int(match.group("number")) if stage else None,
    )


# ----------------------------------------------------------------------
# Repository
# ----------------------------------------------------------------------
VERSION_FILE: Final = Path("src") / "omr_scanner" / "_version.py"
"""The single place the version is written down. See that module's docstring."""

CITATION_FILE: Final = Path("CITATION.cff")
README_FILE: Final = Path("README.md")


def find_repository_root(start: Path | None = None) -> Path:
    """Locate the OMRFlow checkout containing ``start``.

    Asks Git rather than assuming the current directory, so the script works
    when invoked by path from anywhere. Falls back to walking up from this
    file, which covers a checkout where Git is present but the caller's
    directory is outside the work tree.

    Args:
        start: Directory to search from. Defaults to this file's directory.

    Returns:
        The repository root.

    Raises:
        ReleaseError: No OMRFlow checkout was found.
    """
    origin = (start or Path(__file__).resolve().parent).resolve()
    try:
        found = subprocess.run(
            ["git", "-C", str(origin), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if found.returncode == 0:
            root = Path(found.stdout.strip())
            if (root / VERSION_FILE).is_file():
                return root
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git
        pass

    for candidate in (origin, *origin.parents):
        if (candidate / VERSION_FILE).is_file():
            return candidate
    raise ReleaseError(
        "This does not look like an OMRFlow checkout: no "
        f"{VERSION_FILE.as_posix()} was found from {origin}."
    )


def get_current_version(repo_root: Path) -> Version:
    """Read the version the repository currently declares.

    The authoritative implementation. ``release.ps1`` calls this through
    ``--current-version`` rather than parsing anything itself, so the two can
    never report different answers.

    Args:
        repo_root: The checkout to read.

    Returns:
        The declared version.

    Raises:
        ReleaseError: The file is missing, unparsable, declares no
            ``__version__``, declares it more than once, or declares
            something that is not a version this project releases.
    """
    path = repo_root / VERSION_FILE
    if not path.is_file():
        raise ReleaseError(
            f"Cannot read the current version: {path} does not exist."
        )
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise ReleaseError(f"Cannot read the current version from {path}: {exc}") from exc

    found: list[str] = []
    for node in tree.body:
        target = _assigned_name(node)
        if target != "__version__":
            continue
        value = getattr(node, "value", None)
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            raise ReleaseError(
                f"__version__ in {path} is not a plain string literal, so it "
                "cannot be read without executing the module."
            )
        found.append(value.value)

    if not found:
        raise ReleaseError(f"No __version__ assignment found in {path}.")
    if len(found) > 1:
        raise ReleaseError(
            f"{path} assigns __version__ {len(found)} times ({', '.join(found)}). "
            "Exactly one declaration is required."
        )
    return parse_version(found[0])


def _assigned_name(node: ast.stmt) -> str | None:
    """The single name a module-level assignment binds, if it binds one."""
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name):
            return target.id
    return None


# ----------------------------------------------------------------------
# Git
# ----------------------------------------------------------------------
DEFAULT_BRANCH: Final = "main"
REMOTE: Final = "origin"


def git(repo_root: Path, *args: str, check: bool = True, strip: bool = True) -> str:
    """Run a Git command in ``repo_root`` and return its stdout.

    A fixed argument list and no shell: a version string reaches Git as one
    argument and can never be reinterpreted as an option or a second command,
    however it is spelled.

    Args:
        repo_root: Working directory for the command.
        *args: Arguments after ``git``.
        check: Raise when Git exits non-zero.
        strip: Strip surrounding whitespace from the output. Callers reading
            ``status --porcelain`` must pass ``False``: its first two columns
            are the status codes, and an unmodified-but-staged file leaves
            column one blank, so stripping deletes a significant leading
            space and shifts every path in the first line by one character.

    Returns:
        Standard output, stripped unless ``strip`` is false.

    Raises:
        ReleaseError: Git is missing, or it failed and ``check`` is set.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ReleaseError(
            "git was not found on PATH. Install Git for Windows from "
            "https://git-scm.com/download/win and reopen the terminal."
        ) from exc
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ReleaseError(f"git {' '.join(args)} failed:\n{detail}")
    return completed.stdout.strip() if strip else completed.stdout


def check_branch(repo_root: Path) -> str:
    """Confirm the release is being made from the default branch.

    Raises:
        ReleaseError: A different branch is checked out, or HEAD is detached.
    """
    branch = git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != DEFAULT_BRANCH:
        raise ReleaseError(
            f"Releases are made from '{DEFAULT_BRANCH}', but '{branch}' is "
            "checked out.\n"
            "\n"
            f"Switch to it yourself when that is what you intend:\n"
            f"  git switch {DEFAULT_BRANCH}"
        )
    return branch


def changed_paths(repo_root: Path) -> list[str]:
    """Every path Git reports as modified, staged or untracked.

    Reads ``status --porcelain``, whose format is two status columns, a
    space, then the path. The output is taken unstripped for the reason given
    in :func:`git`.

    Returns:
        The paths, sorted. Quoting is undone for the simple case Git applies
        it to; a path odd enough to still be quoted will not match the
        expected release files and so will stop the release, which is the
        safe direction.
    """
    status = git(repo_root, "status", "--porcelain", strip=False)
    return sorted(
        line[3:].strip().strip('"') for line in status.splitlines() if line.strip()
    )


def check_clean_tree(repo_root: Path) -> None:
    """Confirm nothing is modified, staged or untracked.

    A release commit must contain the version bump and nothing else, and the
    only way to promise that is to start from a clean tree.

    Raises:
        ReleaseError: The working tree is dirty, listing what is dirty.
    """
    status = git(repo_root, "status", "--porcelain", strip=False).strip()
    if status:
        listed = "\n".join(f"  {line}" for line in status.splitlines())
        raise ReleaseError(
            "The working tree is not clean, so a release cannot start.\n"
            "\n"
            f"{listed}\n"
            "\n"
            "Commit, stash or discard these first. There is deliberately no "
            "option to release anyway."
        )


def check_remote(repo_root: Path) -> None:
    """Confirm the expected remote exists.

    Raises:
        ReleaseError: There is no ``origin``.
    """
    remotes = git(repo_root, "remote").splitlines()
    if REMOTE not in remotes:
        raise ReleaseError(
            f"No '{REMOTE}' remote is configured (found: "
            f"{', '.join(remotes) or 'none'}).\n"
            "Add it yourself; this script will not rewrite your remotes."
        )


def check_synchronized(repo_root: Path) -> None:
    """Confirm the local branch matches its upstream exactly.

    Fails closed on ahead, behind and diverged alike. Merging, rebasing or
    resetting on the operator's behalf is exactly the kind of history
    rewriting a release script must never do unasked.

    Raises:
        ReleaseError: Local and remote have diverged in any direction.
    """
    upstream = f"{REMOTE}/{DEFAULT_BRANCH}"
    counts = git(repo_root, "rev-list", "--left-right", "--count", f"{upstream}...HEAD")
    behind_text, _, ahead_text = counts.partition("\t")
    behind, ahead = int(behind_text or 0), int(ahead_text or 0)
    if behind and ahead:
        raise ReleaseError(
            f"Local {DEFAULT_BRANCH} and {upstream} have diverged "
            f"({ahead} local, {behind} remote commit(s)).\n"
            "Reconcile them yourself before releasing."
        )
    if behind:
        raise ReleaseError(
            f"Local {DEFAULT_BRANCH} is {behind} commit(s) behind {upstream}.\n"
            "  git pull --ff-only"
        )
    if ahead:
        raise ReleaseError(
            f"Local {DEFAULT_BRANCH} is {ahead} commit(s) ahead of {upstream}.\n"
            "Push or drop them first, so the release is cut from what is "
            "published:\n"
            "  git push"
        )


def check_tag_absent(repo_root: Path, tag: str) -> None:
    """Confirm ``tag`` exists neither locally nor on the remote.

    Raises:
        ReleaseError: The tag already exists. A published tag is immutable;
            the fix is a new version, never a moved tag.
    """
    local = git(repo_root, "tag", "--list", tag)
    if local:
        raise ReleaseError(
            f"The tag {tag} already exists locally.\n"
            "Released tags are never moved. Choose the next version instead."
        )
    remote = git(repo_root, "ls-remote", "--tags", REMOTE, f"refs/tags/{tag}")
    if remote:
        raise ReleaseError(
            f"The tag {tag} already exists on {REMOTE}.\n"
            "That release is published and immutable. Choose the next version."
        )


# ----------------------------------------------------------------------
# Metadata
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Replacement:
    """One textual edit a release makes.

    Attributes:
        path: File to edit, relative to the repository root.
        old: Exact text to replace.
        new: Its replacement.
        expected: How many occurrences must be present. ``None`` means "one
            or more", used where a version legitimately appears several times
            in prose.
        required: Whether the file must exist.
    """

    path: Path
    old: str
    new: str
    expected: int | None = 1
    required: bool = True


def plan_metadata_updates(
    repo_root: Path, current: Version, target: Version, released: date
) -> list[Replacement]:
    """Describe every edit this release makes, without making any of them.

    Returning a plan rather than editing in place is what lets ``--dry-run``
    report exactly what a real run would do, using the same code.

    Args:
        repo_root: The checkout.
        current: The version being replaced.
        target: The version being released.
        released: The release date, for ``CITATION.cff``.

    Returns:
        The replacements, in the order they will be applied.
    """
    badge = str(current).replace("-", "--")
    badge_new = str(target).replace("-", "--")
    plan = [
        Replacement(
            VERSION_FILE,
            f'__version__: Final = "{current}"',
            f'__version__: Final = "{target}"',
        ),
    ]
    if (repo_root / CITATION_FILE).is_file():
        plan.append(Replacement(CITATION_FILE, f"version: {current}", f"version: {target}"))
        existing = _citation_date(repo_root)
        if existing is not None:
            plan.append(
                Replacement(
                    CITATION_FILE,
                    f"date-released: '{existing}'",
                    f"date-released: '{released.isoformat()}'",
                )
            )
    if (repo_root / README_FILE).is_file():
        plan.extend(
            [
                # shields.io escapes a literal hyphen as '--', so the badge
                # carries a different spelling of the same version.
                Replacement(
                    README_FILE,
                    f"badge/release-{badge}-",
                    f"badge/release-{badge_new}-",
                    expected=None,
                ),
                Replacement(
                    README_FILE,
                    f"OMRFlow-{current}-Setup-x64.exe",
                    f"OMRFlow-{target}-Setup-x64.exe",
                    expected=None,
                ),
                Replacement(
                    README_FILE,
                    f"**Current release: `{current}`**",
                    f"**Current release: `{target}`**",
                    expected=None,
                ),
            ]
        )
    # A replacement whose text is unchanged is not an edit. The date is the
    # one that can come out equal - releasing twice in a day - and reporting
    # it as a change in the dry run would be a lie about what will happen.
    return [item for item in plan if item.old != item.new]


def _citation_date(repo_root: Path) -> str | None:
    """The date currently recorded in ``CITATION.cff``, if any."""
    text = (repo_root / CITATION_FILE).read_text(encoding="utf-8")
    match = re.search(r"^date-released:\s*'([^']*)'", text, re.MULTILINE)
    return match.group(1) if match else None


def apply_replacements(repo_root: Path, plan: list[Replacement]) -> list[Path]:
    """Apply a plan, verifying each edit's occurrence count first.

    Every replacement states how many matches it expects. A file that has
    drifted - a README someone reworded, a second ``__version__`` - produces
    the wrong count, and this aborts instead of editing something it does not
    understand.

    Args:
        repo_root: The checkout.
        plan: Replacements from :func:`plan_metadata_updates`.

    Returns:
        The distinct files changed, relative to the root.

    Raises:
        ReleaseError: A file is missing, or an occurrence count is wrong.
    """
    verify_replacements(repo_root, plan)
    changed: list[Path] = []
    for item in plan:
        path = repo_root / item.path
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(item.old, item.new), encoding="utf-8")
        if item.path not in changed:
            changed.append(item.path)
    return changed


def verify_replacements(repo_root: Path, plan: list[Replacement]) -> None:
    """Check every replacement would match the expected number of times.

    Raises:
        ReleaseError: A required file is missing or a count is wrong.
    """
    for item in plan:
        path = repo_root / item.path
        if not path.is_file():
            if item.required:
                raise ReleaseError(f"Expected to update {item.path}, which does not exist.")
            continue
        count = path.read_text(encoding="utf-8").count(item.old)
        if item.expected is None:
            if count < 1:
                raise ReleaseError(
                    f"{item.path} does not contain the expected text:\n"
                    f"  {item.old}\n"
                    "The file has drifted from what this release script knows "
                    "how to update. Fix the file, or update scripts/release.py."
                )
        elif count != item.expected:
            raise ReleaseError(
                f"{item.path} contains {count} occurrence(s) of:\n"
                f"  {item.old}\n"
                f"but exactly {item.expected} was expected. Refusing to edit a "
                "file that is not in the shape this script understands."
            )


def changelog_has_section(repo_root: Path, target: Version) -> bool:
    """Whether ``CHANGELOG.md`` already documents ``target``."""
    path = repo_root / "CHANGELOG.md"
    if not path.is_file():
        return False
    return f"## [{target}]" in path.read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# Gates
# ----------------------------------------------------------------------
RELEASE_GATES: Final = (
    ("Lint", [sys.executable, "-m", "ruff", "check", "src", "tests", "tools", "scripts"]),
    ("Types", [sys.executable, "-m", "mypy", "src/omr_scanner"]),
    ("Tests", [sys.executable, "-m", "pytest", "-q"]),
)
"""The checks CI runs, run here before anything is committed.

Exactly the three gates `.github/workflows/ci.yml` runs, in its order, so a
release cannot pass locally and then fail on the runner. Deliberately the
whole suite: a release is the one moment a shorter one is not good enough.
"""


def run_release_gates(repo_root: Path) -> None:
    """Run the release gates, stopping at the first failure.

    Raises:
        ReleaseError: A gate failed. Its command is named so the operator can
            re-run exactly that one.
    """
    import os

    environment = dict(os.environ)
    # The GUI suite needs a Qt platform; a developer machine has a desktop and
    # would otherwise open real windows for several hundred tests.
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")

    for name, command in RELEASE_GATES:
        printed = " ".join(command[1:])
        print(f"  {name:<8} python {printed}")
        completed = subprocess.run(command, cwd=repo_root, env=environment, check=False)
        if completed.returncode != 0:
            raise ReleaseError(
                f"{name} failed (exit {completed.returncode}).\n"
                "\n"
                f"Re-run it with:\n"
                f"  python {printed}\n"
                "\n"
                "No commit or tag was created. Any version metadata this "
                "script changed has been restored."
            )


# ----------------------------------------------------------------------
# Driving
# ----------------------------------------------------------------------
def _validate_forward(current: Version, target: Version) -> None:
    """Confirm ``target`` is a release after ``current``.

    Raises:
        ReleaseError: The target is the current version or older.
    """
    if target == current:
        raise ReleaseError(
            f"{target} is already the current version. A release must move it "
            "forward."
        )
    if target < current:
        raise ReleaseError(
            f"{target} is older than the current version {current}.\n"
            "Releasing backwards would make the published history "
            "non-monotonic. Choose a later version."
        )


def _print_header(current: Version, target: Version) -> None:
    """Show what is about to happen, before anything happens."""
    print("OMRFlow release automation")
    print()
    print(f"Current version   : {current}")
    print(f"Requested version : {target}")
    print(f"Target tag        : {target.tag}")
    print()


def _suggest_next(current: Version) -> Version:
    """A plausible next version, for the example in a usage message."""
    if current.stage is not None and current.number is not None:
        return Version(
            current.major, current.minor, current.patch, current.stage, current.number + 1
        )
    return Version(current.major, current.minor, current.patch + 1, None, None)


def prepare_release(repo_root: Path, target: Version, *, dry_run: bool) -> int:
    """Run the whole release preparation.

    Args:
        repo_root: The checkout.
        target: The version to release.
        dry_run: Check and report without changing anything.

    Returns:
        A process exit code.
    """
    current = get_current_version(repo_root)
    _print_header(current, target)

    total = 4 if dry_run else 8
    print(f"[1/{total}] Checking repository")
    check_remote(repo_root)
    branch = check_branch(repo_root)
    check_clean_tree(repo_root)
    print(f"  OK       branch {branch}, working tree clean")
    git(repo_root, "fetch", REMOTE, "--tags")
    check_synchronized(repo_root)
    print(f"  OK       synchronized with {REMOTE}/{DEFAULT_BRANCH}")

    print(f"[2/{total}] Validating version")
    _validate_forward(current, target)
    check_tag_absent(repo_root, target.tag)
    print(f"  OK       {current} -> {target}, {target.tag} is free")
    if not changelog_has_section(repo_root, target):
        print(f"  WARNING  CHANGELOG.md has no '## [{target}]' section yet")

    plan = plan_metadata_updates(repo_root, current, target, date.today())
    verify_replacements(repo_root, plan)
    files = sorted({item.path.as_posix() for item in plan})

    if dry_run:
        print(f"[3/{total}] Metadata (not written)")
        for item in plan:
            print(f"  {item.path.as_posix()}")
            print(f"    - {item.old}")
            print(f"    + {item.new}")
        print(f"[4/{total}] Would then")
        for name, command in RELEASE_GATES:
            print(f"  run      {name}: python {' '.join(command[1:])}")
        print(f"  commit   chore(release): {target.tag}")
        print(f"  tag      {target.tag} (annotated)")
        print(f"  push     {DEFAULT_BRANCH} and {target.tag} to {REMOTE}, atomically")
        print()
        print("DRY RUN COMPLETE")
        print("No files, commits, tags or remote refs were changed.")
        return 0

    print(f"[3/{total}] Preparing metadata")
    originals = {path: (repo_root / path).read_text(encoding="utf-8") for path in
                 {item.path for item in plan}}
    changed = apply_replacements(repo_root, plan)
    for path in changed:
        print(f"  updated  {path.as_posix()}")

    print(f"[4/{total}] Running release checks")
    try:
        run_release_gates(repo_root)
    except ReleaseError:
        # Put the tree back exactly as it was. A failed release must not leave
        # a half-bumped checkout behind for the next person to discover.
        for path, text in originals.items():
            (repo_root / path).write_text(text, encoding="utf-8")
        raise
    print("  OK       lint, types and the full suite passed")

    print(f"[5/{total}] Verifying release changes")
    actual = changed_paths(repo_root)
    if actual != files:
        raise ReleaseError(
            "The release would commit files it did not expect.\n"
            f"  expected: {', '.join(files)}\n"
            f"  found:    {', '.join(actual) or 'nothing'}\n"
            "Nothing has been committed. Investigate before retrying."
        )
    print(f"  OK       {len(actual)} file(s): {', '.join(actual)}")

    print(f"[6/{total}] Creating release commit")
    git(repo_root, "add", "--", *files)
    git(repo_root, "commit", "-m", f"chore(release): {target.tag}")
    commit = git(repo_root, "rev-parse", "HEAD")
    print(f"  OK       {commit[:12]}")

    print(f"[7/{total}] Creating tag")
    git(repo_root, "tag", "-a", target.tag, "-m", f"OMRFlow {target.tag}")
    print(f"  OK       {target.tag} (annotated)")

    print(f"[8/{total}] Pushing release")
    try:
        # Atomic: the branch and the tag arrive together or not at all, so a
        # network failure cannot publish a tag whose commit is missing.
        git(repo_root, "push", "--atomic", REMOTE, DEFAULT_BRANCH, target.tag)
    except ReleaseError as exc:
        raise ReleaseError(
            f"{exc}\n"
            "\n"
            "The push failed. Your local repository still holds:\n"
            f"  commit {commit[:12]}  chore(release): {target.tag}\n"
            f"  tag    {target.tag}\n"
            "\n"
            "Nothing was published, and nothing local was rewritten. Once the "
            "cause is fixed, retry exactly this:\n"
            f"  git push --atomic {REMOTE} {DEFAULT_BRANCH} {target.tag}\n"
            "\n"
            "To undo it locally instead:\n"
            f"  git tag -d {target.tag}\n"
            "  git reset --hard HEAD~1"
        ) from exc
    print(f"  OK       pushed {DEFAULT_BRANCH} and {target.tag}")

    print()
    print("OMRFlow release prepared successfully")
    print()
    print(f"Previous version : {current}")
    print(f"Release version  : {target}")
    print(f"Tag              : {target.tag}")
    print(f"Commit           : {commit}")
    print()
    print("The tag has been pushed. GitHub Actions will now:")
    print("  1. check the tag matches the version in the tagged source")
    print("  2. run lint, types and the full suite on Windows and Ubuntu")
    print("  3. build the installer and verify it")
    print("  4. write SHA256SUMS.txt")
    print("  5. publish the GitHub release")
    print()
    print("No GitHub release exists yet. Watch it at:")
    print("  https://github.com/sajidbuet/OMRflow/actions")
    print()
    print("Zenodo archives the release once GitHub publishes it.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The command line. See the module docstring for the design."""
    parser = argparse.ArgumentParser(
        prog="release.py",
        description="Prepare an OMRFlow release: check, bump, validate, commit, tag, push.",
    )
    parser.add_argument(
        "--version",
        dest="target",
        metavar="X.Y.Z[-alpha.N]",
        help="the version to release, without the tag's leading 'v'",
    )
    parser.add_argument(
        "--current-version",
        action="store_true",
        help="print the version the repository currently declares, and exit",
    )
    parser.add_argument(
        "--suggest-next-version",
        action="store_true",
        help="print a plausible next version, for use in a usage example",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="check everything and report, changing nothing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Returns:
        ``0`` on success or for a query, non-zero for usage, preflight, gate
        or push failures.
    """
    arguments = build_parser().parse_args(argv)
    try:
        repo_root = find_repository_root()

        if arguments.current_version:
            # Only the version, on one line: release.ps1 captures this.
            print(get_current_version(repo_root))
            return 0

        if arguments.suggest_next_version:
            # Also one bare line, so release.ps1 can put it in an example
            # without knowing how a version is spelled.
            print(_suggest_next(get_current_version(repo_root)))
            return 0

        if not arguments.target:
            current = get_current_version(repo_root)
            print("OMRFlow release automation")
            print()
            print(f"Current repository version: {current}")
            print()
            sys.stdout.flush()
            print("ERROR: A release version is required.", file=sys.stderr)
            sys.stderr.flush()
            print()
            print("Usage:")
            print("  python scripts/release.py --version <version> [--dry-run]")
            print()
            print("Example:")
            print(f"  python scripts/release.py --version {_suggest_next(current)}")
            return 2

        return prepare_release(
            repo_root, parse_version(arguments.target), dry_run=arguments.dry_run
        )
    except ReleaseError as exc:
        # Flush first: stdout is block-buffered when redirected while stderr is
        # not, so without this the error jumps ahead of the progress lines that
        # explain where it happened.
        sys.stdout.flush()
        print(file=sys.stderr)
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
