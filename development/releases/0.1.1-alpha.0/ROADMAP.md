# `0.1.1-alpha.0` roadmap — live, multi-scanner intake

> **Status: planned target.** Not released and not implemented. The running
> application version is `0.1.0-alpha.2`; it changes only when implementation
> begins (see §7).

**`0.1.0-alpha.2` remains the finite-batch Alpha baseline.**

**`0.1.1-alpha.0` begins the operational live-intake / multi-scanner
development line.**

Companion documents: [ARCHITECTURE_NOTES.md](ARCHITECTURE_NOTES.md) (what
exists, what is proposed, and why), [ACCEPTANCE_CRITERIA.md](ACCEPTANCE_CRITERIA.md)
(what "done" means for each phase and for the release),
[prompts/](prompts/) (one implementation prompt per phase),
[MERGE_NOTES.md](MERGE_NOTES.md).

---

## 1. Why this line exists

`0.1.0-alpha.2` handles a finite stack well:

```text
scan a finite stack
    ↓
copy/import images
    ↓
create/process a finite batch
    ↓
resolve conflicts
    ↓
reconcile / score / report
```

That workflow is **not redefined** and stays supported throughout `0.1.1`.

A real examination scanning room has several medium-speed scanners, each on
its own PC, writing continuously — often to network shares — while operators
resolve conflicts and physically rescan damaged sheets:

```text
Scanner A + PC A ─┐     \\SCAN-PC-A\Exam2026\OMR
Scanner B + PC B ─┼──►  one OMRFlow scan session
Scanner C + PC C ─┘     \\ExamServer\Exam2026\Scanner-C
```

Two intake modes must both work:

- **Mode A — finite/manual intake.** 1,000 sheets from Scanner A are copied
  into a folder and imported; OMRFlow discovers and processes what is new.
- **Mode B — live/watched-folder intake.** OMRFlow watches several source
  folders, processes stable new images as they appear, and keeps going while
  scanners keep producing sheets.

Recognition, conflict resolution and rescanning then happen **concurrently**.

---

## 2. Design principle

```text
0.1.0-alpha.2
    finite static batch

0.1.1-alpha.x
    dynamic examination intake session
        ├── one or many scan sources
        ├── manual or watched-folder intake
        ├── continuously changing workload
        ├── incremental recognition
        ├── incremental conflict review
        ├── scan-quality rejection
        ├── physical rescan/replacement
        └── explicit session closure
```

- **Scan session** = the open-ended operational container.
- **Scan source** = provenance / input location (Scanner A, B, C, or a manual
  import). Never logical sheet identity.
- **Scan asset** = one physical image file and its provenance.
- **`ScanBatch`** = a *finite processing unit*, with Phase 5 semantics
  preserved exactly. It is **not** turned into an ever-growing batch
  (ARCHITECTURE_NOTES.md §3 says why).

Reuse, do not destabilise: Phase 5 durable batches, bounded multiprocessing,
persistent per-sheet status, resume/recovery; the Phase 6 review ledger and
idempotent conflict sync; Phase 7 reconciliation; Phase 10 content hashes,
project locking, append-only history, and the 100,000-sheet qualification
machinery; the single-writer database architecture.

The existing finite-batch workflow must remain usable at every commit of this
line.

---

## 3. Release sequence

```text
0.1.0-alpha.2        finite-batch Alpha baseline               ← released 2026-09-24
        ↓
0.1.1-alpha.0        new operational architecture introduced   ← this plan
        ↓
0.1.1-alpha.1 …      hardening and fixes
        ↓
real-data and real-scanning-room qualification
        ↓
Beta
```

**The major Beta qualification (canonical Phase 11B) should not be performed
against an operational model already scheduled for replacement.** This plan
therefore proposes that 11B's real-data campaign runs on the `0.1.1` line,
and that 11B's current target version (`v0.1.0-beta.1` in
`docs/wiki/Development-Roadmap.md`) be revisited when this plan is adopted. It
does not change the canonical roadmap's content; see §8.

Small real-data smoke tests on `0.1.0-alpha.2` (for example the existing
"scan 20–30 filled sheets" exercise in `CURRENT_STATE.md`) remain valuable:
they measure recognition, which this line does not change.

---

## 4. Phase plan

