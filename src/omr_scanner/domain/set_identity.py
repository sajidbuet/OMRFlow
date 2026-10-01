"""What makes two set codes the same examination set.

Purpose:
    The single rule for examination-set identity. Every comparison of one set
    code with another - a code an operator types, a code recognition read off
    a sheet, the code an answer key or a result was stored under - is decided
    here, so that ``"a"``, ``" A "`` and the full-width capital A (U+FF21)
    are one set everywhere or nowhere.

Responsibilities:
    * :func:`canonical_code` - the canonical logical form of a code:
      ``unicodedata.normalize("NFKC", code).strip().upper()``.
    * :func:`same_set` - whether two codes name one set.
    * :class:`SetCodeMap` - a read-only mapping keyed by canonical code, for
      the places that look a set up in a dictionary (verified keys, results
      grouped by set, template associations).
    * :class:`SetIdentity` - a project's sets with their optional *physical
      marks*: the translation from what is printed on the paper to the logical
      set, and back.
    * :func:`find_collisions` - existing sets whose codes now canonicalise
      alike, which are reported and never merged.

What does NOT belong here:
    * Database, file or Qt access. :mod:`omr_scanner.services.set_identity`
      loads a :class:`SetIdentity` from a project; :mod:`omr_scanner.database.migrations`
      uses :func:`canonical_code` to fill ``project_set.canonical_code``.
    * Whether a mark can be printed on a particular template. That needs the
      template's set field and lives in
      :func:`omr_scanner.services.answer_key.can_print_set_code`.

Logical code and physical mark:
    A set's *code* is its logical identity - ``"10"`` in ``Set 10 - Assistant
    Engineer``. Its optional *physical mark* is the symbol printed (and read)
    on the sheet when that differs - an ``A``-``D`` set field on paper whose
    sets the examination calls 10, 11, 12. Recognition keeps reading what is
    on the paper; :meth:`SetIdentity.logical_for_physical` is the translation,
    applied at exactly one boundary (the effective-set derivation in
    :func:`omr_scanner.services.review_store.effective_set_codes`).

Why canonicalisation is a string rule and never a number rule:
    Set codes are identifiers. ``"05"`` and ``"5"`` are two different codes -
    an examination may print both - so nothing here parses digits. NFKC only
    folds *compatibility* forms (full-width letters and digits, ligatures)
    onto their ordinary spelling; it never removes a leading zero.

Ambiguity rule:
    Within a project every *token* - each set's canonical code and each set's
    canonical physical mark - belongs to at most one set. That is what makes
    the translation from a reading to a set unambiguous: ``10 -> A`` and a
    second set whose own code is ``A`` cannot both exist, and neither can two
    sets printed as the same mark. A project created before this rule may
    already hold two sets whose codes collide (``A`` and ``a``); they are kept,
    reported by :func:`find_collisions`, and every reading of the shared token
    resolves to *no* set until an operator renames one.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.domain.exam_sets import ExamSet


def canonical_code(code: str) -> str:
    """Return the canonical logical form of a set code.

    ``unicodedata.normalize("NFKC", code).strip().upper()``: compatibility
    forms folded, surrounding whitespace removed, upper case. ``"a"``,
    ``" A "`` and the full-width capital A (U+FF21) all become ``"A"``;
    ``"05"`` stays ``"05"``.
    """
    return unicodedata.normalize("NFKC", code).strip().upper()


def same_set(first: str, second: str) -> bool:
    """Whether two set codes name the same logical set."""
    return canonical_code(first) == canonical_code(second)


class SetCodeMap[V](Mapping[str, V]):
    """A read-only mapping whose keys are set codes, compared canonically.

    ``mapping["a"]`` finds what was stored under ``"A"``. Iteration yields the
    spelling each entry was first stored under, so a listing still shows a
    code the way it was written.

    When two inputs canonicalise alike, the **first** is kept and the later
    one is recorded in :attr:`shadowed` rather than silently overwriting it;
    a caller that must not choose between them checks that.
    """

    __slots__ = ("_items", "_spelling", "shadowed")

    def __init__(self, items: Iterable[tuple[str, V]] = ()) -> None:
        self._items: dict[str, V] = {}
        self._spelling: dict[str, str] = {}
        self.shadowed: tuple[str, ...] = ()
        shadowed: list[str] = []
        for code, value in items:
            key = canonical_code(code)
            if key in self._items:
                shadowed.append(code)
                continue
            self._items[key] = value
            self._spelling[key] = code
        self.shadowed = tuple(shadowed)

    def __getitem__(self, code: str) -> V:
        """The value stored under ``code``, compared canonically."""
        return self._items[canonical_code(code)]

    def __contains__(self, code: object) -> bool:
        """Whether a value is stored under ``code``, compared canonically."""
        return isinstance(code, str) and canonical_code(code) in self._items

    def __iter__(self) -> Iterator[str]:
        """The stored codes, each in the spelling it was first stored under."""
        return iter(self._spelling.values())

    def __len__(self) -> int:
        """How many logical sets are stored."""
        return len(self._items)

    def __repr__(self) -> str:
        """Return a debugging representation listing the stored spellings."""
        pairs = dict(zip(self._spelling.values(), self._items.values(), strict=True))
        return f"SetCodeMap({pairs!r})"


def group_by_set[V](items: Iterable[tuple[str, V]]) -> SetCodeMap[list[V]]:
    """Group values by set code, canonically: ``"a"`` and ``"A"`` share a list."""
    grouped: dict[str, tuple[str, list[V]]] = {}
    for code, value in items:
        key = canonical_code(code)
        grouped.setdefault(key, (code, []))[1].append(value)
    return SetCodeMap(grouped.values())


def distinct_codes(codes: Iterable[str]) -> tuple[str, ...]:
    """The codes with canonical duplicates removed, first spelling kept, in order."""
    seen: dict[str, str] = {}
    for code in codes:
        seen.setdefault(canonical_code(code), code)
    return tuple(seen.values())


@dataclass(frozen=True, slots=True)
class SetCollision:
    """Two or more defined sets whose codes canonicalise to one identity.

    Attributes:
        canonical: The shared canonical code, ``"A"`` for ``A`` and ``a``.
        sets: The colliding sets, in the operator's order.
    """

    canonical: str
    sets: tuple[ExamSet, ...]

    def describe(self) -> str:
        """``"Sets 'A' and 'a' are the same set code (A)"``-style sentence."""
        names = [f"'{item.code}'" for item in self.sets]
        listed = ", ".join(names[:-1]) + f" and {names[-1]}"
        return (
            f"Sets {listed} are now the same set code ({self.canonical}): set "
            "codes are compared without regard to case or width."
        )


def find_collisions(sets: Iterable[ExamSet]) -> tuple[SetCollision, ...]:
    """Every group of sets whose codes canonicalise alike, in set order.

    Empty for every project that never defined two codes differing only in
    case, width or surrounding space.
    """
    groups: dict[str, list[ExamSet]] = {}
    for item in sets:
        groups.setdefault(canonical_code(item.code), []).append(item)
    return tuple(
        SetCollision(canonical=key, sets=tuple(members))
        for key, members in groups.items()
        if len(members) > 1
    )


class SetIdentity:
    """A project's sets, and the translation between paper and logical set.

    Args:
        sets: The project's defined sets, in the operator's order.

    Immutable once built. Cheap: a project has tens of sets.
    """

    __slots__ = ("_by_code", "_by_token", "collisions", "sets")

    def __init__(self, sets: Iterable[ExamSet]) -> None:
        self.sets: tuple[ExamSet, ...] = tuple(sets)
        self.collisions: tuple[SetCollision, ...] = find_collisions(self.sets)
        by_code: dict[str, list[ExamSet]] = {}
        by_token: dict[str, list[ExamSet]] = {}
        for item in self.sets:
            by_code.setdefault(canonical_code(item.code), []).append(item)
            for token in _tokens(item):
                owners = by_token.setdefault(token, [])
                if item not in owners:
                    owners.append(item)
        self._by_code = by_code
        self._by_token = by_token

    # ------------------------------------------------------------------
    # Questions about the project
    # ------------------------------------------------------------------
    @property
    def has_sets(self) -> bool:
        """Whether the project defines any set at all."""
        return bool(self.sets)

    @property
    def codes(self) -> tuple[str, ...]:
        """Every set's code as the operator spelled it, in their order."""
        return tuple(item.code for item in self.sets)

    @property
    def has_physical_marks(self) -> bool:
        """Whether any set is printed on the sheet as something other than its code."""
        return any(item.physical_mark for item in self.sets)

    def colliding_sets(self) -> tuple[ExamSet, ...]:
        """Every set involved in a collision."""
        return tuple(item for collision in self.collisions for item in collision.sets)

    def is_colliding(self, code: str) -> bool:
        """Whether ``code`` is one of the colliding identities."""
        return len(self._by_code.get(canonical_code(code), ())) > 1

    # ------------------------------------------------------------------
    # Logical lookups
    # ------------------------------------------------------------------
    def logical(self, code: str) -> ExamSet | None:
        """The set whose *logical* code is ``code``, compared canonically.

        ``None`` for an unknown code, and for a code two sets collide on -
        choosing one of them would be a silent guess.
        """
        found = self._by_code.get(canonical_code(code), ())
        return found[0] if len(found) == 1 else None

    def is_defined(self, code: str) -> bool:
        """Whether ``code`` is the logical code of exactly one defined set."""
        return self.logical(code) is not None

    def by_id(self, set_id: str) -> ExamSet | None:
        """The set with this stable identifier, or ``None``."""
        return next((item for item in self.sets if item.set_id == set_id), None)

    # ------------------------------------------------------------------
    # Paper <-> logical
    # ------------------------------------------------------------------
    def for_reading(self, raw: str) -> ExamSet | None:
        """The set a value read off (or corrected on) the paper names.

        A physical mark resolves to its set; so does a set's own logical code
        (a set without a mark prints its code, and a reviewer may type the
        logical code). ``None`` when the value names no set or is ambiguous.
        """
        owners = self._by_token.get(canonical_code(raw), ())
        return owners[0] if len(owners) == 1 else None

    def is_ambiguous(self, raw: str) -> bool:
        """Whether ``raw`` names more than one set (a collision)."""
        return len(self._by_token.get(canonical_code(raw), ())) > 1

    def logical_for_physical(self, raw: str) -> str:
        """The logical code a paper value stands for, or ``raw`` unchanged.

        ``"A"`` becomes ``"10"`` when Set 10 is printed as ``A``; ``"a"``
        becomes ``"A"`` for a set whose code is ``A``. A value that names no
        defined set - or more than one - is returned exactly as given, so that
        it is reported as undefined rather than quietly reassigned.
        """
        found = self.for_reading(raw)
        return found.code if found is not None else raw

    def physical_for_logical(self, code: str) -> str:
        """What is printed on the paper for logical set ``code``.

        The set's physical mark when it has one, else its own code. A code
        that names no set is returned unchanged.
        """
        found = self.logical(code)
        if found is None:
            return code
        return found.printed_as

    def describe(self, code: str, *, as_read: str = "") -> str:
        """``"Set 10 (A on sheet)"`` for a mapped set, ``"Set A"`` otherwise.

        Args:
            code: A logical code (or a reading, which is resolved first).
            as_read: What the paper actually reads, when known; shown when it
                differs from the logical code even for an unmapped set
                (``"Set A (a on sheet)"`` is not shown - case alone is not a
                difference worth the operator's attention).
        """
        found = self.logical(code) or self.for_reading(code)
        if found is None:
            return f"Set {code}" if code else "No set"
        printed = as_read or found.printed_as
        if printed and not same_set(printed, found.code):
            return f"Set {found.code} ({printed} on sheet)"
        return f"Set {found.code}"


def _tokens(item: ExamSet) -> tuple[str, ...]:
    """The canonical values that name ``item``: its code and its mark."""
    tokens = [canonical_code(item.code)]
    if item.physical_mark:
        mark = canonical_code(item.physical_mark)
        if mark not in tokens:
            tokens.append(mark)
    return tuple(tokens)


def token_owner(
    token: str, sets: Iterable[ExamSet], *, ignoring: str | None = None
) -> ExamSet | None:
    """The set (other than ``ignoring``) whose code or mark is ``token``, if any.

    The uniqueness check :mod:`omr_scanner.domain.exam_sets` applies to a new
    code and to a new physical mark alike: within a project a token may name
    only one set, or a reading of it could not be translated unambiguously.
    """
    wanted = canonical_code(token)
    for item in sets:
        if ignoring is not None and item.set_id == ignoring:
            continue
        if wanted in _tokens(item):
            return item
    return None


__all__ = [
    "SetCodeMap",
    "SetCollision",
    "SetIdentity",
    "canonical_code",
    "distinct_codes",
    "find_collisions",
    "group_by_set",
    "same_set",
    "token_owner",
]
