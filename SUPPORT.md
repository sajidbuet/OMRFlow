# Getting help with OMRFlow

OMRFlow is an Alpha release. Reports from testers are the point of this
stage, so a well-formed one is genuinely useful.

| I want to… | Go here |
|---|---|
| Report something broken | [Open a bug report](https://github.com/sajidbuet/OMRflow/issues/new/choose) |
| Report a recognition mistake | [Recognition problem](https://github.com/sajidbuet/OMRflow/issues/new/choose) — a separate template, because these need different information |
| Report an installer problem | [Installation problem](https://github.com/sajidbuet/OMRflow/issues/new/choose) |
| Report a wrong result or workbook | [Result / Excel problem](https://github.com/sajidbuet/OMRflow/issues/new/choose) |
| Suggest something | [Feature request](https://github.com/sajidbuet/OMRflow/issues/new/choose) |
| Fix or improve the docs | [Documentation issue](https://github.com/sajidbuet/OMRflow/issues/new/choose) |
| Report a security or privacy problem | **Not an issue** — see [SECURITY.md](SECURITY.md) |
| Ask how something works | [Documentation](docs/wiki/Home.md) first, then open a question issue |

## Before reporting

1. Check you are on the latest release — [Releases](https://github.com/sajidbuet/OMRflow/releases).
2. Search [existing issues](https://github.com/sajidbuet/OMRflow/issues).
3. Check [Known Limitations](docs/wiki/Known-Limitations.md). Some behaviour
   is a documented gap in this Alpha rather than a defect, and it saves
   everyone time to know which.

## Finding your version

Three ways, in order of convenience:

- **The title bar** reads `OMRFlow 0.1.0-alpha.1`.
- **Application menu → Help → About OMRFlow** shows the version, the release
  channel, and — hover the version line — the exact build identifier
  including the source commit. Quote the build identifier if you have it; it
  identifies the source revision exactly.
- **The log file** records the version in its first line (see below).

## Finding your logs

OMRFlow writes two kinds of log:

- **Application log** — `%LOCALAPPDATA%\OMRFlow\logs\`. Paste this path into
  Explorer's address bar. Covers start-up, configuration and anything not
  tied to a project.
- **Project log** — inside the project folder, under `logs\`. Covers work
  done on that project.

The most useful single attachment is a **diagnostic bundle**: *Application
menu → Tools → Create Diagnostic Bundle…*. It collects the version, the
platform, the processing settings, a project health check and the tail of the
log into one zip file, and it is designed to contain **no candidate names,
roll numbers, recognised answers, answer keys, scores, attendance records,
scan images or the project database**. Prefer it over pasting logs by hand.

## What makes a report actionable

- What you did, what you expected, what happened instead.
- The version and your Windows version.
- Whether it happens every time or occasionally.
- A diagnostic bundle, or the relevant log lines.
- A screenshot, if the problem is visible.
- For recognition problems: a **sanitised** sample sheet and the template.

## Sanitising before you upload

> **GitHub issues are public. Anything you attach is public permanently, and
> deleting it later does not un-publish it.**

Never attach:

- real candidate names, roll numbers or student IDs;
- real attendance workbooks or candidate rosters;
- real answer keys, before or after an examination;
- scans of real answer sheets;
- a real project folder or its `database.sqlite`;
- examination content that is not already public.

Instead:

- **Reproduce with synthetic data.** OMRFlow can generate a labelled
  synthetic dataset from any template: *Application menu → Tools → Developer
  / Testing → Generate Synthetic Test Dataset…*. A synthetic sheet that
  reproduces the problem is the ideal attachment — it carries no one's data
  and the maintainer can run it directly.
- **Redact an image** before attaching it: black out the candidate ID region
  and any handwritten name. Check the whole page, including margins and any
  invigilator annotation.
- **Trim a workbook** to a few fabricated rows that still reproduce the
  problem, and replace every name and roll number.
- **Check the log** you are pasting. OMRFlow is designed not to log candidate
  data, but read it before posting rather than assuming.

If a problem genuinely cannot be reproduced without real data, say so in the
issue and describe the data's shape — column headings, row count, which
fields are blank, what the set codes look like. Do not attach it and wait to
be told not to.

## Response expectations

OMRFlow is maintained by one person alongside other work. There is no service
level agreement, and Alpha-stage reports are triaged by how much they affect
the qualification work in progress. A clear report with a synthetic
reproduction is dealt with far faster than one that needs a conversation
first.
