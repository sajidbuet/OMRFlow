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
    * Keeping ``project_set.canonical_code`` (migration 13) equal to the
      canonical form of each set's code, so the database's own unique index
      guards set identity even against a caller that skipped the checks here.
    * A set's optional *physical mark* - what the sheet prints for it when
      that is not its code - validated for ambiguity and, when the project's
      template is supplied, for whether the template can print it.
    * :func:`references_to_set`, the single place that decides whether a set
      may be deleted.

What does NOT belong here:
    * Qt, dialogs or wording aimed at a specific screen.
    * The examination *name*. That lives in ``project.json`` with the rest of
      the project's identity - see
      :func:`omr_scanner.services.project_service.update_exam_name`.
    * The identity rule itself - :mod:`omr_scanner.domain.set_identity`.
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
from sqlalchemy.exc import OperationalError

from omr_scanner.database.models import AnswerKeyRevision, BatchScan, ProjectSet
from omr_scanner.domain.exam_sets import (
    ExamSet,
    find_conflicting_mark,
    find_conflicting_set,
    normalise_description,
    validate_physical_mark,
    validate_set_code,
)
from omr_scanner.domain.set_identity import SetIdentity, canonical_code, distinct_codes
from omr_scanner.errors import DatabaseError, OMRScannerError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.domain.template import OmrTemplate

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
        physical_mark=row.physical_mark or "",
    )


def list_sets(database: ProjectDatabase) -> tuple[ExamSet, ...]:
    """Return every set this project defines, in the operator's order.

    Args:
        database: The open project database.

    Returns:
        The sets, ordered by ``display_order`` then ``code``. Empty for a
        project that has not defined any - including every project created
        before this feature existed.

    A project opened **read-only** is never migrated, so one written before
    migration 13 has no ``physical_mark`` column. Its sets are then read from
    the columns it does have, each printed as its own code - which is exactly
    what that project meant - rather than failing every stage that asks.
    """
    try:
        with database.session() as session:
            rows = (
                session.execute(
                    select(ProjectSet).order_by(ProjectSet.display_order, ProjectSet.code)
                )
                .scalars()
                .all()
            )
            return tuple(_to_domain(row) for row in rows)
    except DatabaseError as exc:
        if not (database.read_only and isinstance(exc.__cause__, OperationalError)):
            raise
        _LOGGER.info("project_set predates migration 13; reading its legacy columns")
    with database.session() as session:
        legacy = session.execute(
            select(
                ProjectSet.set_id, ProjectSet.code, ProjectSet.description, ProjectSet.display_order
            ).order_by(ProjectSet.display_order, ProjectSet.code)
        ).all()
        return tuple(
            ExamSet(set_id=set_id, code=code, description=description, display_order=order)
            for set_id, code, description, order in legacy
        )


def get_set(database: ProjectDatabase, set_id: str) -> ExamSet | None:
    """Return one set by its stable identifier, or ``None``."""
    with database.session() as session:
        row = session.get(ProjectSet, set_id)
        return None if row is None else _to_domain(row)


def set_by_code(database: ProjectDatabase, code: str) -> ExamSet | None:
    """Return the set whose logical code is ``code``, compared canonically, or ``None``.

    ``None`` too when two legacy sets collide on that code - see
    :meth:`~omr_scanner.domain.set_identity.SetIdentity.logical`.
    """
    return SetIdentity(list_sets(database)).logical(code)


def _validated_code(code: str) -> str:
    try:
        return validate_set_code(code)
    except ValueError as exc:
        raise ProjectSetError(str(exc), user_message=str(exc)) from exc


