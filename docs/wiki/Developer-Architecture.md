# Architecture

OMRFlow is a layered Python application with a PySide6 (Qt 6) desktop
interface. Dependencies point downward only, and the layering is **enforced
by a test** that parses every source file — the GUI imports no OpenCV or
SQLAlchemy, `imaging` imports no Qt, `domain` imports nothing above it.

> The authoritative document is
> **[`docs/ARCHITECTURE.md`](https://github.com/sajidbuet/OMRflow/blob/main/docs/ARCHITECTURE.md)**.
> This page is an orientation.

## The layers

```text
gui                evaluation          tools
  |                    |                 |
  +--------- services -----------------+
                  |
   domain   database   imaging   recognition   reporting
                  |
        utils    config    errors
```

| Package | Owns |
|---|---|
| `domain` | Data shapes and their validity rules; pure computation |
| `imaging` | Pixel algorithms: preprocessing, marker detection, rectification, bubble metrics |
| `recognition` | Turning measurements into values with confidence |
| `database` | Schema, migrations, engine and session lifetime |
| `services` | Multi-step operations and all side effects |
| `reporting` | XLSX and PDF generation mechanics |
| `gui` | Windows, pages and dialogs — presentation and intent only |
| `evaluation` | Judging the engine: synthetic datasets, benchmarks, the stress and qualification harnesses |
| `tools` | Headless command line utilities |

## The rule that shapes the code

> Domain logic must not live inside GUI event handlers.

A useful test: *would this still be meaningful in a command line version of
OMRFlow?* If yes, it belongs in a service or lower.

In the main window this appears as a deliberate split: `_prompt_*` methods
own the modal dialogs and contain no logic, while the methods that contain
the behaviour are what the tests drive.

## The application shell

The window is three bands — one compact chrome row, the stacked pages, and a
status footer. The chrome row is also the title bar: the window is frameless,
so the application menu, the wordmark, the workflow ribbon and the
minimise/maximise/close buttons all share a single 46-pixel line, and moving
and resizing go through `QWindow.startSystemMove()` and
`startSystemResize()` rather than being re-implemented by hand.

The workflow ribbon is always **one line** — all nine stages when they fit,
a horizontally scrolled strip when they do not, and the current stage alone
plus a selector listing all nine when even scrolling would show barely one at
a time. Every visual constant lives in `gui/theme/tokens.py`, which imports
no Qt; each shell component decides its own layout from its own width. See the
*application shell* section of `docs/ARCHITECTURE.md`, which also records the
one thing framelessness costs.

## Key decisions, recorded

Architecture decision records live in
[`docs/decisions/`](https://github.com/sajidbuet/OMRflow/tree/main/docs/decisions):
the choice of a Python/PySide6 desktop application, the on-disk project
layout, the migration approach, and normalised template coordinates.

## Further reading

| Document | Covers |
|---|---|
| `docs/ARCHITECTURE.md` | The layering, in full |
| `docs/DATA_MODEL.md` | Every database table |
| `docs/IMAGE_PROCESSING.md` | The alignment pipeline |
| `docs/recognition_engine.md` | How a mark becomes a value |
| `docs/TEMPLATE_FORMAT.md` | The `.omrt` document |
| `docs/TESTING.md` | The testing strategy |
