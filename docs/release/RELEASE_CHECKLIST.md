# Release Checklist

## The short version

A routine release is one command:

```powershell
.\scripts\release.ps1 -Version 0.1.0-alpha.3
```

Run with nothing to be told the current version and what is required. It
changes nothing:

```powershell
.\scripts\release.ps1
```

```text
OMRFlow release automation

Current repository version: 0.1.0-alpha.2

ERROR: A release version is required.
```

Full usage, which also reports the current version:

```powershell
.\scripts\release.ps1 --help
```

See exactly what a release would do, without doing any of it:

```powershell
.\scripts\release.ps1 -Version 0.1.0-alpha.3 -DryRun
```

Then make it:

```powershell
.\scripts\release.ps1 -Version 0.1.0-alpha.3
```

The script checks the repository, updates the version metadata, runs lint,
types and the full suite, commits, creates an annotated tag, and pushes the
commit and tag together. **GitHub Actions then builds and publishes the
canonical release** from that tag — it re-runs the gates on a clean runner,
builds the installer, verifies it, writes `SHA256SUMS.txt` and publishes the
GitHub release. Zenodo archives the release once GitHub publishes it.

Nothing in that path needs a person to make a judgement, and nothing in it
needs an AI assistant.

What the script refuses to do, with no option to override: release from a
branch other than `main`, release with anything uncommitted or untracked,
release when local and `origin/main` disagree, reuse or move an existing tag,
go backwards or sideways in version, or continue past a failing gate. A
release that needs a bypass is a release that should not be published.

If it fails after the commit and tag but before the push, it says so and
gives the two commands to either retry or undo. Nothing is published until
the push succeeds, and nothing is downloadable until GitHub Actions finishes.

The rest of this page is the manual checklist: what the script does not and
cannot check — the clean-machine test, the accessibility pass, the release
notes, and everything after publication.

---

Work through this in order. Copy it into the release's tracking issue and
tick as you go.

Items marked **[not blocking for Alpha]** are required before a stable
release and may be deferred for a prerelease — deferred, not skipped
silently: record what was not done in the release notes and in
[Known Limitations](../wiki/Known-Limitations.md).

The commands are in [Release Process](../wiki/Release-Process.md).

---

## 1. Source

Everything marked *(script)* is checked or done by `scripts/release.ps1`, which
refuses to continue if it is not true. They are listed so the checklist still
describes the whole release, not so they are done by hand.

- [ ] Working tree clean (`git status`) *(script)*
- [ ] On `main`, up to date with `origin/main` *(script)*
- [ ] The release commit identified and its hash recorded *(script — it prints
      the commit)*
- [ ] `src/omr_scanner/_version.py` updated to the release version — **the
      only place the version is edited** *(script)*
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

- [ ] `ruff check src tests tools` — clean *(script, and again in CI)*
- [ ] `mypy src/omr_scanner` — clean *(script, and again in CI)*
- [ ] `pytest` — the whole suite; record passed / failed / skipped *(script,
      and again in CI on both Windows and Ubuntu)*
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
      `.\packaging\sandbox\New-SandboxPayload.ps1 -Launch -Wait`, which
      stages the installer, starts a pristine image, runs the mechanical
      checks and waits for the verdict. If it genuinely cannot be run, say so
      explicitly in the release notes rather than leaving it ambiguous
- [ ] **The clean-machine steps that need a person** — in the sandbox window
      the previous step left open, work through the interface and then record
      what you saw with
      `C:\Users\WDAGUtilityAccount\Desktop\OMRFlow\Complete-ManualChecks.ps1`.
      This is where the *installed* build is driven through a recognition run
      and a generated result; the automated portion never touches it. Answer
      `s` for anything you did not actually do
- [ ] `python packaging\audit_dependencies.py dist\OMRFlow` — 0 unresolved
      imports; `python packaging\verify_frozen_imports.py dist\OMRFlow` —
      every imported dependency survived the freeze; and
      `.\scripts\release\Test-SelfContained.ps1` — all checks pass. Run these
      first: they are quick, and they catch most of what the clean-machine
      test catches slowly. **Passing them is not a clean-machine pass.** If
      the clean-machine test genuinely could not be run, record it as *not
      performed*
- [ ] `.\scripts\release\New-ValidationReport.ps1` — the clean-machine result
      is written to `docs/release/validation/`, and the SHA-256 it records is
      the SHA-256 of the installer that will actually be published
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

`scripts/release.ps1` creates and pushes the tag; the `Release` workflow does
the rest and **publishes the release itself**. It is not a draft: Zenodo
archives a repository when GitHub *publishes* a release, and a draft would
never reach it.

- [ ] The tag matches the version exactly: `v<version>` *(script, and the
      workflow refuses to build a tag that disagrees with the source)*
- [ ] Tag created on the release commit, annotated *(script)*
- [ ] Tag pushed atomically with the commit *(script)*
- [ ] The `Release` workflow completed — watch it at
      [Actions](https://github.com/sajidbuet/OMRflow/actions)
- [ ] **Pre-release flag correct** — ticked for Alpha, Beta and RC; clear
      only for a stable release *(workflow, derived from the version)*
- [ ] *Set as the latest release* is **not** ticked for a prerelease
      *(workflow, same derivation)*
- [ ] Installer attached *(workflow; it fails if the file is missing)*
- [ ] `SHA256SUMS.txt` attached *(workflow)*
- [ ] Release notes read — they are generated from the commits and pull
      requests since the previous tag. Add anything the history does not say,
      especially any qualification that was **not** performed
- [ ] The release page shows the expected version and assets

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
| Clean-machine test — automated portion | performed / **not performed** |
| Clean-machine test — steps needing a person | performed / **not performed** |
| Real-data qualification | performed / **not performed** |
| Known release-blocking defects | |
| Released by | |

> Do not tick a box that was not actually done. An honest "not performed"
> is useful; a false tick is worse than no checklist.