The six phases below adjust the suggested five-phase plan in one way:
**session-scoped reconciliation, scoring and reporting is its own phase (E).**
Inspection showed that Phases 7–9 are keyed by `(roster_id, batch_id)` and
the Attendance, Results and Reports pages pick "the latest batch". A session
of many processing units would otherwise reconcile and report one unit — a
silent, serious failure. It is too large and too different in kind (it
touches Phases 7–9, not intake) to fold into C without making C
unreviewable. D and E both depend on A–C and can proceed in either order.

| Phase | Title | Depends on | Prompt | Status |
|---|---|---|---|---|
| **0.1.1-A** | Architecture & persistence | — | [01](prompts/01-architecture-persistence.md) | Pending |
| **0.1.1-B** | Intake engine | A | [02](prompts/02-multisource-live-intake.md) | Pending |
| **0.1.1-C** | Incremental processing, review & rescan | A, B | [03](prompts/03-incremental-review-rescan.md) | Pending |
| **0.1.1-D** | Operational GUI | A–C | [04](prompts/04-operational-gui.md) | Pending |
| **0.1.1-E** | Session-scoped reconciliation, scoring & reporting | A–C | [05](prompts/05-session-results.md) | Pending |
| **0.1.1-F** | Operational qualification & Alpha release | A–E | [06](prompts/06-qualification-release.md) | Pending |

### 0.1.1-A — Architecture & persistence

- Re-inspect the batch, review, reconciliation and provenance contracts.
- Scan session, scan source and scan asset concepts; the asset↔`BatchScan`
  and session↔`ScanBatch` links; supersession and duplicate relationships.
- ADR-0005 (ingest-by-copy vs reference-in-place), ADR on the concurrent
  writer strategy, ADR on template changes within a session.
- Migration 10 (additive), upgrade test from a schema-9 fixture.
- Version bump `0.1.0-alpha.2` → `0.1.1-alpha.0` as the first commit.
- Headless repository layer and domain vocabulary. **No GUI, no watcher.**

### 0.1.1-B — Intake engine

- Multi-source discovery; manual import and watched sources converging on one
  pipeline.
- Stabilisation state machine; periodic authoritative reconciliation;
  optional notification wake-up.
- Hashing during stabilisation; duplicate-content handling; path reuse.
- Source outage and reconnection; restart re-discovery.
- Headless, Qt-free, driven by a fake clock and fake filesystem in unit tests
  and by real temporary directories in integration tests.

### 0.1.1-C — Incremental processing, review & rescan

- Unit scheduler: READY assets → finite `ScanBatch` units → unchanged
  `process_batch`.
- Per-unit sheet-local conflict sync; session-scoped cross-sheet conflicts
  (duplicate ID arriving 44 minutes later).
- Quality decision layer: ACCEPT / ACCEPT WITH WARNING / RESCAN REQUIRED,
  policy-driven.
- Rescan items, replacement suggestion and confirmed association,
  supersession, undo — all audited.
- Session aggregate snapshot service (counts that partition).
- Concurrency: writes from intake, recording and review at once.

### 0.1.1-D — Operational GUI

- Scan session mode on the Scan stage: configure sources, start / pause /
  resume processing, Finish Scan Session with its blocker list.
- Live counts; three separate progress lines; "Caught up — watching for new
  scans"; compact per-source status.
- Resolve and a Rescan queue working while intake continues.
- Responsiveness at 10,000+ assets (paged/lazy models where the list grows).
- The finite Scan workflow is unchanged when no session is open.

### 0.1.1-E — Session-scoped reconciliation, scoring & reporting

- The **effective scan set** of a session as a first-class input to Phases
  7–9, beside today's per-batch input.
- Attendance, Results and Reports select a session or a batch explicitly.
- Provisional results while a session is open; Final Export requires a closed
  session.
- Per-batch behaviour for finite projects unchanged, proven by the existing
  suites plus golden comparisons.

### 0.1.1-F — Operational qualification & Alpha release

- Synthetic multi-source intake campaign (≥ 3 sources, ≥ 10,000 images).
- Real SMB network-share qualification.
- Restart, source loss/reconnection, rescan/replacement under load.
- Regression of the finite-batch mode; Phase 10 harness still passing.
- Packaged-application verification; documentation; the `0.1.1-alpha.0`
  release gate (ACCEPTANCE_CRITERIA.md §7).