def _validated_mark(
    mark: str,
    code: str,
    existing: Sequence[ExamSet],
    *,
    ignoring: str | None,
    template: OmrTemplate | None,
) -> str:
    """Return the physical mark to store for a set with ``code``, or raise."""
    try:
        wanted = validate_physical_mark(mark)
    except ValueError as exc:
        raise ProjectSetError(str(exc), user_message=str(exc)) from exc
    if not wanted:
        return ""
    if canonical_code(wanted) == canonical_code(code):
        # Printed as its own code: no mapping to store.
        return ""
    conflict = find_conflicting_mark(wanted, code, tuple(existing), ignoring=ignoring)
    if conflict is not None:
        message = (
            f"'{wanted}' cannot be the printed mark for Set {code}: it already "
            f"names {conflict.label_with_mark}. A sheet reading '{wanted}' would "
            "then mean two sets."
        )
        raise ProjectSetError(f"Ambiguous physical mark {wanted!r}", user_message=message)
    if template is not None:
        from omr_scanner.services.answer_key import can_print_set_code, set_field_symbols

        if set_field_symbols(template) is not None and not can_print_set_code(template, wanted):
            message = (
                f"The template's set field cannot print '{wanted}'. Choose a mark "
                "the sheet's set-code bubbles can carry."
            )
            raise ProjectSetError(f"Unprintable physical mark {wanted!r}", user_message=message)
    return wanted


def _refresh_canonical_codes(session: Session) -> None:
    """Make every row's ``canonical_code`` the canonical form of its code.

    The first set of any colliding group, in the operator's order, holds the
    canonical code; any later one holds NULL (migration 13's rule). Cleared
    first and refilled after a flush, so the partial unique index never sees
    two rows claiming one value mid-update.
    """
    rows = session.scalars(
        select(ProjectSet).order_by(
            ProjectSet.display_order, ProjectSet.code, ProjectSet.set_id
        )
    ).all()
    wanted: dict[str, str | None] = {}
    taken: set[str] = set()
    for row in rows:
        value = canonical_code(row.code)
        wanted[row.set_id] = None if value in taken else value
        taken.add(value)
    if all(row.canonical_code == wanted[row.set_id] for row in rows):
        return
    for row in rows:
        row.canonical_code = None
    session.flush()
    for row in rows:
        row.canonical_code = wanted[row.set_id]
    session.flush()


def add_set(
    database: ProjectDatabase,
    code: str,
    description: str = "",
    *,
    physical_mark: str = "",
    template: OmrTemplate | None = None,
) -> ExamSet:
    """Define a new set.

    Args:
        database: The open project database.
        code: The operator-visible logical code, trimmed and validated here.
        description: Free text; trimmed, otherwise unrestricted.
        physical_mark: What the sheet prints for this set, when that is not
            ``code``; ``""`` for none.
        template: The project's template, when known, to check that the
            sheet's set field can print the mark.

    Returns:
        The stored set, with the stable identifier that was generated for it.

    Raises:
        ProjectSetError: The code is blank or malformed, or another set
            already uses it (compared canonically, so ``a`` when ``A``
            exists is refused); or the mark is malformed, names another set,
            or cannot be printed. An existing set is never overwritten.
    """
    wanted = _validated_code(code)
    existing = list_sets(database)
    conflict = find_conflicting_set(wanted, existing)
    if conflict is not None:
        message = (
            f"Set '{wanted}' already exists in this project as {conflict.label_with_mark}"
            f"{f' ({conflict.description})' if conflict.description else ''}. "
            "Set codes must be unique - and are compared without regard to case - "
            "so choose a different code, or edit the existing set instead."
        )
        raise ProjectSetError(
            f"Duplicate set code {wanted!r}", user_message=message
        )
    mark = _validated_mark(physical_mark, wanted, existing, ignoring=None, template=template)

    record = ExamSet(
        set_id=_new_set_id(),
        code=wanted,
        description=normalise_description(description),
        display_order=_next_display_order(existing),
        physical_mark=mark,
    )
    moment = _now()
    with database.session() as session:
        session.add(
            ProjectSet(
                set_id=record.set_id,
                code=record.code,
                description=record.description,
                display_order=record.display_order,
                physical_mark=record.physical_mark,
                created_at=moment,
                updated_at=moment,
            )
        )
        session.flush()
        _refresh_canonical_codes(session)
    _LOGGER.info(
        "Defined set %r (%s)%s",
        record.code,
        record.set_id,
        f" printed as {record.physical_mark!r}" if record.physical_mark else "",
    )
    return record


