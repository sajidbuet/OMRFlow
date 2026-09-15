# Icons

Reserved for icons that are not tied to a specific runtime asset lookup (for
example, a future packaged application/window icon or installer artwork).

**The Template Designer's toolbar icons are not here.** They live at
`src/omr_scanner/gui/resources/icons/lucide/`, inside the installable
package rather than this top-level, dev-only `resources/` directory, because
only files inside the package are reliably bundled by the project's normal
packaging mechanism (this directory has no `package-data` entry and is never
installed - see `pyproject.toml`). See that directory's own `README.md` for
the icon set, licence and full list in use, and
`src/omr_scanner/gui/icons.py` for how they are loaded.

When an icon or asset genuinely belongs here (used by path, not imported by
the running package):

- prefer SVG, with PNG fallbacks only where Qt needs them;
- keep a single visual style across the application;
- record the source and licence of every icon in this file - third-party icon
  sets almost always require attribution;
- load them through Qt's resource system or a path resolved from this directory,
  never a hard-coded absolute path.

Index of committed icons:

_None yet._
