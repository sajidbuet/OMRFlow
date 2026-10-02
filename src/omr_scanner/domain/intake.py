"""Intake sources and the intake ledger: the vocabulary (0.1.1 revised phase 5).

Purpose:
    Name the states, transitions, reasons and stabilisation rules of the intake
    ledger (``ARCHITECTURE_NOTES.md`` §10, roadmap phase 0.1.1-D) as plain
    values, so the service, Project Health and the tests apply one definition.

Responsibilities:
    * :class:`IntakeState` and :data:`TRANSITIONS` - the ledger state machine,
      with :func:`require_transition` refusing anything else.
    * :class:`IntakeReason` - why a row is where it is, stable and explainable.
    * :class:`SourceKind`, :class:`Reachability`, :class:`IngestMode`.
    * :class:`StabilityPolicy` - the stabilisation thresholds, as
      configuration (the values are starting points to measure, not
      validated constants).
    * :class:`Exclusions` and :func:`classify_name` - which observed files are
      ignored, and why.
    * :func:`due_for_verification` - when a stable-looking file may be read.

What does NOT belong here:
    Database, Qt, image or file access. :mod:`omr_scanner.services.intake`
    applies these rules.

Identity is not provenance:
    A source and a relative path say *where* bytes were seen. They never say
    whose script it is, and a path is never the identity of the content: the
    same ``(source, relative path)`` may later hold different bytes (a scanner
    counter reset), which is a new ledger row, not a change to the old one.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum

SOURCE_ENTITY = "intake_source"
"""``audit_event.entity_type`` for a source's configuration events."""

MANUAL_SOURCE_LABEL = "Manual import"
"""The built-in manual source's label (*Add Folder* / *Add Files*)."""


class SourceKind(StrEnum):
    """What kind of intake source a row describes."""

    MANUAL = "manual"
    """The built-in source *Add Folder* / *Add Files* record into. One per
    project, created on first use. Has no root folder: each file is recorded
    by the path the operator chose."""

    WATCHED = "watched"
    """A named folder (local or UNC) reconciled periodically."""


class Reachability(StrEnum):
    """Whether a source's folder could be listed at its last attempt.

    Source state only - never a statement about any one file. An unreachable
    source's files keep the state they had.
    """

    UNKNOWN = "unknown"
    """Never listed yet."""
    ONLINE = "online"
    UNREACHABLE = "unreachable"
    """Missing folder, disconnected share, network error."""
    PERMISSION_DENIED = "permission_denied"
    DISABLED = "disabled"
    """Disabled by the operator; not listed."""


class IngestMode(StrEnum):
    """How a registered file's bytes are held (ADR-0008)."""

    COPY = "copy"
    """Copied into the project after it is ready, the copy's hash verified; the
    project copy is what recognition and Resolve read. Watched sources."""

    REFERENCE = "reference"
    """Read in place, as before (ADR-0002). Manual local imports."""


class IntakeState(StrEnum):
    """Where one ledger row - one observed version of one file - stands."""

    DISCOVERED = "discovered"
    """Seen once by a reconciliation; nothing is trusted yet."""

    STABILIZING = "stabilizing"
    """Seen again, but not yet proven complete: still changing, inside its
    quiet period, empty, held open, or a decode failed and will be retried."""

    READY = "ready"
    """Proven complete: stable metadata over the quiet period, read once, the
    same bytes hashed and fully decoded, unchanged after the read. Safe to
    register. Not yet registered."""

    REGISTERED = "registered"
    """Registered: linked to the ``batch_scan`` it became. From here on the
    file's processing state is that row's status - never duplicated here."""

    DUPLICATE_CONTENT = "duplicate_content"
    """Registered, and its bytes repeat an effective sheet of the same scan
    session (phase 4's rule). Linked to that sheet; never recognised."""

    IGNORED = "ignored"
    """Not a candidate: unsupported suffix, temporary name, configured
    exclusion, or a re-observation of bytes the ledger already holds at the
    same path (``unchanged_content``)."""

    VANISHED = "vanished"
    """Gone before it was registered. Kept; reopened if it reappears."""

    UNREADABLE = "unreadable"
    """Stable, but never fully decodable after the bounded retries - an
    *intake* outcome, distinct from a recognition failure of a decodable image."""

    UNSUPPORTED = "unsupported"
    """Complete and decodable, but not admissible in this release: a
    multi-page TIFF (refused at file level, never split, never page 1 only)."""

    HELD = "held"
    """Ready, but intended for a scan session that is closed. Never registered
    into it; what to do is an operator decision (phase 7)."""

    @property
    def is_terminal(self) -> bool:
        """Whether no reconciliation moves the row on.

        A terminal row may still be followed by a *new* row for the same path.
        """
        return self in TERMINAL_STATES

    @property
    def is_unsettled(self) -> bool:
        """Whether reconciliation still observes the row to move it towards ready."""
        return self in (IntakeState.DISCOVERED, IntakeState.STABILIZING)

    @property
    def consumed_content(self) -> bool:
        """Whether the row's bytes entered the project (registered or linked).

        New content at its path afterwards is flagged ``path_reused``.
        """
        return self in (IntakeState.REGISTERED, IntakeState.DUPLICATE_CONTENT)


