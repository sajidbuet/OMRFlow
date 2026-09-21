# Lucide icons

Icons for the Template Designer's toolbars and for the application shell
(the workflow navigator, the page headers, the Project dashboard), bundled
here so they resolve correctly through `omr_scanner.gui.icons.load_icon`
regardless of how OMRFlow is run (source checkout, `python -m`, an installed
package, or a future frozen build) - see that module's docstring for why this
location and `importlib.resources` were chosen over a top-level `resources/`
folder or a Qt `.qrc` file.

## Source

[Lucide](https://lucide.dev) ([github.com/lucide-icons/lucide](https://github.com/lucide-icons/lucide)),
fetched unmodified from the `main` branch at commit `58ef6830c6fdf6374b751ab28b147a921bd31b34`
(2026-09-14).

## Licence

ISC License (Lucide Icons and Contributors), with a small number of icons -
those derived from the [Feather](https://feathericons.com) project Lucide
forked from - additionally MIT licensed (Copyright (c) 2013-present Cole
Bemis). The exact, unmodified upstream text, including which icons fall under
the additional MIT grant, is in `LICENSE` next to this file. Do not edit that
file; if the icon set is ever updated, replace it with the new upstream copy
in full.

Every SVG file here is used exactly as downloaded - none has been edited,
recoloured or resized. Each uses `stroke="currentColor"`, which Qt's SVG icon
engine resolves to the icon's applied text colour, so one file serves both
normal and disabled toolbar states without a second copy.

## Icons in use

| File | Used for |
|---|---|
| `file-plus.svg` | New Template from Image |
| `folder-open.svg` | Open Template |
| `save.svg` | Save |
| `save-all.svg` | Save As |
| `undo-2.svg` | Undo |
| `redo-2.svg` | Redo |
| `scan-line.svg` | Detect Markers |
| `check-check.svg` | Confirm Detected Markers |
| `id-card.svg` | Add Student ID region |
| `list-checks.svg` | Add Question Set region |
| `circle-dot.svg` | Add Questions region |
| `copy-plus.svg` | Create Question Column Array |
| `align-horizontal-distribute-center.svg` | Distribute Columns Evenly |
| `square-dashed.svg` | Add Custom region |
| `file-text.svg` | Add Reference/ignored region |
| `pencil.svg` | Edit Individual Bubbles |
| `rotate-ccw.svg` | Reset Bubbles in Region |
| `circle-check.svg` | Validate |
| `zoom-in.svg` | Zoom In |
| `zoom-out.svg` | Zoom Out |
| `maximize.svg` | Fit to Window |
| `scan.svg` | Actual Size (100%) |
| `grid-3x3.svg` | Grid toggle |
| `copy.svg` | Duplicate region (region list) |
| `trash.svg` | Delete region (region list) |

## Icons in use - the application shell

One icon per workflow stage, named beside the stage it belongs to in
`gui/pages/catalog.py` (`WorkflowPageSpec.icon`), plus the shell's own.

| File | Used for |
|---|---|
| `menu.svg` | The application-menu button in the header |
| `folder.svg` | Stage 1, Project - and the no-project empty state |
| `file-text.svg` | Stage 2, Template (also a toolbar icon above) |
| `settings-2.svg` | Stage 3, Calibrate |
| `printer.svg` | Stage 4, Scan |
| `triangle-alert.svg` | Stage 5, Resolve |
| `users.svg` | Stage 6, Attendance |
| `key-round.svg` | Stage 7, Answer Key |
| `chart-column.svg` | Stage 8, Results |
| `file-chart-column.svg` | Stage 9, Reports |
| `plus.svg` | The Create Project button |
| `folder-open.svg` | The Open Project button (also a toolbar icon above) |
| `file-plus.svg` | Getting Started: create (also a toolbar icon above) |
| `info.svg` | Getting Started: project information |
| `chevron-right.svg` | The click affordance on a Getting Started entry, and View All |

The shell renders these through a tint mask rather than relying on the
widget's palette, so one asset serves a white glyph on the active accent and
a charcoal one on an inactive step - see
`gui/widgets/workflow_step.py::WorkflowStep._paint_icon`. The files
themselves are still unmodified; the tint is applied to the rendered pixmap.

No icon exists yet for a dedicated "orientation" toolbar action: the current
Template Designer has no such action (the orientation mark is adjusted
directly on the canvas, like a registration marker) - see
`docs/template_designer.md`. `file-text.svg` above is the closest existing
action to what a UI polish request described as "Orientation Reference"; it
is the pre-existing "+ Reference" button for a logo/instructions region, not
an orientation control.

Adding another bundled icon: download the file unmodified from
`https://raw.githubusercontent.com/lucide-icons/lucide/main/icons/<name>.svg`,
place it in this directory, add a row above, and reference it with
`load_icon("<name>")` - no other registration step is required.
