# Security Policy

OMRFlow processes examination material and candidate information. A defect
that exposes a project's contents is a privacy problem as much as a security
one, and is treated as such.

## Supported versions

OMRFlow is pre-1.0 and currently distributed as an **Alpha** release. Only
the most recent release receives fixes; there are no maintenance branches for
older prereleases, and there is no long-term support commitment before
`v1.0.0`.

| Version | Supported |
|---|---|
| `0.1.0-alpha.2` (latest Alpha) | Yes — fixes land in the next prerelease |
| Any earlier prerelease | No — upgrade to the latest release |
| Unreleased `main` | Yes, on a best-effort basis |

Find the version you are running under **Application menu → Help → About
OMRFlow**, or read it from the window's title bar.

## Reporting a vulnerability

**Please do not open a public issue for a vulnerability that could put a
user's examination data at risk.** A public report tells everyone, including
people running the affected build on live examination material, before a fix
exists.

Use GitHub's private vulnerability reporting:

1. Go to <https://github.com/sajidbuet/OMRFlow/security/advisories/new>
2. Describe the problem, the version, and how to reproduce it.
3. Submit. Only the repository maintainers can see the report.

> **Note for the repository owner.** Private vulnerability reporting must be
> switched on for that link to work: *Settings → Advanced Security → Private
> vulnerability reporting → Enable*. Until it is enabled, the link returns a
> 404 and a reporter has no private channel. This file deliberately does not
> list an email address, because inventing one would be worse than naming
> none — see the fallback below.

If private reporting is unavailable and the issue is genuinely sensitive,
open a public issue containing **only**:

> "Potential security issue, details withheld — please advise a private
> contact."

…and nothing else. Do not include the reproduction, the affected version's
specifics, or any data.

### What to include

- The OMRFlow version and your Windows version.
- What an attacker or a mistaken user could achieve.
- Steps to reproduce, ideally with **synthetic** data.
- Any logs, with candidate information removed.

### What not to include

Never attach real candidate rosters, real answer keys, real scanned sheets,
or a real project database to a report of any kind. Reproduce the problem
with synthetic data if you can; if the problem only occurs with real data,
say so and describe the shape of the data rather than sending it.

## What to expect

This is a small project without a funded security team. There is no
guaranteed response time. Reports are acknowledged as soon as they are read,
and a fix is prioritised by how much data it puts at risk.

## Scope

In scope:

- Anything that lets one user read or alter another's project data.
- Anything that causes OMRFlow to transmit project data anywhere. OMRFlow has
  no telemetry and makes no network requests during examination processing;
  if you observe one, that is a defect worth reporting.
- Writing outside the project directory or the per-user configuration and log
  directories.
- Code execution through a crafted project, template, workbook or scan.
- Credentials, keys or candidate data written into logs or diagnostic
  bundles. The diagnostic bundle is designed to exclude candidate data; a
  leak there is a real defect.

Out of scope:

- The absence of a code-signing certificate. Alpha installers are unsigned
  and Windows SmartScreen will warn about them. This is documented, not a
  vulnerability — see `docs/wiki/Installation.md`.
- Anything requiring an attacker to already have write access to the
  operator's machine or project folder. OMRFlow does not defend against a
  compromised host.
- Vulnerabilities in Python, Qt, OpenCV or other dependencies — report those
  upstream. Do tell us if OMRFlow pins an affected version, so the pin can be
  moved.

## Handling your own examination data

OMRFlow keeps everything local: a project is a folder on your disk holding a
SQLite database, imported scans and generated workbooks. Nothing is uploaded.
Protect it the way you would protect any other examination material —
`docs/wiki/Backup-and-Data-Retention.md` covers what a project contains and
what to back up.