TERMINAL_STATES: frozenset[IntakeState] = frozenset(
    {
        IntakeState.REGISTERED,
        IntakeState.DUPLICATE_CONTENT,
        IntakeState.IGNORED,
        IntakeState.UNREADABLE,
        IntakeState.UNSUPPORTED,
        IntakeState.HELD,
    }
)

INITIAL_STATES: frozenset[IntakeState] = frozenset(
    {IntakeState.DISCOVERED, IntakeState.IGNORED, IntakeState.READY}
)
"""States a row may be created in. ``READY`` only for the manual source's
one-shot recording, whose readiness evidence is the operator's choice of a
finished file (see :data:`MANUAL_POLICY`)."""

_SETTLING = frozenset(
    {
        IntakeState.STABILIZING,
        IntakeState.READY,
        IntakeState.VANISHED,
        IntakeState.UNREADABLE,
        IntakeState.UNSUPPORTED,
        IntakeState.IGNORED,
    }
)

TRANSITIONS: dict[IntakeState, frozenset[IntakeState]] = {
    IntakeState.DISCOVERED: _SETTLING,
    IntakeState.STABILIZING: _SETTLING,
    IntakeState.READY: frozenset(
        {
            IntakeState.REGISTERED,
            IntakeState.DUPLICATE_CONTENT,
            IntakeState.STABILIZING,
            IntakeState.VANISHED,
            IntakeState.HELD,
        }
    ),
    IntakeState.REGISTERED: frozenset({IntakeState.DUPLICATE_CONTENT}),
    IntakeState.VANISHED: frozenset({IntakeState.DISCOVERED}),
    IntakeState.HELD: frozenset(),
    IntakeState.DUPLICATE_CONTENT: frozenset(),
    IntakeState.IGNORED: frozenset(),
    IntakeState.UNREADABLE: frozenset(),
    IntakeState.UNSUPPORTED: frozenset(),
}
"""Every allowed ``state -> next state``.

* ``registered -> duplicate_content``: registration commits first, then the
  phase 4 duplicate link (its own transaction) marks a copy; recovery
  completes it after a crash in between.
* ``vanished -> discovered``: a file that reappears is observed afresh.
* ``held`` has no exit in phase 5; releasing or discarding a held file is a
  phase 7 operator decision.
* Terminal rows never change state again. New bytes at their path are a new
  row (``path_reused`` when the old row's content was consumed).
"""


class IntakeTransitionError(ValueError):
    """A ledger transition outside :data:`TRANSITIONS` was attempted."""


def require_transition(current: IntakeState | None, target: IntakeState) -> None:
    """Refuse a transition the state machine does not allow.

    Args:
        current: The row's state, or ``None`` for a row being created.
        target: The state it would move to.

    Raises:
        IntakeTransitionError: The transition is not in :data:`TRANSITIONS`
            (or ``target`` is not an initial state for a new row). A
            same-state "transition" of an unsettled row is allowed: another
            observation.
    """
    if current is None:
        if target not in INITIAL_STATES:
            raise IntakeTransitionError(f"an intake row cannot be created as {target.value}")
        return
    if current is target and current.is_unsettled:
        return
    if target not in TRANSITIONS[current]:
        raise IntakeTransitionError(
            f"intake transition {current.value} -> {target.value} is not allowed"
        )


