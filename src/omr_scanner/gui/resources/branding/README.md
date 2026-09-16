# Branding assets

The official OMR Flow logo and the application icon derived from it. Bundled
here (inside the installable package, not the top-level `resources/`
directory) for exactly the reason `gui/resources/icons/lucide/` is, and
loaded the same way - through `omr_scanner.gui.branding` and
`importlib.resources`, never a path relative to the current working
directory. See `gui/icons.py` and `gui/branding.py` for why.

## Files

| File | Purpose |
|---|---|
| `logo.svg` | The full "OMR Flow" wordmark - the OMR bubble incorporated into the "O" of "OMR", accent colour `#AC1F24`. Displayed directly (as SVG) in the main window's header. Never recoloured, stretched or edited. |
| `icon_mark.svg` | A simplified, square derivative: just the bubble-in-O from `logo.svg`, re-cropped to its own bounding box. The full two-line wordmark stops being legible much below icon sizes of about 64px, so every application/window/taskbar icon is rendered from this mark instead - same path data, same `#AC1F24` accent, not a redesign. |
| `icon.ico` | Multi-resolution Windows icon (16, 24, 32, 48, 64, 128, 256 px) rasterised from `icon_mark.svg`, each frame PNG-compressed. Used as the window/title-bar icon and is ready to hand to a future packaging step's `--icon` option. |
| `icon_256.png` | A single 256x256 PNG rendering of `icon_mark.svg`, for anything that wants a plain raster image rather than an `.ico` container (e.g. a Linux `.desktop` file, should packaging for that platform be added later). |

`icon.ico` and `icon_256.png` are generated, not hand-drawn - regenerate them
with `scripts/generate_branding_assets.py` (from the repository root) if
`icon_mark.svg` ever changes. Nothing at import time depends on that script;
it is a one-off dev tool, not part of the package.

## Licence

This is the project's own artwork, not a third-party asset - no separate
attribution file is required, unlike `gui/resources/icons/lucide/`.