---

## 5. Status vocabulary for this line

OMRFlow already distinguishes these and they are **not synonyms**:

```text
implemented
automated tests passing
synthetically validated
real-scan validated
operationally qualified
released
```

For this line each phase additionally tracks, separately:

| Track | Meaning |
|---|---|
| Implementation complete | The phase's scope exists in code |
| Automated tests complete | Unit, integration and GUI tests for that scope pass, with `ruff` and `mypy` |
| Synthetic validation complete | The relevant part of the intake campaign (ACCEPTANCE_CRITERIA.md §4) has run and passed |
| Network-share validation complete | The SMB qualification (§5 there) has run and passed |
| Real scanner validation complete | Real scanner workstations, real paper (§6 there) |
| Production qualification complete | Operationally qualified for a real examination |

**Passing unit tests never completes a phase by itself.** A phase's status
may read "Implemented; automated tests passing" and still be pending every
validation track. Each prompt enforces this.

### Status table (update as phases progress)

| Phase | Implementation | Automated tests | Synthetic validation | Network share | Real scanners | Production |
|---|---|---|---|---|---|---|
| A | Pending | Pending | n/a | n/a | n/a | n/a |
| B | Pending | Pending | Pending | Pending | Pending | Pending |
| C | Pending | Pending | Pending | Pending | Pending | Pending |
| D | Pending | Pending | Pending | Pending | Pending | Pending |
| E | Pending | Pending | Pending | n/a | Pending | Pending |
| F | Pending | Pending | Pending | Pending | Pending (not required for `alpha.0`) | Pending (not required for `alpha.0`) |

---

## 6. Explicitly out of scope for `0.1.1-alpha.0`

- Changing recognition, alignment, scoring rules or the conflict policy's
  meaning (answer ambiguity stays a result, not a conflict).
- Redesigning the Phase 10 100,000-sheet qualification. It is preserved and
  an **additional** intake campaign is defined.
- Several OMRFlow instances writing one project. Scanner PCs write image
  files; exactly one OMRFlow coordinator owns the project.
- Controlling scanners (TWAIN/WIA/ISIS). OMRFlow consumes files.
- PDF input.
- Authentication; reviewer identity stays a name.
- Automatic, unconfirmed replacement of rejected scans.
- Validated quality-decision defaults — those come from real-data
  qualification after this release.

---

## 7. Version-bump timing

- `src/omr_scanner/_version.py` remains the **single source of truth**. No
  second version constant is created anywhere.
- It stays `0.1.0-alpha.2` on this planning branch and after it is merged.
- It becomes `0.1.1-alpha.0` **only when implementation of this line begins on
  the merged, current codebase**: as the first commit of prompt 01, or a very
  small preparatory commit immediately before it (MERGE_NOTES.md §5).
- Releasing `0.1.1-alpha.0` (tag, installer, GitHub pre-release) happens only
  after the gate in ACCEPTANCE_CRITERIA.md §7, via
  `docs/release/RELEASE_CHECKLIST.md`.

Consequence to accept knowingly: between the bump and the release, a source
build reports `0.1.1-alpha.0` while the feature is incomplete. That matches
how `0.1.0-alpha.2` was developed, and the About dialog's Alpha notice already
covers it.

---

## 8. Relationship to the canonical roadmap

`docs/wiki/Development-Roadmap.md` stays canonical. This branch adds only a
short pointer section there. When the plan is adopted on `main`, the
canonical page should gain a proper `0.1.1` entry, and Phase 11B's target and
the "Where `0.1.0-alpha.1` stands" table should be brought up to date — in a
normal documentation commit, not in this conflict-sensitive branch.

## 9. Documentation rule for every phase

Each phase updates, at minimum: this file's status table;
`development/CURRENT_STATE.md`; a `development/releases/0.1.1-alpha.0/PHASE_<X>_HANDOFF.md`;
the README **Development status** and **Testing status** tables (phases
completed, under testing, automated / synthetic / real-data status, pending
work); `docs/wiki/Development-Roadmap.md`'s `0.1.1` entry; `CHANGELOG.md`
`[Unreleased]`; and the user/developer documents its scope touches.
