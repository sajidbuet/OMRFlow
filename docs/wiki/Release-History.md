# Release History

The user-facing changelog is
**[CHANGELOG.md](https://github.com/sajidbuet/OMRFlow/blob/main/CHANGELOG.md)**.
Downloads are on the
[Releases page](https://github.com/sajidbuet/OMRFlow/releases).

| Version | Date | Channel | Notes |
|---|---|---|---|
| [`0.1.0-alpha.2`](https://github.com/sajidbuet/OMRFlow/releases/tag/v0.1.0-alpha.2) | 2026-09-24 | **Alpha** | Redesigned application shell; template-driven synthetic dataset generator with paired attendance workbooks; four cross-platform CI defects fixed. Clean-machine test not re-run for this build |
| [`0.1.0-alpha.1`](https://github.com/sajidbuet/OMRFlow/releases/tag/v0.1.0-alpha.1) | 2026-09-22 | **Alpha** | First installable release. Core workflow implemented and clean-machine validated; real examination-data qualification incomplete |

## What the channels mean

| Channel | Meaning |
|---|---|
| **Alpha** | Core features implemented and covered by automated and synthetic tests. **Real examination-data qualification incomplete.** Interfaces and project formats may still change. Verify results independently |
| **Beta** | Representative real attendance workbooks and scanned cohorts processed end to end. Remaining work is mostly defect correction |
| **Release Candidate** | Feature frozen. The packaged application itself passes qualification, including upgrade and migration |
| **Stable** | Production release. Full qualification met |

A build's channel is derived from its version string, so it cannot claim a
maturity its version does not support.

## What is next

- **`0.1.0-beta.1`** — after [Phase 11B](Development-Roadmap#phase-11b--real-data-qualification--beta-release):
  real-data qualification against real sheets, scanners, attendance workbooks
  and answer keys, verified against independently known expected results.
- **`1.0.0-rc.1`**, then **`1.0.0`** — after
  [Phase 11C](Development-Roadmap#phase-11c--release-candidate--stable-release).

No dates are promised.

## Related

- [Development Roadmap](Development-Roadmap)
- [Known Limitations](Known-Limitations)
- [Upgrading OMRFlow](Upgrading-OMRFlow)