class IntakeReason(StrEnum):
    """Why a row is in its state. Stored as ``intake_file.state_reason``."""

    NONE = ""
    # --- ignored -----------------------------------------------------------
    UNSUPPORTED_SUFFIX = "unsupported_suffix"
    TEMPORARY_NAME = "temporary_name"
    EXCLUDED_NAME = "excluded_name"
    EXCLUDED_FOLDER = "excluded_folder"
    UNCHANGED_CONTENT = "unchanged_content"
    """Metadata changed, bytes did not: the ledger already holds this content
    at this path (see ``previous_intake_file_id``)."""
    # --- stabilising (not yet) ----------------------------------------------
    CHANGED = "changed"
    """Size or modification time changed since the last observation."""
    QUIET_PERIOD = "quiet_period"
    EMPTY_FILE = "empty_file"
    LOCKED = "locked"
    """Another program holds the file open without read sharing (a Windows
    sharing violation): not yet, not an error."""
    ACCESS_DENIED = "access_denied"
    READ_ERROR = "read_error"
    CHANGED_DURING_READ = "changed_during_read"
    DECODE_RETRY = "decode_retry"
    REVERIFY = "reverify"
    """Ready before a restart; re-read before it may be registered."""
    SOURCE_CHANGED = "source_changed"
    """Its bytes no longer match the verified hash (found while copying)."""
    # --- unreadable / unsupported -------------------------------------------
    DECODE_FAILED = "decode_failed"
    MULTIPAGE_TIFF = "multipage_tiff"
    # --- held ------------------------------------------------------------------
    SESSION_CLOSED = "session_closed"
    # --- vanished --------------------------------------------------------------
    DISAPPEARED = "disappeared"


IGNORE_REASONS: frozenset[IntakeReason] = frozenset(
    {
        IntakeReason.UNSUPPORTED_SUFFIX,
        IntakeReason.TEMPORARY_NAME,
        IntakeReason.EXCLUDED_NAME,
        IntakeReason.EXCLUDED_FOLDER,
    }
)
"""Reasons decided from the name alone, before any byte is read."""


class SourceAction(StrEnum):
    """``audit_event.action`` values for intake sources (20 characters at most)."""

    CREATED = "source_created"
    UPDATED = "source_updated"
    ENABLED = "source_enabled"
    DISABLED = "source_disabled"
    ATTACHED = "source_attached"
    DETACHED = "source_detached"


# ----------------------------------------------------------------------
# Stabilisation policy
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class StabilityPolicy:
    """When an observed file may be trusted as complete.

    **Starting values to measure, not validated defaults** (ARCHITECTURE_NOTES
    §10.4). Stored per source (``intake_source.policy_json``) so phases 6 and 8
    can tune them without a migration.

    Attributes:
        min_observations: Consecutive reconciliations that must see the same
            ``(size, mtime_ns)`` (*K*).
        quiet_seconds: How long that observation must have held (*T*).
        max_decode_attempts: Full reads that may fail to decode before the
            file is ``unreadable``. Changing metadata resets the count.
        retry_backoff_seconds: Wait after a failed decode before the next
            attempt; doubles per attempt.
        stall_after_seconds: A file still unsettled this long after it was
            first seen is reported as stalled (it changes no state).
        poll_interval_seconds: Advisory reconciliation interval for the phase 6
            engine. Phase 5 reconciles only when asked.
    """

    min_observations: int = 2
    quiet_seconds: float = 5.0
    max_decode_attempts: int = 3
    retry_backoff_seconds: float = 5.0
    stall_after_seconds: float = 600.0
    poll_interval_seconds: float = 10.0

    def __post_init__(self) -> None:
        """Refuse thresholds that would make readiness meaningless."""
        if self.min_observations < 1:
            raise ValueError("min_observations must be at least 1")
        if self.quiet_seconds < 0 or self.retry_backoff_seconds < 0:
            raise ValueError("quiet and backoff periods cannot be negative")
        if self.max_decode_attempts < 1:
            raise ValueError("max_decode_attempts must be at least 1")

    def to_json(self) -> str:
        """The stored form."""
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(
        cls, text: str | None, *, default: StabilityPolicy | None = None
    ) -> StabilityPolicy:
        """Read a stored policy; unknown keys are ignored, missing ones defaulted."""
        base = default or LOCAL_POLICY
        if not text:
            return base
        data = json.loads(text)
        known = {name: data[name] for name in asdict(base) if name in data}
        return replace(base, **known)

    def backoff(self, attempts: int) -> timedelta:
        """The wait after the ``attempts``-th failed decode."""
        return timedelta(seconds=self.retry_backoff_seconds * (2 ** max(0, attempts - 1)))


