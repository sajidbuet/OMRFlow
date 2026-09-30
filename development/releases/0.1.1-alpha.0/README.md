# `0.1.1-alpha.0` — release planning area

> **Planned target. Not released, not implemented.** The running application
> is `0.1.0-alpha.2` (`src/omr_scanner/_version.py`) and remains so until
> implementation of this line begins — see [ROADMAP.md](ROADMAP.md) §9.

This directory holds **the single authoritative plan** for `0.1.1-alpha.0`:
scan sessions over finite batches, multi-source intake, session-level results,
and set-code identity.

```text
Project → ScanSession → one or more finite ScanBatch objects → sheets
```

| File | What it is |
|---|---|
| [ROADMAP.md](ROADMAP.md) | Objective, architecture summary, baseline, Phase 11B policy, phases A–G, priorities, status tracks, version rules, and which earlier plans it supersedes |
| [ARCHITECTURE_NOTES.md](ARCHITECTURE_NOTES.md) | What exists on `main` today (audited), the six latent defects, further findings, the session/batch model, set identity, session-level results, duplicates and rescans, intake, concurrency, migrations, open questions |
| [ACCEPTANCE_CRITERIA.md](ACCEPTANCE_CRITERIA.md) | Invariants, per-phase exit criteria, closure checks, the real-world matrix, the synthetic intake campaign, the SMB qualification, the real scanning-room qualification and the release gate |
| [prompts/](prompts/) | One implementation prompt per phase. **Written, not executed** |

These documents specify **semantics first**. Where they name a table, column
or class, that illustrates the required meaning; each phase re-inspects the
code and chooses the cleanest representation, recording differences in its
handoff.

**Superseded:** `development/ROADMAP_v0.1.1-alpha.0.md` (now a pointer here)
and the earlier contents of this directory from the
`roadmap/0.1.1-alpha.0-live-intake` branch, including its `MERGE_NOTES.md`.
Details: ROADMAP.md §11.
