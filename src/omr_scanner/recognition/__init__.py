"""Interpretation of measurements as OMR values. RESERVED - no implementation in Phase 0.

Purpose:
    Turn per-bubble measurements produced by ``imaging`` into logical values:
    a roll number, a set code, an answer per question - each with a confidence
    and an explicit representation of "missing" and "multiple" marks.

Planned modules (Phase 3 and Phase 6, see ``development/ROADMAP.md``):
    * ``decide.py``    - threshold logic turning fill ratios into marked/unmarked.
    * ``fields.py``    - assembling per-bubble decisions into field values.
    * ``conflicts.py`` - detecting the cases that require human resolution.

What does NOT belong here:
    * Pixel access or OpenCV calls; this layer consumes measurements, it does not
      produce them.
    * Database writes or GUI code. Persisting a recognition result is a service
      responsibility.

Contract for later phases:
    * A result never silently discards an ambiguity. A value is either confident,
      or it carries the competing alternatives so Phase 6 can present them.
    * Recognition thresholds come from the template
      (:class:`~omr_scanner.domain.template.RecognitionSettings`), never from
      module-level constants.
    * Failures raise :class:`omr_scanner.errors.RecognitionError`.
"""
