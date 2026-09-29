"""The open project's template, read from the project - never from a page.

Purpose:
    Answer "which template does this project use, and what does it say about
    the paper?" for any stage that needs the answer, from the one durable
    source there is: ``project.json``'s ``active_template`` and the ``.omrt``
    file it names.

Why this exists:
    The Answer Key stage used to learn the template from the Scan stage's
    ``template_changed`` signal. A project opened straight onto Answer Key, a
    Scan stage whose load had failed, or a template re-saved in place on the
    Template stage all left the key editor asking the operator to "load one on
    the Scan stage" - for a project that had a perfectly good template on
    disk. The rule is now::

        Project -> persisted active template -> every stage

    and this module is the "persisted active template" step.

What it distinguishes, because an operator fixes each one differently:
    * :attr:`TemplateAvailability.NONE` - the project has never named one.
    * :attr:`TemplateAvailability.MISSING` - it names one that is not on disk.
    * :attr:`TemplateAvailability.UNSUPPORTED` - written by a newer OMRFlow.
    * :attr:`TemplateAvailability.CORRUPTED` - unreadable or invalid.
    * :attr:`TemplateAvailability.NO_QUESTIONS` - valid, but no question regions.
    * :attr:`TemplateAvailability.INVALID_QUESTIONS` - question regions that
      cannot carry one key (gaps, duplicates, mixed option labels).
    * :attr:`TemplateAvailability.READY` - usable.

What does NOT belong here:
    Qt, and any decision about *which* template a project should use. That is
    :func:`omr_scanner.services.project_service.set_active_template`'s job.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from omr_scanner.domain.template import TEMPLATE_FORMAT_VERSION, OmrTemplate
from omr_scanner.errors import TemplateError
from omr_scanner.services.answer_key import AnswerKeyError, QuestionPlan, plan_for
from omr_scanner.services.project_service import (
    active_template_is_missing,
    resolve_active_template,
)
from omr_scanner.services.template_service import load_template
from omr_scanner.utils.json_io import read_json

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.domain.project import Project

_LOGGER = logging.getLogger(__name__)


class TemplateAvailability(StrEnum):
    """Whether the project's template can be used to write an answer key."""

    NONE = "none"
    MISSING = "missing"
    UNSUPPORTED = "unsupported"
    CORRUPTED = "corrupted"
    NO_QUESTIONS = "no_questions"
    INVALID_QUESTIONS = "invalid_questions"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class ProjectTemplate:
    """The project's template as far as it could be read.

    Attributes:
        availability: What was found.
        path: The recorded template file, when one is recorded.
        template: The loaded document, when it could be loaded - also for
            :attr:`TemplateAvailability.NO_QUESTIONS`, so a caller can still
            name it.
        plan: The question numbering and option labels, only when
            :attr:`availability` is :attr:`TemplateAvailability.READY`.
        message: One operator-facing sentence saying what is wrong and where
            to fix it. Empty when ready.
    """

    availability: TemplateAvailability
    path: Path | None = None
    template: OmrTemplate | None = None
    plan: QuestionPlan | None = None
    message: str = ""

    @property
    def is_ready(self) -> bool:
        """Whether an answer key can be written against this template."""
        return self.availability is TemplateAvailability.READY

    @property
    def fingerprint(self) -> str:
        """The template's geometry fingerprint, or ``""``."""
        return self.template.geometry_fingerprint() if self.template is not None else ""


NO_TEMPLATE_MESSAGE = (
    "This project does not yet have an active OMR template. Create or load one "
    "on the Template stage before defining answer keys."
)


def from_template(template: OmrTemplate, path: Path | None = None) -> ProjectTemplate:
    """Describe an already-loaded template the same way a project's would be.

    Used where a caller holds a template explicitly (a test, a tool) rather
    than through a project, so both routes report problems identically.
    """
    try:
        plan = plan_for(template)
    except AnswerKeyError as exc:
        no_questions = not any(
            getattr(zone.field, "question_count", 0) for zone in template.zones
        )
        return ProjectTemplate(
            TemplateAvailability.NO_QUESTIONS
            if no_questions
            else TemplateAvailability.INVALID_QUESTIONS,
            path=path,
            template=template,
            message=(
                "The active template contains no question regions. Add a "
                "Questions region on the Template stage before defining answer "
                "keys."
                if no_questions
                else exc.user_message or str(exc)
            ),
        )
    return ProjectTemplate(
        TemplateAvailability.READY, path=path, template=template, plan=plan
    )


def load_project_template(project: Project) -> ProjectTemplate:
    """Read the project's active template and say whether it can carry a key.

    Never raises: every failure becomes an availability and a message, because
    a project whose template is broken must still open - the Template stage is
    where it gets fixed.
    """
    path = resolve_active_template(project)
    if path is None:
        if active_template_is_missing(project):
            recorded = project.metadata.active_template or ""
            return ProjectTemplate(
                TemplateAvailability.MISSING,
                path=project.layout.resolve(Path(recorded)) if recorded else None,
                message=(
                    f"This project's template ({Path(recorded).name}) is no longer "
                    "in the project folder. Restore it, or choose another on the "
                    "Template stage."
                ),
            )
        return ProjectTemplate(TemplateAvailability.NONE, message=NO_TEMPLATE_MESSAGE)

    if _is_newer_format(path):
        return ProjectTemplate(
            TemplateAvailability.UNSUPPORTED,
            path=path,
            message=(
                f"This project's template ({path.name}) was written by a newer "
                "version of OMRFlow. Update OMRFlow to use it."
            ),
        )
    try:
        template = load_template(path)
    except TemplateError as exc:
        _LOGGER.warning("Project template %s could not be loaded: %s", path.name, exc)
        return ProjectTemplate(
            TemplateAvailability.CORRUPTED,
            path=path,
            message=(
                f"This project's template ({path.name}) could not be read: "
                f"{exc.user_message or exc} Open it on the Template stage to "
                "repair or replace it."
            ),
        )
    return from_template(template, path)


def _is_newer_format(path: Path) -> bool:
    """Whether ``path`` declares a template format newer than this build's."""
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    version = payload.get("format_version", TEMPLATE_FORMAT_VERSION)
    return isinstance(version, int) and version > TEMPLATE_FORMAT_VERSION


__all__ = [
    "NO_TEMPLATE_MESSAGE",
    "ProjectTemplate",
    "TemplateAvailability",
    "from_template",
    "load_project_template",
]
