# Moving Projects

## Moving a project folder

A project is self-contained and can be moved or copied anywhere, including to
another machine. With **OMRFlow closed**, move the folder, then use **Open
Project** to point at the new location.

Nothing inside a project stores its own absolute path, so the project itself
does not care where it lives.

## Moving the source scans

This is the part that needs attention. **Imported scans are referenced, not
copied into the project.** OMRFlow records each image's path along with a
hash of its contents, so it can tell the difference between:

- **present** — the file is where it was expected and unchanged;
- **missing** — it is not there any more;
- **changed** — something is there, but it is not the same image.

Move the scans and they become *missing*. The project keeps every recognised
result — those are in the database — but OMRFlow can no longer show you the
original sheet or reprocess it.

### Relinking

OMRFlow can relink a moved scan, and verifies the content hash when it does,
so a relink cannot silently attach the wrong image. Use **Application menu →
Tools → Project Health / Recovery…** to see which scans are missing or
changed.

### If you are moving anything, move both

Keep the project folder and its scan folder together, and relink after the
move. The simplest arrangement is to keep the scans inside or beside the
project folder from the start.

## Moving to another machine

1. Close OMRFlow.
2. Copy the project folder **and** the scan folders.
3. [Install OMRFlow](Installation) on the new machine — the same version, if
   you can. A newer one may migrate the project, which cannot be undone.
4. **Open Project**.
5. Run the project health check and relink any scans that moved.
6. Optionally copy `%LOCALAPPDATA%\OMRFlow\` for your settings; it holds
   preferences only, no examination data.

## Renaming

Renaming the *folder* is fine. The project's display name and examination
name are stored inside it and are unaffected; change those in **Project
Configuration**.

## Related

- [Backup & Data Retention](Backup-and-Data-Retention)
- [Project Format](Project-Format)
- [Upgrading OMRFlow](Upgrading-OMRFlow)
