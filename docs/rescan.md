# Reject & Rescan, suggested rescans and replacements

Status: Reject & Rescan **implemented — automated tests passing; scripted in
the real window; not yet worked by an operator on real sheets.** The quality
suggestions and the session-wide, provenance-ranked rescan matching below
(0.1.1 revised phase 7) are **implemented and tested headless; no GUI shows
them yet (phase 8); not validated on real rejected scripts.**

## The lifecycle (unchanged)

A scan's logical lifecycle lives in `scan_rejection`
(`domain/scan_lifecycle.py`, `services/scan_lifecycle.py`); history in
`audit_event` (`entity_type = scan_lifecycle`). Every transition is one
transaction with its audit event and a named operator:

```text
active --reject_scan--> rejected_pending_rescan --confirm_replacement--> superseded_by_replacement
  ^          (undo_reject)       |  ^                (remove_replacement)          |
  +------------------------------+  +----------------------------------------------+
```

Only `active` counts. Nothing is deleted: a rejected original stays stored,
with its result, conflicts and history; a confirmed rescan counts **once**, in
the original's session, whichever batch or scanner read it. New links across
scan sessions are refused.

## Where a rejection can come from (revised phase 7)

1. **An operator's own judgement** - Resolve's *Reject & Rescan*, as before.
2. **A suggested rescan.** The scan-quality decision layer
   ([scan_quality.md](scan_quality.md#the-decision-layer-above-the-evidence-011-revised-phase-7))
   marks a read sheet `RESCAN_REQUIRED` when its stored evidence says the
   *paper* must be scanned again - the image did not decode, the page did not
   register, or the geometry check judged it `UNUSABLE` - with an existing
   rejection reason (*poor quality*, *registration*, *clipped*, *folded*,
   *id_unreadable*). It is a **suggestion**: the sheet keeps counting until a
   named operator answers it -
   * `quality_decisions.confirm_suggestion(scan, reviewer=...)` is exactly
     `reject_scan` (the operator may choose another reason, and may declare
     the Student ID the case is known by);
   * `quality_decisions.dismiss_suggestion(scan, reviewer=...)` keeps the sheet
     as read (audited, `entity_type = scan_quality`);
   * resolving the sheet's evidence conflict in Resolve is the same answer as
     dismissing.

   Nothing in OMRFlow rejects, supersedes or replaces a scan by itself. A
   software failure (`RETRY_PROCESSING`) never becomes a suggestion.

## Finding the rescan (revised phase 7)

`scan_lifecycle.session_possible_rescans(database, scan_session_id)` lists, for
every outstanding case of the session, the session's read, active sheets with
the same **effective** Student ID - from any batch and **any intake source**:
a sheet rejected at Scanner A and scanned again at Scanner C is suggested.
Each candidate carries its provenance (source, arrival time, whether it arrived
after the rejection) and the ranking is: agreeing set code, then arrived after
the rejection, then newest. Exact re-imports of the rejected bytes are never
candidates (`sync_reimports` links them as `reimport_of_rejected`). File names
and batch numbers are never evidence. **A suggestion is not a confirmation:**
`confirm_replacement` remains the operator's explicit action, and
`remove_replacement` undoes it (the original returns to *awaiting rescan*; the
former replacement is an ordinary active scan again).

## Closing a session

*Finish scan session* reports, separately, rejected sheets whose rescan has not
arrived (`rescan_outstanding`), rejected sheets whose likely rescan has arrived
but is unconfirmed (`replacement_unmatched`) and suggested rescans nobody has
answered (`rescan_suggested`). The first two may be closed past only by a named
operator's audited acceptance of incomplete results; the third may not - it is
a decision still owed. See [intake.md](intake.md#finish-scan-session-revised-phase-7).

## Restarts and crashes

Rejection, confirmation, removal and dismissal are each one transaction with
their audit events (real process kills inside each: all or nothing, audited
once - `tests/crash/test_phase7_kills.py`). Decisions are written in the
sheet's own work unit, so a crash never leaves half a decision; lineage and
effective counts are re-derived from the stored links after every restart.
