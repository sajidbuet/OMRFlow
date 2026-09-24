# OMRFlow documentation

OMRFlow processes optical-mark-recognition answer sheets: you design a
template, scan the sheets, let OMRFlow read them, review what it could not
decide, reconcile attendance against your candidate list, score against an
answer key, and generate result workbooks built on your own attendance
workbook.

Everything happens on your machine. Nothing is uploaded anywhere.

![The Project stage as OMRFlow opens: the compact chrome row with the
application menu, the wordmark and the nine-stage workflow ribbon across the
top, the no-project panel, and the Getting Started and Recent Projects
cards](../images/omrflow-main-window.png)

<sub>This screenshot predates the shell redesign: the stages, the panel and
the cards are current, the two-band chrome above them is not. See *The
application window* in the
[README](https://github.com/sajidbuet/OMRFlow#the-application-window).</sub>

> ### ⚠️ This is an Alpha release
>
> **Version `0.1.0-alpha.2`.** Core workflows are implemented and covered by
> an automated test suite, but **real examination-data qualification is
> still in progress** — no real attendance workbook and no real scanned
> cohort has been processed end to end.
>
> **Independently verify any generated result before using it
> operationally.** See [Known Limitations](Known-Limitations) for exactly
> what has and has not been validated.

## Start here

| If you want to… | Read |
|---|---|
| Install OMRFlow | [Installation](Installation) |
| See it work, end to end, in about half an hour | [Quick Start](Quick-Start) |
| Know what is and is not trustworthy yet | [Known Limitations](Known-Limitations) |
| Understand a particular stage | [User Guide](User-Guide) |
| Install a newer Alpha without losing work | [Upgrading OMRFlow](Upgrading-OMRFlow) |
| Fix something that has gone wrong | [Troubleshooting](Troubleshooting) |
| Report a problem | [SUPPORT.md](https://github.com/sajidbuet/OMRFlow/blob/main/SUPPORT.md) |
| Contribute code | [Contribution Guide](Contribution-Guide) |

## The workflow

OMRFlow's window presents nine stages left to right, in the order you work
through them. You can move between them freely; each stage says what it is
waiting for.

| | Stage | What you do |
|---|---|---|
| 1 | **Project** | Create or open a project; name the examination and define its sets |
| 2 | **Template** | Mark up a blank answer sheet so OMRFlow knows where the bubbles are |
| 3 | **Calibrate** | Check the template against real scans and tune recognition before a batch |
| 4 | **Scan** | Import scanned sheets and run recognition |
| 5 | **Resolve** | Decide the sheets recognition was unsure about |
| 6 | **Attendance** | Import the candidate list and reconcile it against the scripts |
| 7 | **Answer Key** | Enter or scan the key for each question-paper set, and verify it |
| 8 | **Results** | Configure marking and calculate scores and ranks |
| 9 | **Reports** | Generate roll-wise and merit-wise workbooks |

Stages 6 and 7 do not depend on each other and can be done in either order,
or while scanning is still running.

## How this documentation is arranged

These pages are the **user-facing** documentation and are the canonical
source: the GitHub Wiki is published from `docs/wiki/` in the repository, so
what you are reading is version-controlled alongside the code that it
describes.

Some pages are deliberately short and hand off to a longer reference
document kept elsewhere in the repository — `docs/reporting.md`,
`docs/scoring.md` and so on. Those documents are detailed, current and
verified; duplicating them here would create two versions of the truth and
one of them would rot. Where that happens the page says so and links
directly.

Developer documentation — architecture, the data model, the recognition
engine, the testing strategy — stays in `docs/` and is linked from the
Developers section.

## Project status

OMRFlow is pre-1.0 and developed in numbered phases. Phases 0–10 are
implemented; the current work is Phase 11, which takes the application from
"implemented and synthetically tested" to a qualified stable release:

- **11A — Alpha release infrastructure.** This release.
- **11B — Real-data qualification and Beta.** Validating against real
  examination material.
- **11C — Release candidate and stable.** Feature freeze, packaged-build
  qualification, `v1.0.0`.

See the [Development Roadmap](Development-Roadmap) for the detail, and
[Release History](Release-History) for what has shipped.

## Licence

MIT. See [LICENSE](https://github.com/sajidbuet/OMRFlow/blob/main/LICENSE).
