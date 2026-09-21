# Release Process

How an OMRFlow release is made. The pipeline:

```text
source → version → tests → tag → reproducible package → installer
       → verification → checksums → documented prerelease
```

Release engineering is treated as part of the software: an artifact nobody
can trace back to a source revision, or whose maturity claim nobody checked,
is not a release.

## The version is the single source of truth

`src/omr_scanner/_version.py` holds the version, and **nothing else does** —
`pyproject.toml` reads it, and `tests/unit/test_version.py` fails if a second
copy appears anywhere in the package.

`0.1.0-alpha.1` is simultaneously valid Semantic Versioning and valid PEP 440
(which normalises it to `0.1.0a1` for wheel filenames). So the Git tag, the
installer filename and the About dialog all use the readable spelling while
Python packaging gets something it accepts, from one constant.

The **release channel** — Alpha, Beta, Release Candidate, Stable — is
*derived* from that string rather than configured beside it. A build
therefore cannot claim to be stable while carrying a prerelease version, and
a prerelease is automatically flagged as a GitHub pre-release.

To make a release, edit that one string.

## Prerequisites

```powershell
pip install -e ".[dev,packaging]"
winget install --id JRSoftware.InnoSetup
```

Inno Setup is not a Python package and is not installed automatically; the
installer script says so and exits non-zero rather than producing nothing.

## The commands

Each script reads the version itself, returns a non-zero exit code on
failure, and says what to run next.

```powershell
# 1. Quality gates: ruff, mypy, pytest
.\scripts\release\Invoke-Tests.ps1

# 2. The application bundle -> dist\OMRFlow\
.\scripts\release\Build-App.ps1 -Clean

# 3. Launch it and check it behaves - needs a desktop
.\scripts\release\Test-PackagedApp.ps1

# 4. The installer -> dist\installer\OMRFlow-<version>-Setup-x64.exe
.\scripts\release\Build-Installer.ps1

# 5. Install, launch, uninstall, and confirm user data survives
.\scripts\release\Test-InstallerRoundTrip.ps1

# 6. SHA-256 checksums -> dist\installer\SHA256SUMS.txt
.\scripts\release\New-Checksums.ps1

# 7. Verify everything agrees before publishing
.\scripts\release\Invoke-ReleaseVerification.ps1
```

`Get-OMRFlowVersion.ps1` reports the version, channel, tag and build
identifier, and is what the other scripts use.

## Traceability

`Build-App.ps1` records the source commit in the build through
`OMRFLOW_BUILD_COMMIT`, because a frozen application has no `.git` directory
to ask. The About dialog's version tooltip and the diagnostic bundle then
report, for example, `0.1.0-alpha.1+a1b2c3d` — so a downloaded installer can
be traced back to an exact revision.

The build warns if the working tree is dirty. A release build should come
from a clean tree.

## Continuous integration

- **`ci.yml`** — on pushes and pull requests: ruff, mypy and the test suite
  on Windows and Linux, then a packaging smoke test on Windows. It builds the
  bundle and verifies its contents, but cannot launch it: a CI runner has no
  interactive desktop.
- **`release.yml`** — on a `v*` tag, or manually. Runs the gates, builds the
  bundle and the installer, generates checksums, verifies the artifacts, and
  creates a **draft** GitHub release.

**Nothing publishes automatically.** The release is a draft, and a prerelease
version is flagged as a pre-release, derived from the version. Making it
visible is a deliberate click after the artifacts have been checked.

The release workflow also **refuses a tag that disagrees with the version in
the source**, which is cheaper to discover there than after publication.

## What each channel requires

See the [release qualification matrix](Development-Roadmap#release-qualification-matrix).
Alpha does not require real-data qualification; Beta does. The matrix is the
authority on what may be deferred and what may not.

## Making a release

Work through
**[`docs/release/RELEASE_CHECKLIST.md`](https://github.com/sajidbuet/OMRflow/blob/main/docs/release/RELEASE_CHECKLIST.md)**,
which covers source, testing, packaging, documentation, publication and
post-release, and marks which stable-release requirements are not blocking
for an Alpha.

Release notes start from
**[`docs/release/RELEASE_NOTES_TEMPLATE.md`](https://github.com/sajidbuet/OMRflow/blob/main/docs/release/RELEASE_NOTES_TEMPLATE.md)**.

Clean-machine validation is
**[`docs/release/CLEAN_MACHINE_TEST.md`](https://github.com/sajidbuet/OMRflow/blob/main/docs/release/CLEAN_MACHINE_TEST.md)**.
In Windows Sandbox it is one command:

```powershell
.\packaging\sandbox\New-SandboxPayload.ps1 -Launch
```

Where a clean machine is genuinely unavailable, the substitutes are
`packaging/audit_dependencies.py` (a static PE import audit of the bundle)
and `scripts/release/Test-SelfContained.ps1` (installs, then launches with
no Python on the `PATH` and every `PYTHON*`/`QT*` variable cleared). Both
must pass — but **passing them is not a clean-machine pass**, and the
release notes must record the clean-machine test as *not performed*.

## Code signing

**Not configured.** Alpha installers are unsigned, Windows SmartScreen warns
about them, and this is documented rather than implied away. The packaging is
structured so signing can be added without rework; it is a
[Phase 11C](Development-Roadmap#phase-11c--release-candidate--stable-release)
item.

**No signing certificate or private key is ever committed to this
repository.**

## Related

- [Development Roadmap](Development-Roadmap)
- [Release History](Release-History)
- [Development Setup](Development-Setup)