LOCAL_POLICY = StabilityPolicy()
"""Starting values for a local folder: *K* = 2, *T* = 5 s, poll 10 s."""

NETWORK_POLICY = StabilityPolicy(
    quiet_seconds=15.0, retry_backoff_seconds=15.0, poll_interval_seconds=30.0
)
"""Starting values for a UNC share: *T* = 15 s, poll 30 s. **Not** measured
on a real share - phase 10's SMB qualification settles them."""

MANUAL_POLICY = StabilityPolicy(min_observations=1, quiet_seconds=0.0)
"""The manual source's one-shot rule: the operator chose finished files and
pressed *Process All*, so one observation is the readiness evidence, and
decoding happens at recognition as it always has (a file that does not decode
there is a recognition failure, reported as before)."""


def is_network_path(path: str) -> bool:
    r"""Whether ``path`` names a UNC share (``\\server\share``)."""
    stripped = path.replace("/", "\\")
    if stripped.startswith("\\\\?\\UNC\\"):
        return True
    return stripped.startswith("\\\\") and not stripped.startswith("\\\\?\\")


def default_policy(root_path: str) -> StabilityPolicy:
    """The starting policy for a folder: network for UNC paths, local otherwise.

    A mapped drive letter pointing at a share is indistinguishable here and
    gets the local policy; its source can store the network policy explicitly.
    """
    return NETWORK_POLICY if is_network_path(root_path) else LOCAL_POLICY


def due_for_verification(
    *,
    observations: int,
    stable_since: datetime | None,
    retry_after: datetime | None,
    now: datetime,
    policy: StabilityPolicy,
) -> bool:
    """Whether an unsettled, non-empty row may now be read and verified.

    True only when the same ``(size, mtime_ns)`` has been observed at least
    ``min_observations`` times, the first of them at least ``quiet_seconds``
    ago, and no decode-retry back-off is pending. A row whose stability timer
    was reset (``stable_since`` ``None``, as after a restart) is never due.
    """
    if stable_since is None or observations < policy.min_observations:
        return False
    if now - stable_since < timedelta(seconds=policy.quiet_seconds):
        return False
    return retry_after is None or now >= retry_after


# ----------------------------------------------------------------------
# Names and exclusions
# ----------------------------------------------------------------------
TEMPORARY_SUFFIXES: tuple[str, ...] = (".tmp", ".part", ".partial", ".crdownload")
"""Suffixes writers use for a file still being written, then rename away."""


def is_temporary_name(name: str) -> bool:
    """``*.tmp``, ``*.part``, ``*.partial``, ``*.crdownload``, ``~*`` and ``.*``.

    Dot-files are treated as temporary for **watched** sources only
    (``ARCHITECTURE_NOTES`` §10.4): synchronisation tools and writers use them
    for in-flight copies (``.~lock``, ``._name``). The Windows *hidden*
    attribute is not consulted - existing manual import never did, and a
    scanner that sets it is not known.
    """
    lowered = name.lower()
    return lowered.startswith(("~", ".")) or lowered.endswith(TEMPORARY_SUFFIXES)


@dataclass(frozen=True, slots=True)
class Exclusions:
    """Configured, per-source exclusions (``fnmatch`` patterns, case-insensitive).

    Attributes:
        files: Patterns matched against the file name (``*_preview.jpg``).
        folders: Patterns matched against each folder name on the relative
            path, and against the relative folder path itself (``archive``,
            ``old/*``). A matching folder is not descended into.
    """

    files: tuple[str, ...] = ()
    folders: tuple[str, ...] = ()

    def to_json(self) -> str:
        """The stored form."""
        return json.dumps({"files": list(self.files), "folders": list(self.folders)})

    @classmethod
    def from_json(cls, text: str | None) -> Exclusions:
        """Read the stored form."""
        if not text:
            return cls()
        data = json.loads(text)
        return cls(
            files=tuple(str(item) for item in data.get("files", ())),
            folders=tuple(str(item) for item in data.get("folders", ())),
        )

    def excludes_folder(self, relative_folder: str) -> bool:
        """Whether a folder (``a/b``, relative to the source root) is excluded."""
        if not relative_folder:
            return False
        lowered = relative_folder.lower()
        parts = lowered.split("/")
        for pattern in self.folders:
            pattern = pattern.lower().strip("/")
            if fnmatch.fnmatchcase(lowered, pattern) or any(
                fnmatch.fnmatchcase(part, pattern) for part in parts
            ):
                return True
        return False

    def excludes_file(self, name: str) -> bool:
        """Whether a file name matches a configured pattern."""
        lowered = name.lower()
        return any(fnmatch.fnmatchcase(lowered, pattern.lower()) for pattern in self.files)