def _next_display_order(existing: Sequence[ExamSet]) -> int:
    return max((item.display_order for item in existing), default=-1) + 1


def update_set(
    database: ProjectDatabase,
    set_id: str,
    *,
    code: str | None = None,
    description: str | None = None,
    physical_mark: str | None = None,
    template: OmrTemplate | None = None,
) -> ExamSet:
    """Change a set's code, description or physical mark.

    Args:
        database: The open project database.
        set_id: The set to change, by its stable identifier - never by code,
            which is exactly the thing this call may be changing.
        code: New code, or ``None`` to leave it alone.
        description: New description, or ``None`` to leave it alone. Pass
            ``""`` to clear it.
        physical_mark: New printed mark, ``""`` to remove it, or ``None`` to
            leave it alone.
        template: The project's template, when known, to check printability.

    Returns:
        The set as stored after the change.

    Raises:
        ProjectSetError: No such set, the new code is malformed, or another
            set already uses the new code (canonically); or the mark is
            malformed, ambiguous or unprintable.

    :attr:`~omr_scanner.domain.exam_sets.ExamSet.set_id` never changes, which
    is the whole point of it existing: a code corrected from ``"1O"`` to
    ``"10"`` must not detach whatever a later phase has already linked to
    that set. Renaming one of two colliding legacy sets (``A`` / ``a``) is how
    an operator resolves the collision.
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
        new_code = _validated_code(code)
        # Keeping a colliding legacy set's own spelling (an edit of its
        # description only) is not a new conflict; any other code is checked.
        unchanged = new_code == current.code  # set-identity: exact (unchanged spelling)
        conflict = (
            None if unchanged else find_conflicting_set(new_code, existing, ignoring=set_id)
        )
        if conflict is not None:
            message = (
                f"Set '{new_code}' already exists in this project as "
                f"{conflict.label_with_mark}. Set codes must be unique, and are "
                "compared without regard to case."
            )
            raise ProjectSetError(f"Duplicate set code {new_code!r}", user_message=message)

    new_mark = current.physical_mark
    if physical_mark is not None or code is not None:
        new_mark = _validated_mark(
            current.physical_mark if physical_mark is None else physical_mark,
            new_code,
            existing,
            ignoring=set_id,
            template=template,
        )

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
        row.physical_mark = new_mark
        row.updated_at = _now()
        session.flush()
        _refresh_canonical_codes(session)

    _LOGGER.info("Updated set %s (code %r, mark %r)", set_id, new_code, new_mark)
    return ExamSet(
        set_id=set_id,
        code=new_code,
        description=new_description,
        display_order=current.display_order,
        physical_mark=new_mark,
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
        session.flush()
        # Deleting one of two colliding sets hands the canonical code to the
        # other.
        _refresh_canonical_codes(session)
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

    The order of two colliding legacy sets does **not** move the canonical
    code from one to the other: reordering is not a decision about identity,
    and nothing should change which of them is treated as holding it.
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
            ``definitions`` (canonically: ``A`` and ``a`` are one code).

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
        wanted = _validated_code(code)
        if canonical_code(wanted) in seen:
            message = (
                f"Set '{wanted}' is listed more than once. Set codes must be unique, "
                "and are compared without regard to case."
            )
            raise ProjectSetError(f"Duplicate set code {wanted!r}", user_message=message)
        seen.add(canonical_code(wanted))
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
                    canonical_code=canonical_code(code),
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
        The codes, sorted, with anything already in the registry removed -
        compared canonically, and as printed marks too, so a sheet reading
        ``A`` for a Set 10 printed as ``A`` is not offered as a new set.

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
    identity = SetIdentity(list_sets(database))
    with database.session() as session:
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
    candidates = distinct_codes(
        sorted(code.strip() for code in (from_keys | from_scans))
    )
    return tuple(
        sorted(
            code
            for code in candidates
            if identity.for_reading(code) is None and not identity.is_ambiguous(code)
        )
    )


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
