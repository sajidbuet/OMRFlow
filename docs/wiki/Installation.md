# Installation

## System requirements

| | |
|---|---|
| Operating system | Windows 10 version 1809 (build 17763) or newer, 64-bit |
| Disk space | About 350 MB for the application, plus room for your projects |
| Memory | 4 GB minimum; 8 GB or more if you process large batches |
| Processor | Any x86-64 processor. OMRFlow reads several sheets at once, so more cores make a batch faster |
| Python | **Not required.** The installer bundles everything |

**About those Windows versions.** Windows 10 1809 is the floor the bundled Qt
runtime supports, and the installer refuses to run below it. OMRFlow has been
built and tested on **Windows 11**. Windows 10 is expected to work but has not
been tested — if you use it, please
[say whether it worked](https://github.com/sajidbuet/OMRflow/issues/new/choose).

32-bit Windows and Windows on ARM are not supported.

## Download

Get the installer from the
[Releases page](https://github.com/sajidbuet/OMRflow/releases):

```text
OMRFlow-0.1.0-alpha.2-Setup-x64.exe
```

### Verify what you downloaded

Each release includes `SHA256SUMS.txt`. Because Alpha installers are
unsigned, this checksum is the only way to confirm you have the file the
maintainer built. In PowerShell, from your Downloads folder:

```powershell
certutil -hashfile OMRFlow-0.1.0-alpha.2-Setup-x64.exe SHA256
```

Compare the result with the line for that filename in `SHA256SUMS.txt`. If
they differ, delete the file and download it again — do not install it.

## Windows SmartScreen

> **Expected:** Windows will warn that the publisher is unknown and may say
> *"Windows protected your PC"*.

This is not a sign that anything is wrong. It happens because OMRFlow's
Alpha installers are **not code-signed** — a signing certificate is a paid
annual expense and is a
[Phase 11C](Development-Roadmap#phase-11c--release-candidate--stable-release)
item, not something silently implied to be in place.

To proceed: click **More info**, then **Run anyway**. Verify the SHA-256
checksum first if you have not already.

If your organisation blocks unsigned installers by policy, you cannot bypass
that, and should not try. Run OMRFlow
[from source](Development-Setup) instead, or wait for a signed release.

## Install

1. Run the installer.
2. Accept the MIT licence.
3. Choose where to install. The default is per-user
   (`%LOCALAPPDATA%\Programs\OMRFlow`) and needs **no administrator rights**.
   An administrator may choose an all-users installation instead.
4. Optionally tick **Create a desktop icon**.
5. Install, then launch.

The installer states the Alpha warning during installation as well, because
that is the moment you are deciding whether to trust the build.

### Installing silently

For a managed rollout:

```powershell
OMRFlow-0.1.0-alpha.2-Setup-x64.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER
```

Add `/DIR="C:\Path\To\Install"` to choose the location, and `/LOG=install.log`
to record what happened.

## First launch

OMRFlow opens on the **Project** stage with no project open. The window title
reads `OMRFlow 0.1.0-alpha.2`, and *Getting Started* offers **Create a new
project** and **Open an existing project**.

Go to [Quick Start](Quick-Start) next.

## Checking your version

Three ways, in increasing precision:

- The **title bar**: `OMRFlow 0.1.0-alpha.2`.
- **Application menu → Help → About OMRFlow**: the version, the release
  channel (*Alpha*), the licence, and a link to the repository. **Hover the
  version line** to see the full build identifier including the source
  commit, for example `0.1.0-alpha.2+a1b2c3d`.
- The **log file** records the version on its first line — see
  [Troubleshooting](Troubleshooting#finding-the-logs).

Quote the build identifier in a bug report if you have it; it identifies the
exact source revision.

## Where things are installed

| What | Where | Removed by uninstall? |
|---|---|---|
| The application | `%LOCALAPPDATA%\Programs\OMRFlow` (per-user default) | Yes |
| Your settings | `%LOCALAPPDATA%\OMRFlow\` | **No** |
| Application logs | `%LOCALAPPDATA%\OMRFlow\logs\` | **No** |
| Your projects | Wherever you chose to create them | **No** |

OMRFlow deliberately keeps no examination data inside its installation
directory, so installing, upgrading and uninstalling cannot touch your work.
See [Backup & Data Retention](Backup-and-Data-Retention).

## Upgrading

See [Upgrading OMRFlow](Upgrading-OMRFlow). In short: **back up any project
you care about first.** Prerelease versions make no compatibility promises to
each other.

## Uninstalling

See [Uninstalling](Uninstalling).

## Running from source instead

If you cannot install an unsigned binary, or you want to develop OMRFlow,
see [Development Setup](Development-Setup).
