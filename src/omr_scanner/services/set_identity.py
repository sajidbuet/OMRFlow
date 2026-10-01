"""The open project's examination-set identity: the one authority callers use.

Purpose:
    Load a project's sets as a :class:`~omr_scanner.domain.set_identity.SetIdentity`
    and answer, for any stage, "which logical set does this code name?" - the
    question every set-dependent stage (Resolve, Attendance, Reject & Rescan,
    Answer Key, scoring, Results, Reports, Project Health) used to answer for
    itself with its own mixture of exact matches and ``.upper()``.

Responsibilities:
    * :func:`load` - the project's :class:`SetIdentity`.
    * Re-exporting the pure rule (:func:`canonical_code`, :func:`same_set`,
      :class:`SetCodeMap`) so a service or a page imports set identity from
      one place.
    * :func:`require_no_collision` - the refusal every set-dependent operation
      applies while two legacy sets share a canonical code.
    * :func:`describe_reading` - ``"Set 10 (A on sheet)"`` for display.

What does NOT belong here:
    * The rule itself, which is pure and lives in
      :mod:`omr_scanner.domain.set_identity` so that the schema migration can
      use it too (the database layer may import the domain, never services).
    * Recognition. What is printed on the sheet is read exactly as before; the
      translation to a logical set happens at one boundary,
      :func:`omr_scanner.services.review_store.effective_set_codes`.
    * Qt.

The translation boundary:
    ``batch_scan.set_code_value`` and the review ledger keep what was read or
    corrected **on the paper**. ``review_store.effective_set_codes`` assembles
    that physical value after every Resolve-stage decision and translates it
    with :meth:`SetIdentity.logical_for_physical`; its
    :attr:`~omr_scanner.services.review_store.EffectiveIdentifier.value` is the
    logical set and :attr:`~omr_scanner.services.review_store.EffectiveIdentifier.as_read`
    the paper's value. Reconciliation, scoring, results and reports read the
    effective value, so none of them interprets physical marks itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omr_scanner.domain.set_identity import (
    SetCodeMap,
    SetCollision,
    SetIdentity,
    canonical_code,
    distinct_codes,
    find_collisions,
    group_by_set,
    same_set,
)
from omr_scanner.errors import OMRScannerError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

    from omr_scanner.database.engine import ProjectDatabase


class SetCollisionError(OMRScannerError):
    """A set-dependent operation was refused because two sets share one identity."""


def load(database: ProjectDatabase) -> SetIdentity:
    """The open project's sets, as a :class:`SetIdentity`.

    Read fresh each call - the set list changes from Project Configuration at
    any time - and cheap: one small table.
    """
    from omr_scanner.services import project_sets

    return SetIdentity(project_sets.list_sets(database))


def collisions(database: ProjectDatabase) -> tuple[SetCollision, ...]:
    """Every group of the project's sets whose codes canonicalise alike."""
    return load(database).collisions


def describe_collisions(found: Iterable[SetCollision]) -> str:
    """One paragraph naming every collision and what to do about it."""
    sentences = [item.describe() for item in found]
    if not sentences:
        return ""
    return (
        " ".join(sentences)
        + " Rename or remove one of each in Project Configuration -> Sets; "
        "OMRFlow will not choose between them."
    )


def require_no_collision(
    identity: SetIdentity, *, purpose: str, codes: Iterable[str] | None = None
) -> None:
    """Refuse a set-dependent operation while a relevant collision exists.

    Args:
        identity: The project's sets.
        purpose: What was refused, for the message - ``"Calculate Results"``.
        codes: The sets the operation concerns. ``None`` means every set
            (the operation is project-wide, like scoring a batch).

    Raises:
        SetCollisionError: A collision involves one of ``codes`` (or any set).
    """
    found = identity.collisions
    if codes is not None:
        wanted = {canonical_code(code) for code in codes}
        found = tuple(item for item in found if item.canonical in wanted)
    if not found:
        return
    raise SetCollisionError(
        f"{purpose} refused: colliding set codes "
        + ", ".join(item.canonical for item in found),
        user_message=f"{purpose} is not possible yet. " + describe_collisions(found),
    )


def describe_reading(identity: SetIdentity, logical: str, *, as_read: str = "") -> str:
    """``"Set 10 (A on sheet)"``, ``"Set A"``, or ``"No set"`` for display."""
    return identity.describe(logical, as_read=as_read)


__all__ = [
    "SetCodeMap",
    "SetCollision",
    "SetCollisionError",
    "SetIdentity",
    "canonical_code",
    "collisions",
    "describe_collisions",
    "describe_reading",
    "distinct_codes",
    "find_collisions",
    "group_by_set",
    "load",
    "require_no_collision",
    "same_set",
]
