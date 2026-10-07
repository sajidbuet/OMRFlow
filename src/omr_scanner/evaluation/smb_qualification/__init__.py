r"""Revised phase 10: the real-network (SMB) intake qualification.

``ACCEPTANCE_CRITERIA.md`` §6 requires intake on genuine SMB shares written by
processes on other Windows machines. This package prepares that run (the
Phase 9 cohort, one writer package per scanner PC), drives it from the OMRFlow
machine (one forced restart, one real share outage made by a person and
*observed*, never assumed), measures listing cost and stabilisation latency,
and judges it. Its rehearsal mode runs the same procedure on local folders to
validate the tooling; a rehearsal is never SMB evidence, and the evaluator
cannot be talked into saying otherwise.

Command line: ``python -m omr_scanner.tools.smb_qualification``. Procedure:
``docs/release/SMB_QUALIFICATION.md``.
"""
