"""Developer command line utilities.

Purpose:
    Let a developer run one stage of OMRFlow against one file without starting
    the GUI, so that an alignment can be inspected while it is being tuned.

Modules:
    * ``align_image.py``    - normalise one scan and report what was measured.
    * ``make_test_sheet.py`` - render a synthetic sheet, optionally distorted.

What does NOT belong here:
    * Anything a user is expected to run. These are development tools; the
      supported interfaces are the GUI and, from Phase 5, the batch pipeline.
    * Algorithms. A tool parses arguments, calls a service or the imaging layer,
      and prints. Nothing here may be the only implementation of anything.

Usage is documented in ``docs/IMAGE_PROCESSING.md`` ("Developer tools").
"""