def is_hidden_folder(name: str) -> bool:
    """Dot-folders (``.sync``, ``.dropbox.cache``) are not descended into."""
    return name.startswith(".")


def classify_name(
    relative_path: str,
    *,
    exclusions: Exclusions,
    supported_suffixes: frozenset[str],
) -> IntakeReason:
    """Decide, from the path alone, whether an observed file is ignored.

    Args:
        relative_path: ``/``-separated path relative to the source root.
        exclusions: The source's configured exclusions.
        supported_suffixes: Lower-case suffixes OMRFlow decodes.

    Returns:
        An ignore reason, or :attr:`IntakeReason.NONE` for a candidate file.
        Checked in order: temporary name, configured file pattern, configured
        folder, unsupported suffix - so a ``scan.jpg.tmp`` reads "temporary".
    """
    folder, _, name = relative_path.rpartition("/")
    if is_temporary_name(name):
        return IntakeReason.TEMPORARY_NAME
    if exclusions.excludes_file(name):
        return IntakeReason.EXCLUDED_NAME
    if exclusions.excludes_folder(folder):
        return IntakeReason.EXCLUDED_FOLDER
    suffix = name[name.rfind(".") :].lower() if "." in name else ""
    if suffix not in supported_suffixes:
        return IntakeReason.UNSUPPORTED_SUFFIX
    return IntakeReason.NONE


@dataclass(frozen=True, slots=True)
class ObservedFile:
    """One file a listing returned (no content read).

    Attributes:
        relative_path: ``/``-separated, relative to the source root. Provenance
            and location, **not** identity.
        absolute_path: The path as observed, in the platform's form.
        size: Bytes.
        mtime_ns: Modification time, nanoseconds.
    """

    relative_path: str
    absolute_path: str
    size: int
    mtime_ns: int

    @property
    def signature(self) -> tuple[int, int]:
        """``(size, mtime_ns)`` - the observation cache key, never identity."""
        return (self.size, self.mtime_ns)


@dataclass(frozen=True, slots=True)
class SourceListing:
    """What one listing of a source returned.

    Attributes:
        files: Every file found (including ones that will be ignored).
        unlisted_folders: Relative folders that could not be listed. Rows
            under them were **not observed** this pass: their absence is not
            evidence they vanished.
        skipped_folders: Folders excluded by configuration or as dot-folders.
    """

    files: tuple[ObservedFile, ...]
    unlisted_folders: tuple[str, ...] = ()
    skipped_folders: tuple[str, ...] = field(default=())

    def covers(self, relative_path: str) -> bool:
        """Whether this listing could have seen ``relative_path``.

        False under an unlisted folder; there, absence proves nothing.
        """
        return not any(
            relative_path.startswith(f"{folder}/") for folder in self.unlisted_folders
        )


__all__ = [
    "IGNORE_REASONS",
    "INITIAL_STATES",
    "LOCAL_POLICY",
    "MANUAL_POLICY",
    "MANUAL_SOURCE_LABEL",
    "NETWORK_POLICY",
    "SOURCE_ENTITY",
    "TEMPORARY_SUFFIXES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "Exclusions",
    "IngestMode",
    "IntakeReason",
    "IntakeState",
    "IntakeTransitionError",
    "ObservedFile",
    "Reachability",
    "SourceAction",
    "SourceKind",
    "SourceListing",
    "StabilityPolicy",
    "classify_name",
    "default_policy",
    "due_for_verification",
    "is_hidden_folder",
    "is_network_path",
    "is_temporary_name",
    "require_transition",
]
