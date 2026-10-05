"""The revised phase 9 automated intake qualification (0.1.1-alpha.0, roadmap 0.1.1-G part 1).

Purpose:
    Answer, with machine-verifiable evidence, whether the integrated
    ScanSession + finite ScanBatch + multi-source intake + continuous engine
    + Resolve / rescan + session-level scoring and reporting architecture
    processes a large, adversarial, continuously arriving **synthetic**
    examination correctly, survives forced termination, and produces results
    exactly equal to independently known ground truth
    (``ACCEPTANCE_CRITERIA.md`` §5).

Modules:
    * :mod:`.config` - what a campaign is, its modes and the release scale.
    * :mod:`.cohort` - the deterministic examination and arrival plan, with
      its ground truth (pure; no OMRFlow service is consulted).
    * :mod:`.render` - the plan's images, rendered by the existing
      template-driven generator into a pool the writers copy from.
    * :mod:`.evidence` - append-only, fsynced JSON-lines logs.
    * :mod:`.writer` - one simulated scanner: a separate process.
    * :mod:`.coordinator` - the OMRFlow application under test: a separate
      process the supervisor kills.
    * :mod:`.operator` - the scripted operator, through production services.
    * :mod:`.reference` - the independent reference computation.
    * :mod:`.inspect` - read-only fact extraction from a project database.
    * :mod:`.assertions` - the release-blocking assertion registry, crash
      matrix, endurance cases and verdict.
    * :mod:`.supervisor` - the campaign itself.
    * :mod:`.report` - ``report.json`` and ``report.md``.

Additional to - never a replacement for - the Phase 10 finite harness
(:mod:`omr_scanner.evaluation.qualification`), which is unchanged.

What it does not prove: genuine SMB behaviour, real scanners, power loss,
real-paper quality-policy calibration, the installed build, production
readiness. See ``docs/intake_qualification.md``.
"""
