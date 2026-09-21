# Projects

A project is one examination. It is a single folder on your disk containing
its database, its imported material and its generated reports — see
[Project Format](Project-Format) for the layout.

## Creating one

On the **Project** stage, **Create Project**, then choose a parent folder and
a name. The name becomes the folder name, so it has to be a legal folder
name; the *examination* name, which appears on reports, does not and is set
separately.

OMRFlow creates the folder structure, initialises the database at the current
schema version, and opens **Project Configuration** automatically.

An existing non-empty folder is refused rather than written into.

## Project Configuration

**Application menu → File → Project Configuration…**

- **Examination name** — the title used on generated reports. Free text.
- **Question-paper sets** — one per paper your candidates sat. Each has a
  code (the machine-readable key, which must match what is printed on the
  sheets) and a description.

A set cannot be deleted once something references it; OMRFlow says what is
blocking the deletion rather than cascading it.

## Opening and closing

- **Open Project** on the Project stage, or **Application menu → File → Open
  Project…**
- **Open Recent** lists projects you have used, and the Project dashboard's
  *Recent Projects* card shows the most recent few. An entry whose folder has
  moved or been deleted is shown, disabled, and says so.
- **Application menu → File → Close Project** releases the project.

### Only one process at a time

A project is locked while open. A second OMRFlow trying to open the same
project is refused and told who holds it. If a previous run crashed, the lock
is left behind on purpose — OMRFlow never removes it automatically — and you
are shown what held it and offered *cancel*, *read-only* or an explicit
override.

## What the dashboard shows

With a project open, the Project stage lists its examination name, its sets,
its description, its location on disk, its project identifier, and when it
was created and last modified.

## Related

- [Project Format](Project-Format)
- [Examination Sets](Examination-Sets)
- [Moving Projects](Moving-Projects)
- [Backup & Data Retention](Backup-and-Data-Retention)
