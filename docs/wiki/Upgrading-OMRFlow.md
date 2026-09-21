# Upgrading OMRFlow

> ### ⚠️ Back up your projects before upgrading a prerelease
>
> **Prerelease versions make no compatibility promises to each other.** An
> Alpha may change the database schema, and a migration is applied **in
> place and cannot be undone**. A project opened by a newer OMRFlow cannot be
> opened by the older one again.
>
> Copy the whole project folder somewhere safe first. See
> [Backup & Data Retention](Backup-and-Data-Retention).

## What an upgrade does and does not touch

| | Affected by upgrading? |
|---|---|
| The application in `%LOCALAPPDATA%\Programs\OMRFlow` | **Replaced** |
| Your settings in `%LOCALAPPDATA%\OMRFlow\` | Kept |
| Application logs in `%LOCALAPPDATA%\OMRFlow\logs\` | Kept |
| Your project folders | Kept — but see *migrations* below |

The installer writes nothing into your projects. OMRFlow itself may, the
first time it opens one.

## How to upgrade

1. **Close OMRFlow.** A project is locked while it is open.
2. **Back up any project you care about** — copy the folder.
3. Download the new installer and
   [verify its checksum](Installation#verify-what-you-downloaded).
4. Run it. It detects the previous installation and replaces it; there is no
   need to uninstall first.
5. Launch OMRFlow and check **Application menu → Help → About OMRFlow** shows
   the version you expected.
6. Open a project. If the schema has moved on, OMRFlow migrates it — see
   below.

## Database migrations

Each OMRFlow build expects a particular database schema version. This release
expects **schema 9**.

When you open a project written by an older schema, OMRFlow applies the
migrations between them automatically:

- Migrations are **append-only** — a released migration is never edited, so
  two builds at the same schema version always agree about the shape of the
  data.
- A migration runs **in place**. There is no automatic backup and **no way to
  reverse it**. This is why the backup step above is not optional.
- If a migration cannot complete, OMRFlow **stops with a message** and leaves
  the project as it was rather than half-migrating it.
- A project written by a **newer** schema than the running build understands
  is **refused**, with a message naming both versions. It is not opened
  read-only and it is not downgraded. Install the newer OMRFlow to open it.

You can see the schema a build expects in the diagnostic bundle
(**Application menu → Tools → Create Diagnostic Bundle…**), as
`expected_schema_version`.

### Before a migration, if the project matters

Use **Application menu → Tools → Project Health / Recovery…** to take a
backup through OMRFlow's own mechanism, which snapshots the database safely
using SQLite's online backup API rather than copying a file that may be
mid-write. Then upgrade.

## Downgrading

**Not supported.** Once a project has been migrated forward, an older
OMRFlow will refuse it. If you need to go back, reinstall the older version
and restore your pre-upgrade backup.

## Upgrading between Alpha releases specifically

Because `0.1.0-alpha.1` is the **first** release, no upgrade path has been
exercised yet — there is nothing to upgrade from. Migration code is covered
by automated tests against databases created by previous *schema* versions,
but an upgrade between two *released builds* has never been performed.

Treat the first Alpha-to-Alpha upgrade with corresponding caution: back up,
and check a sample of results after opening a migrated project.

## Checklist

- [ ] OMRFlow closed
- [ ] Every project I care about copied somewhere else
- [ ] Installer checksum verified
- [ ] Installed, and the About dialog shows the new version
- [ ] A project opens, and a sample of its results still looks right

## Related

- [Installation](Installation)
- [Backup & Data Retention](Backup-and-Data-Retention)
- [Upgrade Compatibility](Upgrade-Compatibility)
- [Uninstalling](Uninstalling)
