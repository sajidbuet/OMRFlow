"""Reading and writing ``.omrt`` template documents.

Purpose:
    The I/O half of the template feature: turn a file into a validated
    :class:`omr_scanner.domain.template.OmrTemplate` and back again.

Responsibilities:
    * Translate file and validation failures into
      :class:`omr_scanner.errors.TemplateError`.
    * Enforce the format magic string and refuse documents from a newer format
      version.

What does NOT belong here:
    * The document *shape* and its rules (:mod:`omr_scanner.domain.template`).
    * Any editing operation. The template designer is Phase 2; this module only
      loads and saves.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from omr_scanner.domain.template import (
    TEMPLATE_FILE_SUFFIX,
    TEMPLATE_FORMAT_VERSION,
    OmrTemplate,
)
from omr_scanner.errors import TemplateError
from omr_scanner.utils.json_io import read_json, write_json_atomic

logger = logging.getLogger(__name__)


def load_template(path: Path) -> OmrTemplate:
    """Load and validate a template document.

    Args:
        path: ``.omrt`` file to read.

    Returns:
        The validated template.

    Raises:
        TemplateError: The file is missing, unreadable, not JSON, not an OMRFlow
            template, written in a newer format version, or semantically invalid
            (for example a bubble grid extending outside its zone).
    """
    if not path.is_file():
        raise TemplateError(
            f"Template not found: {path}",
            user_message="The template file could not be found.",
        )

    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise TemplateError(
            f"Could not read template {path}: {exc}",
            user_message="The template file is damaged and could not be read.",
        ) from exc

    if isinstance(payload, dict):
        stored_version = payload.get("format_version", TEMPLATE_FORMAT_VERSION)
        if isinstance(stored_version, int) and stored_version > TEMPLATE_FORMAT_VERSION:
            raise TemplateError(
                f"Template format version {stored_version} is newer than supported "
                f"version {TEMPLATE_FORMAT_VERSION}",
                user_message=(
                    "This template was created with a newer version of OMRFlow. "
                    "Please update OMRFlow to use it."
                ),
            )

    try:
        template = OmrTemplate.model_validate(payload)
    except ValidationError as exc:
        raise TemplateError(
            f"Invalid template {path}: {exc}",
            user_message="The template file is not valid.",
        ) from exc

    logger.debug("Loaded template '%s' from %s", template.name, path)
    return template


def save_template(template: OmrTemplate, path: Path) -> Path:
    """Write a template document.

    Args:
        template: Validated template to store.
        path: Destination file. The ``.omrt`` suffix is appended when missing.

    Returns:
        The path actually written.

    Raises:
        TemplateError: The file could not be written.
    """
    destination = path if path.suffix == TEMPLATE_FILE_SUFFIX else path.with_suffix(
        TEMPLATE_FILE_SUFFIX
    )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(destination, template.model_dump(mode="json"))
    except OSError as exc:
        raise TemplateError(
            f"Could not write template {destination}: {exc}",
            user_message="The template could not be saved.",
        ) from exc

    logger.info("Saved template '%s' to %s", template.name, destination)
    return destination


def list_templates(directory: Path) -> tuple[Path, ...]:
    """Return the ``.omrt`` files in ``directory``, sorted by name.

    A missing directory yields an empty result rather than an error: a project
    simply has no templates yet.
    """
    if not directory.is_dir():
        return ()
    return tuple(sorted(directory.glob(f"*{TEMPLATE_FILE_SUFFIX}")))
