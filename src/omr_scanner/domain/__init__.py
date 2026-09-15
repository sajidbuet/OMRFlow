"""Pure domain models.

Purpose:
    Vocabulary of the problem: projects, templates, zones, fields, bubbles and
    (from later phases) scans, recognition results, candidates, answer keys and
    results.

Responsibilities:
    * Define data shapes and the rules that make an instance *valid*.
    * Provide pure computations over those shapes (for example the geometric
      centre of a bubble in a grid).

What does NOT belong here:
    * I/O of any kind - no file reading, no database session, no Qt, no OpenCV.
      Loading a ``.omrt`` file is a service responsibility; the model only says
      what a valid template *is*.
    * Workflow/orchestration logic.

Dependencies:
    Pydantic and the standard library only. Nothing in ``domain`` may import
    ``services``, ``gui``, ``database``, ``imaging``, ``recognition`` or
    ``reporting``; this is asserted by ``tests/unit/test_architecture.py``.

Phase status:
    Phase 0 implements :mod:`omr_scanner.domain.project`,
    :mod:`omr_scanner.domain.geometry` and :mod:`omr_scanner.domain.template`.
    Scan, RecognitionResult, RecognitionConflict, Candidate, AttendanceRecord,
    AnswerKey, ScoringConfiguration, CandidateResult and AuditEvent are
    specified in ``docs/DATA_MODEL.md`` and are introduced by the phases that
    first need them.
"""
