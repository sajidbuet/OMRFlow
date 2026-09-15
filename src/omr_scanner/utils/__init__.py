"""Small, dependency-light helpers shared by every other layer.

Purpose:
    Utilities that are useful everywhere and depend on nothing inside OMRFlow
    except :mod:`omr_scanner.errors`.

What does NOT belong here:
    * Domain concepts (a "zone" or a "scan" is domain vocabulary, not a utility).
    * Anything importing Qt, OpenCV, SQLAlchemy or the service layer. ``utils``
      sits at the bottom of the dependency graph and must stay importable from
      unit tests without side effects.
"""
