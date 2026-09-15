"""Support ``python -m omr_scanner``.

Delegates to :func:`omr_scanner.main.main` so the module and the console script
entry point behave identically.
"""

from __future__ import annotations

from omr_scanner.main import main

if __name__ == "__main__":
    raise SystemExit(main())
