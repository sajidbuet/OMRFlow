# `0.1.1-alpha.0` — release planning area

> **Planned target. Not released, not implemented.**
> The running application is `0.1.0-alpha.2`
> (`src/omr_scanner/_version.py`), and remains so until implementation of this
> line begins — see [MERGE_NOTES.md](MERGE_NOTES.md) §5.

`0.1.0-alpha.2` remains the finite-batch Alpha baseline.
`0.1.1-alpha.0` begins the operational live-intake / multi-scanner development
line.

| File | What it is |
|---|---|
| [ROADMAP.md](ROADMAP.md) | Why this line exists, the release sequence, the phase plan and its status table |
| [ARCHITECTURE_NOTES.md](ARCHITECTURE_NOTES.md) | What exists today that the new line must reuse, the proposed concepts and lifecycles, and the design questions each phase must settle |
| [ACCEPTANCE_CRITERIA.md](ACCEPTANCE_CRITERIA.md) | Per-phase exit criteria, the intake qualification campaign, the network-share qualification, the later real-scanning-room qualification, and the `0.1.1-alpha.0` release gate |
| [MERGE_NOTES.md](MERGE_NOTES.md) | How to land this planning branch on `main` after the concurrent update, and when the version is bumped |
| [prompts/](prompts/) | One Claude Code implementation prompt per phase. **Written, not executed** |

These documents specify **semantics first**. Where they name a table, column
or class, that is an illustration of the required meaning, not a schema the
implementer is bound to; each phase prompt requires the implementer to
re-inspect the code and choose the cleanest representation the existing data
model allows.
