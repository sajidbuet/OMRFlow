"""Storing and editing the examination sets a project defines.

Purpose:
    The persistence and validation behind *Project Configuration -> Sets*:
    create, list, edit, reorder and delete the
    :class:`~omr_scanner.domain.exam_sets.ExamSet` records that say which
    sets this examination is divided into and what each one means.

Responsibilities:
    * CRUD over the ``project_set`` table, returning domain
      :class:`~omr_scanner.domain.exam_sets.ExamSet` values rather than ORM
      rows, so nothing above this layer holds a live SQLAlchemy object.
    * Turning the pure validation rules in
      :mod:`omr_scanner.domain.exam_sets` - and the one rule that needs the
      whole project, uniqueness of a code - into a
      :class:`ProjectSetError` carrying a message a dialog can show
      unchanged.
    * :func:`references_to_set`, the single place that decides whether a set
      may be deleted.

What does NOT belong here:
    * Qt, dialogs or wording aimed at a specific screen.
    * The examination *name*. That lives in ``project.json`` with the rest of
      the project's identity - see
      :func:`omr_scanner.services.project_service.update_exam_name`.
    * Any change to how answer keys, scoring, reconciliation or reports
      behave. This module only reads those tables, and only in
      :func:`suggest_sets_from_existing_data`, which exists so the interface
      can *offer* codes a project already mentions rather than inventing
      registry entries behind the operator's back.

Ordering:
    Sets are returned by ``display_order`` and then ``code``, so a listing is
    deterministic even if two rows somehow share an order value.
    :func:`reorder_sets` renumbers from zero, which keeps the stored order
    dense and makes "move up" a swap rather than a rebalance.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select

from omr_scanner.database.models import AnswerKeyRevision, BatchScan, ProjectSet
from omr_scanner.domain.exam_sets import (
    ExamSet,
    find_conflicting_set,
    normalise_description,
    normalise_set_code,
    validate_set_code,
)
from omr_scanner.errors import OMRScannerError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from omr_scanner.database.engine import ProjectDatabase

_LOGGER = logging.getLogger(__name__)


class ProjectSetError(OMRScannerError):
    """A set could not be created, changed or removed as asked.

    Its own error rather than a :class:`~omr_scanner.errors.ProjectError`
    subclass, following the pattern Phase 7's ``CandidateImportError`` and
    Phase 9's ``ReportStoreError`` already set: a failure *inside* a service
    that merely belongs to a project is not a failure to open or create the
    project itself, and a caller catching one should not accidentally catch
    the other.
    """


def _now() -> datetime:
    return datetime.now(UTC)


def _new_set_id() -> str:
    """Return a fresh stable identifier for a set.

    ``uuid4().hex`` - the same 32-character form
    :func:`omr_scanner.services.batch_store.new_batch_id` already uses, so
    every stable identifier in a project database looks alike and none of
    them collide when two projects' data is compared side by side.
    """
    return uuid.uuid4().hex


def _to_domain(row: ProjectSet) -> ExamSet:
    return ExamSet(
        set_id=row.set_id,
        code=row.code,
        description=row.description,
        display_order=row.display_order,
    )


def list_sets(database: ProjectDatabase) -> tuple[ExamSet, ...]:
    """Return every set this project defines, in the operator's order.

    Args:
        database: The open project database.

    Returns:
        The sets, ordered by ``display_order`` then ``code``. Empty for a
        project that has not defined any - including every project created
        before this feature existed.
    """
    with database.session() as session:
        rows = (
            session.execute(
                select(ProjectSet).order_by(ProjectSet.display_order, ProjectSet.code)
            )
            .scalars()
            .all()
        )
        return tuple(_to_domain(row) for row in rows)


def get_set(database: ProjectDatabase, set_id: str) -> ExamSet | None:
    """Return one set by its stable identifier, or ``None``."""
    with database.session() as session:
        row = session.get(ProjectSet, set_id)
        return None if row is None else _to_domain(row)


def set_by_code(database: ProjectDatabase, code: str) -> ExamSet | None:
    """Return the set using ``code``, or ``None``.

    The lookup later phases will use to resolve a code printed on a sheet, or
    named by an answer key, to the set an operator defined.
    """
    wanted = normalise_set_code(code)
    with database.session() as session:
        row = session.execute(
            select(ProjectSet).where(ProjectSet.code == wanted)
        ).scalar_one_or_none()
        return None if row is None else _to_domain(row)


def add_set(database: ProjectDatabase, code: str, description: str = "") -> ExamSet:
    """Define a new set.

    Args:
        database: The open project database.
        code: The operator-visible code, trimmed and validated here.
        description: Free text; trimmed, otherwise unrestricted.

    Returns:
        The stored set, with the stable identifier that was generated for it.

    Raises:
        ProjectSetError: The code is blank or malformed, or another set
            already uses it. An existing set is never overwritten.
    """
    try:
        wanted = validate_set_code(code)
    except ValueError as exc:
        raise ProjectSetError(str(exc), user_message=str(exc)) from exc

    existing = list_sets(database)
    conflict = find_conflicting_set(wanted, existing)
    if conflict is not None:
        message = (
            f"Set '{wanted}' already exists in this project"
            f"{f' ({conflict.description})' if conflict.description else ''}. "
            "Set codes must be unique - choose a different code, or edit the "
            "existing set instead."
        )
        raise ProjectSetError(
            f"Duplicate set code {wanted!r}", user_message=message
        )

    record = ExamSet(
        set_id=_new_set_id(),
        code=wanted,
        description=normalise_description(description),
        display_order=_next_display_order(existing),
    )
    moment = _now()
    with database.session() as session:
        session.add(
            ProjectSet(
                set_id=record.set_id,
                code=record.code,
                description=record.description,
                display_order=record.display_order,
                created_at=moment,
                updated_at=moment,
            )
        )
    _LOGGER.info("Defined set %r (%s)", record.code, record.set_id)
    return record


def _next_display_order(existing: Sequence[ExamSet]) -> int:
    return max((item.display_order for item in existing), default=-1) + 1


def update_set(
    database: ProjectDatabase,
    set_id: str,
    *,
    code: str | None = None,
    description: str | None = None,
) -> ExamSet:
    """Change a set's code, its description, or both.

    Args:
        database: The open project database.
        set_id: The set to change, by its stable identifier - never by code,
            which is exactly the thing this call may be changing.
        code: New code, or ``None`` to leave it alone.
        description: New description, or ``None`` to leave it alone. Pass
            ``""`` to clear it.

    Returns:
        The set as stored after the change.

    Raises:
        ProjectSetError: No such set, the new code is malformed, or another
            set already uses the new code.

    :attr:`~omr_scanner.domain.exam_sets.ExamSet.set_id` never changes, which
    is the whole point of it existing: a code corrected from ``"1O"`` to
    ``"10"`` must not detach whatever a later phase has already linked to
    that set.
    """
    existing = list_sets(database)
    current = next((item for item in existing if item.set_id == set_id), None)
    if current is None:
        raise ProjectSetError(
            f"No set with id {set_id!r}",
            user_message="That set no longer exists in this project.",
        )

    new_code = current.code
    if code is not None:
        try:
            new_code = validate_set_code(code)
        except ValueError as exc:
            raise ProjectSetError(str(exc), user_message=str(exc)) from exc
        conflict = find_conflicting_set(new_code, existing, ignoring=set_id)
        if conflict is not None:
            message = (
                f"Set '{new_code}' already exists in this project. "
                "Set codes must be unique."
            )
            raise ProjectSetError(f"Duplicate set code {new_code!r}", user_message=message)

    new_description = (
        current.description if description is None else normalise_description(description)
    )

    with database.session() as session:
        row = session.get(ProjectSet, set_id)
        if row is None:  # pragma: no cover - re-checked inside the transaction
            raise ProjectSetError(
                f"No set with id {set_id!r}",
                user_message="That set no longer exists in this project.",
            )
        row.code = new_code
        row.description = new_description
        row.updated_at = _now()

    _LOGGER.info("Updated set %s (code %r)", set_id, new_code)
    return ExamSet(
        set_id=set_id,
        code=new_code,
        description=new_description,
        display_order=current.display_order,
    )


def references_to_set(database: ProjectDatabase, set_id: str) -> tuple[str, ...]:
    """Return plain-language descriptions of what depends on this set.

    Args:
        database: The open project database.
        set_id: The set being considered for deletion.

    Returns:
        One entry per kind of dependent record, empty when the set may be
        deleted freely.

    **Currently always empty, on purpose.** No table links to
    ``project_set.set_id`` yet: the phase that introduced the set registry
    was scoped to the registry alone, so attendance, candidates, scans,
    answer keys and results are all unchanged and none of them reference a
    set by its identifier.

    This function exists now, and :func:`delete_set` consults it now, so that
    the phase which *does* add those links has one obvious place to declare
    them - rather than having to find and retrofit a deletion path that by
    then will have been silently orphaning records for a release.
    """
    del database, set_id
    return ()


def delete_set(database: ProjectDatabase, set_id: str) -> None:
    """Remove a set from the project's registry.

    Args:
        database: The open project database.
        set_id: The set to remove.

    Raises:
        ProjectSetError: No such set, or something already depends on it (see
            :func:`references_to_set`).

    Deleting a set removes only the project's *definition* of it. Nothing
    that merely names the same code elsewhere - an answer key, a generated
    report, the set code recognition read off a sheet - is touched or
    invalidated, because none of those reference this table.
    """
    blockers = references_to_set(database, set_id)
    if blockers:
        listed = "; ".join(blockers)
        raise ProjectSetError(
            f"Set {set_id!r} is still referenced: {listed}",
            user_message=(
                "This set cannot be deleted because other records still refer "
                f"to it ({listed}). Remove or reassign those first."
            ),
        )

    with database.session() as session:
        row = session.get(ProjectSet, set_id)
        if row is None:
            raise ProjectSetError(
                f"No set with id {set_id!r}",
                user_message="That set no longer exists in this project.",
            )
        session.delete(row)
    _LOGGER.info("Deleted set %s", set_id)


def reorder_sets(database: ProjectDatabase, ordered_ids: Sequence[str]) -> tuple[ExamSet, ...]:
    """Store a new ordering for the project's sets.

    Args:
        database: The open project database.
        ordered_ids: Every set's identifier, in the wanted order.

    Returns:
        The sets as stored afterwards.

    Raises:
        ProjectSetError: ``ordered_ids`` is not exactly the project's current
            set of identifiers. Reordering is a permutation; accepting a
            partial list would silently decide an order for whatever was left
            out.
    """
    existing = list_sets(database)
    if sorted(ordered_ids) != sorted(item.set_id for item in existing):
        raise ProjectSetError(
            "reorder_sets requires every current set id exactly once",
            user_message="The set order could not be saved because the list has changed.",
        )

    moment = _now()
    with database.session() as session:
        rows = {row.set_id: row for row in session.execute(select(ProjectSet)).scalars()}
        for position, set_id in enumerate(ordered_ids):
            row = rows[set_id]
            if row.display_order != position:
                row.display_order = position
                row.updated_at = moment
    return list_sets(database)


def move_set(database: ProjectDatabase, set_id: str, offset: int) -> tuple[ExamSet, ...]:
    """Move one set up or down the list by ``offset`` positions.

    Args:
        database: The open project database.
        set_id: The set to move.
        offset: ``-1`` for one position earlier, ``+1`` for one later.

    Returns:
        The sets as stored afterwards. Moving past either end is a no-op
        rather than an error - a "move up" button on the first row should do
        nothing, not complain.

    Raises:
        ProjectSetError: No such set.
    """
    existing = list_sets(database)
    order = [item.set_id for item in existing]
    if set_id not in order:
        raise ProjectSetError(
            f"No set with id {set_id!r}",
            user_message="That set no longer exists in this project.",
        )

    index = order.index(set_id)
    target = index + offset
    if not 0 <= target < len(order):
        return existing

    order.insert(target, order.pop(index))
    return reorder_sets(database, order)


def replace_all_sets(
    database: ProjectDatabase, definitions: Sequence[tuple[str, str]]
) -> tuple[ExamSet, ...]:
    """Define this project's sets from scratch, in the order given.

    Args:
        database: The open project database.
        definitions: ``(code, description)`` pairs.

    Returns:
        The stored sets.

    Raises:
        ProjectSetError: Any code is blank, malformed or repeated within
            ``definitions``.

    Used when a project is first configured, where "these are the sets" is
    one decision rather than a sequence of additions. Every set is given a
    **new** stable identifier, so this is not a way to edit an existing
    registry - it refuses to run against one rather than quietly reissuing
    identifiers that other data may already point at.
    """
    if list_sets(database):
        raise ProjectSetError(
            "replace_all_sets called on a project that already defines sets",
            user_message=(
                "This project already defines sets. Add, edit or remove them "
                "individually instead."
            ),
        )

    validated: list[tuple[str, str]] = []
    seen: set[str] = set()
    for code, description in definitions:
        try:
            wanted = validate_set_code(code)
        except ValueError as exc:
            raise ProjectSetError(str(exc), user_message=str(exc)) from exc
        if wanted in seen:
            message = f"Set '{wanted}' is listed more than once. Set codes must be unique."
            raise ProjectSetError(f"Duplicate set code {wanted!r}", user_message=message)
        seen.add(wanted)
        validated.append((wanted, normalise_description(description)))

    moment = _now()
    with database.session() as session:
        for position, (code, description) in enumerate(validated):
            session.add(
                ProjectSet(
                    set_id=_new_set_id(),
                    code=code,
                    description=description,
                    display_order=position,
                    created_at=moment,
                    updated_at=moment,
                )
            )
    return list_sets(database)


def clear_sets(database: ProjectDatabase) -> None:
    """Remove every set definition. Used by tests and by a deliberate reset."""
    with database.session() as session:
        session.execute(delete(ProjectSet))


def suggest_sets_from_existing_data(database: ProjectDatabase) -> tuple[str, ...]:
    """Return set codes this project already mentions but has not defined.

    Args:
        database: The open project database.

    Returns:
        The codes, sorted, with anything already in the registry removed.

    Read-only, and offered rather than applied. A project that predates the
    set registry still names set codes in places that record *what happened* -
    an answer key that was entered, a set code recognition read off a sheet -
    and those are a useful starting point when an operator opens *Project
    Configuration* for the first time on an existing examination.

    They are deliberately not turned into registry entries automatically:
    they carry no description, and they are evidence of which sets were
    *processed*, not of which sets the examination was *meant* to have. A set
    whose papers were never scanned would be missing, and nothing in the data
    says so. The operator sees the suggestion and decides.
    """
    with database.session() as session:
        defined = set(session.execute(select(ProjectSet.code)).scalars())
        from_keys = {
            code
            for code in session.execute(
                select(AnswerKeyRevision.set_code).distinct()
            ).scalars()
            if code
        }
        from_scans = {
            code
            for code in session.execute(
                select(BatchScan.set_code_value)
                .where(func.trim(BatchScan.set_code_value) != "")
                .distinct()
            ).scalars()
            if code and code.strip()
        }
    candidates = {code.strip() for code in (from_keys | from_scans)} - defined
    return tuple(sorted(candidates))


__all__ = [
    "ProjectSetError",
    "add_set",
    "clear_sets",
    "delete_set",
    "get_set",
    "list_sets",
    "move_set",
    "references_to_set",
    "reorder_sets",
    "replace_all_sets",
    "set_by_code",
    "suggest_sets_from_existing_data",
    "update_set",
]
