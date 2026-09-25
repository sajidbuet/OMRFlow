# Backup and Data Retention

## What a project contains

A project is a single folder. Everything about one examination lives inside
it:

```text
Demo Exam\
├── project.json          the project's identity and settings (format version 3)
├── database.sqlite       candidates, recognised results, decisions, marks
├── logs\                 what OMRFlow did on this project
├── answer_keys\
├── candidate_lists\      imported attendance workbooks
└── exports\              generated report workbooks
```

**Treat the whole folder as confidential.** `database.sqlite` holds candidate
identifiers, recognised answers, attendance states and marks; the imported
workbooks hold names and roll numbers. This is why nothing from a project
belongs in a public issue — see
[SUPPORT.md](https://github.com/sajidbuet/OMRFlow/blob/main/SUPPORT.md).

Imported scans are referenced by content hash and provenance rather than
copied wholesale, so **the folder your scans live in matters too**. If you
move or delete the source images, OMRFlow can tell they are missing or have
changed, but it cannot re-read them.

## Where else OMRFlow keeps things

| What | Where |
|---|---|
| Application settings (worker count, reviewer name, recent projects) | `%LOCALAPPDATA%\OMRFlow\` |
| Application log | `%LOCALAPPDATA%\OMRFlow\logs\` |

Neither contains candidate data. Neither is removed by uninstalling.

## Backing up

### Through OMRFlow

**Application menu → Tools → Project Health / Recovery…** takes a snapshot of
the project database using SQLite's own online backup API. This is safe
against a database that is open, which a plain file copy is not: copying
`database.sqlite` while OMRFlow has it open can produce a file that looks
fine and is subtly broken.

A manifest is written **only after** the backup has finished and been hashed,
so an interrupted backup can never be mistaken for a complete one.

The same dialog restores a backup.

### By hand

With **OMRFlow closed**, copy the entire project folder. Closing matters for
the same reason as above.

Keep the source scans too, if you may need to reprocess.

### What to back up, and when

| When | What |
|---|---|
| Before upgrading OMRFlow — **always**, on a prerelease | The whole project folder |
| After processing a batch, before resolving conflicts | The whole project folder |
| After scoring, before generating reports | At least a database backup |
| After generating final reports | The folder, plus the generated workbooks stored separately |

The pattern worth keeping: back up at each point where the **next** step
would be expensive to redo.

## Retention

OMRFlow deletes nothing on its own.

- **Corrections are append-only.** Resolving a conflict does not overwrite
  what recognition read; both are kept, with who decided and why.
- **Reprocessing archives the superseded reading** before resetting a sheet,
  so a sheet processed twice keeps both readings on record.
- **Reports are written to `exports\`** and are never overwritten silently.

How long to keep a project after an examination is your institution's
decision. OMRFlow imposes no retention policy and will not remove anything
for you.

## What uninstalling removes

| | Removed? |
|---|---|
| The application files | Yes |
| Your projects | **No** |
| Your settings and logs | **No** |

There is deliberately no "also delete my data" option in the uninstaller: it
is too easy to click, and examination data is too expensive to recreate. See
[Uninstalling](Uninstalling).

## Before reinstalling or moving machines

Copy:

1. Every project folder you need.
2. The source scan folders, if you might reprocess.
3. `%LOCALAPPDATA%\OMRFlow\` if you want your settings — optional; it is
   only preferences.

Then see [Moving Projects](Moving-Projects), which covers what OMRFlow does
when a project or its scans appear at a different path.

## Related

- [Upgrading OMRFlow](Upgrading-OMRFlow)
- [Moving Projects](Moving-Projects)
- [Recovery After Interrupted Processing](Recovery-After-Interrupted-Processing)
- [Project Format](Project-Format)
