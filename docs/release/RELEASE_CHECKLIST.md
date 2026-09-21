# Release Checklist

Work through this in order. Copy it into the release's tracking issue and
tick as you go.

Items marked **[not blocking for Alpha]** are required before a stable
release and may be deferred for a prerelease — deferred, not skipped
silently: record what was not done in the release notes and in
[Known Limitations](../wiki/Known-Limitations.md).

The commands are in [Release Process](../wiki/Release-Process.md).

---

## 1. Source

- [ ] Working tree clean (`git status`)
- [ ] On `main`, up to date with `origin/main`
- [ ] The release commit identified and its hash recorded
- [ ] `src/omr_scanner/_version.py` updated to the release version — **the
      only place the version is edited**
- [ ] `pip install -e .` re-run, so the installed metadata matches (an
      editable install caches its metadata at install time)
- [ ] `CHANGELOG.md` has a section for this version, dated, with Known
      Limitations
- [ ] Documentation updated: README status, `docs/wiki/`, roadmap statuses
- [ ] No leftover debugging code, commented-out blocks or `TODO`s introduced
      by this release
- [ ] No real candidate data, examination content, answer keys, project
      folders, signing keys or credentials anywhere in the tree

## 2. Testing

- [ ] `ruff check src tests` — clean
- [ ] `mypy src/omr_scanner` — clean
- [ ] `pytest` — the whole suite; record passed / failed / skipped
- [ ] Any skip is understood and expected (environmental, not a regression)
- [ ] Any pre-existing failure is recorded **as pre-existing**, with evidence
      it predates this release
- [ ] Synthetic end-to-end run performed by hand — the
      [Quick Start](../wiki/Quick-Start.md) from installation to a generated
      report
- [ ] **[not blocking for Alpha]** Real-data qualification (Phase 11B)
- [ ] **[not blocking for Alpha]** Golden-result verification against
      independently known expected outputs
- [ ] **[not blocking for Alpha]** 100,000-sheet qualification campaign

## 3. Packaging

- [ ] `.\scripts\release\Build-App.ps1 -Clean` succeeds
- [ ] The build reported the source commit (no "not a Git checkout" warning)
- [ ] The build did **not** warn about a dirty working tree
- [ ] `.\scripts\release\Test-PackagedApp.ps1` — all checks pass
- [ ] `.\scripts\release\Build-Installer.ps1` succeeds
- [ ] The installer filename carries the version:
      `OMRFlow-<version>-Setup-x64.exe`
- [ ] The installer's and the executable's Windows metadata report the right
      version
- [ ] The application icon is correct in Explorer, the Start menu and the
      title bar
- [ ] `.\scripts\release\Test-InstallerRoundTrip.ps1` — all checks pass,
      including **user configuration and logs preserved**
- [ ] `.\scripts\release\New-Checksums.ps1` — `SHA256SUMS.txt` written
- [ ] `.\scripts\release\Invoke-ReleaseVerification.ps1` — all checks pass
- [ ] **Clean-machine test** — [`CLEAN_MACHINE_TEST.md`](CLEAN_MACHINE_TEST.md).
      Required for every channel. In Windows Sandbox:
      `.\packaging\sandbox\New-SandboxPayload.ps1 -Launch`. If it genuinely
      cannot be run, say so explicitly in the release notes rather than
      leaving it ambiguous
- [ ] If the clean-machine test could not be run, the substitutes were:
      `python packaging\audit_dependencies.py dist\OMRFlow` — 0 unresolved
      imports, and `.\scripts\release\Test-SelfContained.ps1` — all checks
      pass. **Passing these is not a clean-machine pass**; record the
      clean-machine test as *not performed*
- [ ] **[not blocking for Alpha]** Installer is code-signed
- [ ] Accessibility checklist reviewed —
      [`ACCESSIBILITY_CHECKLIST.md`](ACCESSIBILITY_CHECKLIST.md). *Initial* is
      sufficient at Alpha; a full audit is required from Beta

## 4. Upgrade

- [ ] **[not blocking for the first release]** Upgrade over the previous
      released version, in place, without uninstalling
- [ ] **[not blocking for the first release]** A project created by the
      previous release opens, migrates, and its results still look right
- [ ] The schema version this build expects is recorded in the release notes
- [ ] [Upgrade Compatibility](../wiki/Upgrade-Compatibility.md) reflects
      reality

## 5. Documentation

- [ ] README: version, status section, screenshots current
- [ ] Every README link resolves
- [ ] `docs/wiki/` — every internal link resolves
- [ ] [Known Limitations](../wiki/Known-Limitations.md) updated, with each
      item at its true status
- [ ] [Installation](../wiki/Installation.md) — supported Windows versions
      reflect what was actually tested
- [ ] [Quick Start](../wiki/Quick-Start.md) — uses the interface's current
      wording
- [ ] [Development Roadmap](../wiki/Development-Roadmap.md) — phase statuses
      current
- [ ] [Release History](../wiki/Release-History.md) — the new row added
- [ ] Release notes written from
      [`RELEASE_NOTES_TEMPLATE.md`](RELEASE_NOTES_TEMPLATE.md), claiming
      **only** tests that were actually run
- [ ] Screenshots regenerated if the interface changed, using synthetic data

## 6. Release

- [ ] The tag matches the version exactly: `v<version>`
- [ ] Tag created on the release commit
- [ ] Tag pushed
- [ ] The release workflow ran and produced a **draft**
- [ ] **Pre-release flag correct** — ticked for Alpha, Beta and RC; clear
      only for a stable release
- [ ] *Set as the latest release* is **not** ticked for a prerelease
- [ ] Installer attached
- [ ] `SHA256SUMS.txt` attached
- [ ] Release notes reviewed — no claim stronger than the evidence
- [ ] Published

## 7. Post-release

- [ ] Downloaded the installer **from the release page**
- [ ] Its SHA-256 matches `SHA256SUMS.txt`
- [ ] Installed it from that download and launched it
- [ ] The About dialog shows the expected version, channel and build
- [ ] Every link on the release page resolves
- [ ] The release appears correctly on the Releases page, marked as a
      pre-release where applicable
- [ ] A fresh `## [Unreleased]` section added to `CHANGELOG.md`
- [ ] The next version's placeholder considered (do not bump
      `_version.py` until the next release is actually being prepared)

---

## Sign-off

| | |
|---|---|
| Version | |
| Channel | |
| Release commit | |
| Date | |
| Full suite | passed / failed / skipped |
| Clean-machine test | performed / **not performed** |
| Real-data qualification | performed / **not performed** |
| Known release-blocking defects | |
| Released by | |

> Do not tick a box that was not actually done. An honest "not performed"
> is useful; a false tick is worse than no checklist.
