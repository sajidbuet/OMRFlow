"""What a sheet should read, and how a dataset of them is described.

Purpose:
    Give synthetic and real validation data one shape, so that the benchmark
    harness, the synthetic generator and whatever a human eventually types by
    hand while checking a stack of real papers all speak the same language.

Responsibilities:
    * :class:`SheetGroundTruth` - the correct answers for one scan.
    * :class:`DatasetManifest` - what a dataset contains and how it was made.
    * Reading and writing both as JSON.

What does NOT belong here:
    * Comparison logic (:mod:`omr_scanner.evaluation.benchmark`) and generation
      (:mod:`omr_scanner.evaluation.synthetic_dataset`). This module is the
      vocabulary those two share.
    * Any real candidate data. Committed ground truth uses fictional roll
      numbers, and a real dataset lives outside the repository - see
      ``local_test_data/README.md``.

The answer convention, which is deliberately the engine's own:
    ``""`` means blank, ``"B"`` one mark, ``"B-D"`` two marks with both kept.
    Identical to
    :attr:`~omr_scanner.services.recognition_models.AnswerView.value`, so a
    comparison is a string equality and not a translation layer that could
    itself be wrong. Note this is the *value*, not the display form: a ``"?"``
    is something the engine says about its own uncertainty, never something
    ground truth asserts.

Why one schema for synthetic and real:
    The evaluator must not care where the data came from, or the day a real
    corpus arrives becomes the day a second evaluator is written and the two
    disagree. The only difference is that a real sheet records who verified it,
    and a synthetic one records the defects that were injected into it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GROUND_TRUTH_SCHEMA_VERSION = 1
"""Version of the documents this module reads and writes."""

BLANK = ""
"""How "no mark" is spelled in ground truth - the engine's own convention."""

MULTIPLE_SEPARATOR = "-"
"""Separator between the labels of a deliberately multiply-marked question."""


