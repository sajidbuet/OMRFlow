"""Reading and writing the small JSON documents OMRFlow owns.

Purpose:
    ``project.json``, the application configuration file and ``.omrt`` templates
    are all human-readable JSON that users may edit or diff. This module is the
    single place that decides *how* such files are encoded on disk.

Responsibilities:
    * UTF-8, ``indent=2``, trailing newline, stable key order as produced by the
      caller (no alphabetical re-sorting - template field order is meaningful).
    * Atomic replacement, so an interrupted save cannot truncate an existing
      project file.

What does NOT belong here:
    * Schema knowledge. Validation is the caller's job (Pydantic models in
      ``domain``/``config``).
    * Large binary payloads; images are never stored as JSON.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

JSON_INDENT = 2
"""Indentation used for every JSON document OMRFlow writes, so that project
files diff cleanly in version control."""


def read_json(path: Path) -> Any:
    """Parse a UTF-8 JSON document.

    Args:
        path: File to read.

    Returns:
        The decoded JSON value (usually a ``dict``).

    Raises:
        OSError: The file cannot be read.
        json.JSONDecodeError: The file is not valid JSON.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write ``payload`` as JSON, replacing ``path`` atomically.

    The document is first written to a temporary file in the *same* directory
    (a rename is only atomic within one filesystem) and then moved over the
    destination. A crash therefore leaves either the old file or the new one,
    never a half-written one.

    Args:
        path: Destination file. Parent directories must already exist.
        payload: Any JSON-serialisable value.

    Raises:
        OSError: The destination directory is not writable.
        TypeError: ``payload`` contains values JSON cannot represent.
    """
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=JSON_INDENT, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
