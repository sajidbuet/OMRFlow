# Release History

The user-facing changelog is
**[CHANGELOG.md](https://github.com/sajidbuet/OMRflow/blob/main/CHANGELOG.md)**.
Downloads are on the
[Releases page](https://github.com/sajidbuet/OMRflow/releases).

| Version | Date | Channel | Notes |
|---|---|---|---|
| `0.1.0-alpha.1` | *not yet published* | **Alpha** | First installable release. Built, and clean-machine validated; the Git tag and the GitHub release have not been created yet, so there is nothing to link to and nothing to download |

> Until `v0.1.0-alpha.1` is tagged and published, the
> [Releases page](https://github.com/sajidbuet/OMRflow/releases) is empty.
> This row becomes a link, and gains its date, when it is. Nothing else on
> this page assumes the release exists.

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
