# Troubleshooting

Check [Known Limitations](Known-Limitations) first — some behaviour is a
documented gap in this Alpha rather than a fault.

## Finding the logs

| Log | Where |
|---|---|
| Application | `%LOCALAPPDATA%\OMRFlow\logs\` |
| Project | `logs\` inside the project folder |

Paste the path into Explorer's address bar. The first line of a log records
the OMRFlow version.

The most useful single thing to attach to a report is a **diagnostic
bundle**: **Application menu → Tools → Create Diagnostic Bundle…**. It
gathers the version, the platform, the settings, a project health check and
the log tail, and contains **no candidate data** by design.

## Installation and start-up

**"Windows protected your PC" / unknown publisher.**
Expected — Alpha installers are unsigned. *More info* → *Run anyway*, after
[verifying the checksum](Installation#verify-what-you-downloaded). If your
organisation blocks unsigned installers by policy, run
[from source](Development-Setup) instead.

**The installer will not start, or fails part way.**
Check you are on 64-bit Windows 10 1809 or newer. Re-run with a log:
`OMRFlow-…-Setup-x64.exe /LOG=%TEMP%\omrflow-install.log` and attach it to an
[installation problem](https://github.com/sajidbuet/OMRFlow/issues/new/choose).

**Installed, but nothing happens when I launch it.**
Look in `%LOCALAPPDATA%\OMRFlow\logs\` — a start-up failure is logged even
when no window appears. Antivirus quarantining part of the bundle is a common
cause.

## Projects

**"A project is already open elsewhere."**
Another OMRFlow has it, or a previous run crashed and left its lock. You are
shown what held it and offered *cancel*, *read-only* or an explicit
override. OMRFlow never clears a lock by itself — if you are certain nothing
else is running, override it.

**"This project was created by a newer version."**
Install the newer OMRFlow. A project is never downgraded. See
[Upgrade Compatibility](Upgrade-Compatibility).

**A project will not open, or reports damage.**
Run **Application menu → Tools → Project Health / Recovery…**. It reports
integrity, foreign keys, stale jobs and missing scans, and restores a backup
if you have one.

## Recognition

**Many sheets `failed`.**
Almost always registration — the four printed markers could not be found:

- Is the **whole page** on the image, including all four markers?
- Is the resolution in the 150–300 dpi range?
- Does the template match **this** printed sheet, not a previous revision?
- Are the scans very dark, very light or heavily compressed?

**Many fields `uncertain`.**
The thresholds do not suit your paper and scanner. Run the **Calibrate**
stage against representative real scans and adjust them — see
[Processing](Processing#calibrate-before-a-real-batch).

**Answers read but shifted by one, or in the wrong block.**
The template's regions do not line up with the printed sheet. Reopen it,
re-detect the markers, and check the question-block geometry.

**Wrong or missing set codes.**
The set codes defined in **Project Configuration** must match exactly what is
printed and marked on the sheets.

See [Recognition Symbols](Recognition-Symbols) for what each status means.

## Scanner and image problems

- Feed sheets straight; skew is corrected but not unlimited.
- Keep one resolution for a batch.
- Avoid aggressive JPEG compression — the artefacts land on bubble edges.
- Clean the scanner glass; a streak across a bubble column reads as marks.
- Photographs of sheets may work but are not a tested path.

## Excel import problems

**My attendance workbook will not import.**
Start from **Download Sample Template…** and compare. A title above the
candidate table is fine; OMRFlow searches for the header row. Unexpected
columns, several header rows or merged blocks may not be handled — this area
is
🟡 [synthetically tested only](Known-Limitations). Please
[report it](https://github.com/sajidbuet/OMRFlow/issues/new/choose) with
names and roll numbers replaced.

**Names or roll numbers look wrong after import.**
Excel silently turns a roll number like `0015` into the number 15. Format
identifier columns as **Text** before saving.

## Reporting problems

**"Generate" refuses.**
**Validate** first; it names what is missing — an unverified key, an
unreconciled roster, a set with no result template.

**The logo is missing from the generated workbook.**
Report it. Image preservation is deliberate and tested; a loss is a defect.

**PDF export is unavailable.**
Install LibreOffice. The XLSX is still generated without it.

**Ranks look wrong.**
OMRFlow uses standard competition ranking: `90, 88, 88, 85` → `1, 2, 2, 4`.

## Performance

**The Results, Resolve or Attendance table is slow with a big cohort.**
Known: those three displays are not lazily loaded, though the backend
handles 100,000 rows. See [Known Limitations](Known-Limitations).

**A batch is slower than expected.**
Check **Settings → Processing**; *Automatic* uses several workers. Processing
competes with other work on the machine — a throughput figure measured on a
busy machine is not comparable to one measured on an idle machine.

## Still stuck

[SUPPORT.md](https://github.com/sajidbuet/OMRFlow/blob/main/SUPPORT.md)
explains what makes a report actionable and how to sanitise a reproduction
first. Security and privacy problems go through
[SECURITY.md](https://github.com/sajidbuet/OMRFlow/blob/main/SECURITY.md),
**not** a public issue.