@dataclass(frozen=True, slots=True)
class SheetGroundTruth:
    """The correct reading of one scan.

    Attributes:
        scan: File name of the image this describes, without a directory, so a
            dataset can be moved without invalidating its ground truth.
        roll: The candidate identifier that should be read, or ``""`` when the
            sheet deliberately carries none.
        set_code: The question-paper set code that should be read, or ``""``.
        answers: Question number to expected value, using the convention in the
            module docstring. A question absent from this mapping is not
            checked - which is how a partially verified real sheet is recorded
            honestly rather than by guessing the rest.
        ambiguous: Question numbers whose mark is deliberately borderline - a
            faint pencil, a half-erased answer, a mark that barely touches the
            bubble. For these, *flagging* the question is correct behaviour and
            is scored as such; only a confident wrong answer counts against the
            engine. Without this distinction a dataset would punish the engine
            for the exact caution it is designed to show, and the pressure
            would be to lower thresholds until nothing is ever flagged.
        expect_failure: This sheet *should* fail to register. Used by the
            structural cases (a missing marker, a cropped page): counting them
            as recognition errors would punish the engine for correctly
            refusing to read an unreadable page.
        notes: Free text for a human.
        human_verified: A person checked this against the paper. Always false
            for synthetic data, which is generated rather than verified.
        reviewer: Who checked it, when anyone did.
        dataset_version: Which revision of the dataset this belongs to.
        metadata: Anything else worth recording - for synthetic sheets, exactly
            which defects were injected and with what parameters, which is what
            makes a failure reproducible.
    """

    scan: str
    roll: str = ""
    set_code: str = ""
    answers: dict[int, str] = field(default_factory=dict)
    ambiguous: tuple[int, ...] = ()
    expect_failure: bool = False
    notes: str = ""
    human_verified: bool = False
    reviewer: str = ""
    dataset_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_ambiguous(self, question: int) -> bool:
        """Whether ``question`` was deliberately marked borderline."""
        return question in self.ambiguous

    @property
    def blank_answers(self) -> tuple[int, ...]:
        """Question numbers that should read as blank."""
        return tuple(sorted(number for number, value in self.answers.items() if value == BLANK))

    @property
    def multiple_mark_answers(self) -> tuple[int, ...]:
        """Question numbers that should read as more than one mark."""
        return tuple(
            sorted(
                number
                for number, value in self.answers.items()
                if MULTIPLE_SEPARATOR in value
            )
        )

    def to_dict(self) -> dict[str, Any]:
        """Return this ground truth as JSON-safe plain data."""
        return {
            "schema_version": GROUND_TRUTH_SCHEMA_VERSION,
            "scan": self.scan,
            "roll": self.roll,
            "set_code": self.set_code,
            # JSON object keys are strings; the numbers come back on load.
            "answers": {str(number): value for number, value in sorted(self.answers.items())},
            "ambiguous": list(self.ambiguous),
            "expect_failure": self.expect_failure,
            "notes": self.notes,
            "human_verified": self.human_verified,
            "reviewer": self.reviewer,
            "dataset_version": self.dataset_version,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SheetGroundTruth:
        """Rebuild ground truth from :meth:`to_dict` output.

        Raises:
            ValueError: The document declares a newer schema version, or its
                answer keys are not question numbers.
        """
        version = int(payload.get("schema_version", GROUND_TRUTH_SCHEMA_VERSION))
        if version > GROUND_TRUTH_SCHEMA_VERSION:
            raise ValueError(
                f"Ground-truth schema version {version} is newer than this build "
                f"understands (max {GROUND_TRUTH_SCHEMA_VERSION})"
            )
        raw = payload.get("answers") or {}
        try:
            answers = {int(number): str(value) for number, value in raw.items()}
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Ground truth for '{payload.get('scan')}' has a non-numeric question key"
            ) from exc

        return cls(
            scan=str(payload.get("scan", "")),
            roll=str(payload.get("roll", "")),
            set_code=str(payload.get("set_code", "")),
            answers=answers,
            ambiguous=tuple(int(number) for number in payload.get("ambiguous") or ()),
            expect_failure=bool(payload.get("expect_failure", False)),
            notes=str(payload.get("notes", "")),
            human_verified=bool(payload.get("human_verified", False)),
            reviewer=str(payload.get("reviewer", "")),
            dataset_version=str(payload.get("dataset_version", "")),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    """What one validation dataset contains and how it came to exist.

    Attributes:
        name: Human readable dataset name.
        version: Dataset revision, so a benchmark result can say what it was
            measured against.
        created_at: ISO-8601 UTC timestamp.
        template: The ``.omrt`` the sheets were built from or printed from.
        generator: Everything needed to regenerate a synthetic dataset
            byte-for-byte: the seed, the profile, the count, the generator
            version. Empty for a real dataset, which cannot be regenerated -
            which is itself the reason a real dataset must be backed up rather
            than reproduced.
        entries: The ground-truth file names, in dataset order.
        notes: Free text.
    """

    name: str
    version: str = "1"
    created_at: str = ""
    template: str = ""
    generator: dict[str, Any] = field(default_factory=dict)
    entries: tuple[str, ...] = ()
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return this manifest as JSON-safe plain data."""
        return {
            "schema_version": GROUND_TRUTH_SCHEMA_VERSION,
            "name": self.name,
            "version": self.version,
            "created_at": self.created_at,
            "template": self.template,
            "generator": self.generator,
            "entries": list(self.entries),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DatasetManifest:
        """Rebuild a manifest from :meth:`to_dict` output."""
        return cls(
            name=str(payload.get("name", "")),
            version=str(payload.get("version", "1")),
            created_at=str(payload.get("created_at", "")),
            template=str(payload.get("template", "")),
            generator=dict(payload.get("generator") or {}),
            entries=tuple(payload.get("entries") or ()),
            notes=str(payload.get("notes", "")),
        )


def save_ground_truth(truth: SheetGroundTruth, path: Path) -> Path:
    """Write one ground-truth document, creating its folder if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(truth.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def load_ground_truth(path: Path) -> SheetGroundTruth:
    """Read one ground-truth document.

    Raises:
        ValueError: The file is not valid JSON, or not a valid document.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"'{path}' is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"'{path}' does not contain a ground-truth object")
    return SheetGroundTruth.from_dict(payload)


def load_ground_truth_directory(directory: Path) -> dict[str, SheetGroundTruth]:
    """Read every ``*.json`` in ``directory``, keyed by scan **stem**.

    Keyed by stem rather than file name so that a dataset regenerated as JPEG
    still matches ground truth written for PNG: the pairing is by sheet, not by
    encoding.

    Raises:
        FileNotFoundError: The directory does not exist.
    """
    if not directory.is_dir():
        raise FileNotFoundError(f"Ground-truth directory not found: {directory}")
    truths: dict[str, SheetGroundTruth] = {}
    for path in sorted(directory.glob("*.json")):
        truth = load_ground_truth(path)
        truths[Path(truth.scan or path.stem).stem] = truth
    return truths


def save_manifest(manifest: DatasetManifest, path: Path) -> Path:
    """Write a dataset manifest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def load_manifest(path: Path) -> DatasetManifest:
    """Read a dataset manifest.

    Raises:
        ValueError: The file is not valid JSON.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"'{path}' is not valid JSON: {exc}") from exc
    return DatasetManifest.from_dict(payload)


__all__ = [
    "BLANK",
    "GROUND_TRUTH_SCHEMA_VERSION",
    "MULTIPLE_SEPARATOR",
    "DatasetManifest",
    "SheetGroundTruth",
    "load_ground_truth",
    "load_ground_truth_directory",
    "load_manifest",
    "save_ground_truth",
    "save_manifest",
]
