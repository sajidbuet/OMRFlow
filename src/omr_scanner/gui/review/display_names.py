"""File names in Resolve's decision panels, bounded (0.1.1 revised phase 8).

A scanner may write a file name - or a sub-folder path - of 80 characters with
nowhere to wrap. Shown whole in a rich-text panel, one such name sets the
panel's minimum width, and through the decision stack (whose minimum is its
widest page's) the whole Resolve page's. Panels therefore show a name
shortened in the **middle** - its start and its ending (the extension, the
student number a scanner appended) kept - and the provenance tooltip beside
the evidence tabs has it whole.
"""

from __future__ import annotations

SHEET_NAME_LIMIT = 32
"""Characters of a name shown in a decision panel before it is shortened - no
wider than the panels' own headings, so a name never sets their width."""


def middle_ellipsis(name: str, limit: int = SHEET_NAME_LIMIT) -> str:
    """``name`` at most ``limit`` characters, its start and its ending kept."""
    if len(name) <= limit:
        return name
    tail = (limit - 1) // 2
    return f"{name[: limit - 1 - tail]}…{name[-tail:]}"


__all__ = ["SHEET_NAME_LIMIT", "middle_ellipsis"]
