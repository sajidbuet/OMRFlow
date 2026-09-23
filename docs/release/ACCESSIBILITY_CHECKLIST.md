# Accessibility Checklist

The initial checklist for an **Alpha** release. It is not a full audit, and
does not claim to be one: it covers what can be checked without assistive
technology on hand, and records explicitly what still needs a screen reader
and a person who relies on one.

The release qualification matrix requires *Initial* at Alpha and a full audit
at Beta — see
[Development Roadmap](../wiki/Development-Roadmap.md#release-qualification-matrix).

## Result for `0.1.0-alpha.1`

| | Status |
|---|---|
| Keyboard | ✅ Verified |
| Focus visibility | ✅ Verified |
| Contrast | ✅ Verified, computed rather than judged |
| Colour independence | ✅ Verified |
| Text scaling and high DPI | ✅ Verified, 100–200% |
| Accessible names | ✅ Set and asserted |
| **Screen reader** | 🟠 **Not tested** |
| Full audit with assistive-technology users | 🟠 Not performed |

> **The shell was rebuilt after this result was recorded.** The single chrome
> row, the one-line workflow ribbon, its narrow-mode stage selector, the
> density controls and the custom window buttons all postdate it. Every item
> below marked `[x]` for those components is backed by an automated test, and
> the row's behaviour under a larger *application font* is exercised by the
> suite — but the **manual** pass at 125%, 150%, 175% and 200% Windows
> *display* scaling has not been repeated on the new shell. Treat the "Text
> scaling and high DPI" row above as verified for the pages and pending for
> the chrome row until that pass is redone.

---

## Keyboard

- [x] Every control in the shell is reachable by Tab
- [x] Shift+Tab moves back through the same order
- [x] The application menu is reachable and openable from the keyboard —
      it is the first stop in the tab order, so the whole File/Tools/Help
      hierarchy is available without a mouse
- [x] Every workflow stage is reachable by Tab
- [x] The workflow ribbon additionally supports Left/Right, Up/Down, Home and
      End — both axes, so an operator never has to know which of the three
      layouts is on screen to use the keyboard
- [x] Arrow movement skips disabled stages rather than stopping on a dead end
- [x] The previous/next stage buttons, the ribbon density `-`/`+` buttons and
      the minimise/maximise/close buttons are all keyboard-focusable and named
- [x] **The narrow layout's stage selector is not hover-only.** Hovering the
      current stage reveals the other eight, and so does clicking it, and so
      does Down from the keyboard; Escape and a click outside close it
- [x] Space and Enter activate a focused control
- [x] No keyboard trap: an unhandled key falls through, so Tab can always
      leave a component
- [x] Window state has its usual keyboard routes as well as its buttons —
      Alt+F4, Win+Up and Win+Down all still work, because the window is
      frameless but otherwise an ordinary top-level window
- [x] Dialog tab order is sensible, and Cancel — not the destructive action —
      is the default button on the qualification launcher

*Automated coverage:* `tests/gui/test_workflow_ribbon.py` (classes E and K),
`tests/gui/test_window_chrome.py` (classes C and E),
`tests/gui/test_app_shell.py` (class J).

## Focus visibility

- [x] Every focusable control draws a visible focus indicator
- [x] The workflow step's focus ring is drawn as an accent ring **plus a
      light halo**, so it stays visible on the active step, whose fill is
      that same accent — a single-colour ring disappeared exactly where it
      mattered most
- [x] Focus indicators survive every responsive layout change

## Contrast

- [x] Body and secondary text meet **WCAG AA** (4.5:1) against every surface
      they are used on
- [x] Disabled text meets the large-text threshold (3:1) and remains legible
- [x] The focus ring is visible against both a white surface and the accent
- [x] Status dots are visible against the footer

*Computed, not judged.* `tests/unit/test_theme_tokens.py` implements the
WCAG 2.1 relative-luminance formula and asserts each ratio. This caught a
real defect: `TEXT_TERTIARY` was a perfectly ordinary-looking grey that
failed at 4.45:1 while its own docstring claimed it passed.

## Colour independence

No state is carried by colour alone:

- [x] The **active** workflow stage is also **bold**
- [x] A **disabled** stage also has a **dashed** outline and explains itself
      in its tooltip, and does not respond to hover
- [x] The footer's status is a **word** — the coloured dot is a redundant
      accent on it
- [x] Preflight and validation results are words (PASS/FAIL/WARN), not
      colours
- [x] Recognition outcomes are symbols and named statuses, not colours

## Text scaling and high DPI

- [x] Verified at display scaling 100%, 125%, 150%, 175% and 200%
- [x] Verified at application font scaling 125%, 150%, 200% and 250%
- [x] **No text is clipped** and **no label is elided** at any of them
- [x] A larger font changes the *layout* — the ribbon starts scrolling, and
      then collapses to the current stage — rather than shrinking the text.
      The ribbon's density `-`/`+` controls change padding only and never the
      font, for the same reason
- [x] The logo is a vector and keeps its aspect ratio at every scale
- [x] Custom-painted geometry (the chevrons) is computed per repaint, so it
      is sharp and unclipped at every device pixel ratio
- [x] No fixed heights on text-bearing controls in the stylesheet — a
      stylesheet that pins a height clips its label when the font grows

## Accessible names and descriptions

- [x] The icon-only application-menu button has the accessible name
      *"Application menu"* — an icon-only control must never be announced as
      an unnamed button
- [x] Every workflow stage has an accessible name including its number, and
      a description from the stage's summary
- [x] A disabled stage's accessible description states **why** it is disabled
- [x] Clickable card rows carry their heading as an accessible name
- [x] Every icon-only control in the chrome row is named: the density
      buttons, the previous/next stage arrows, and the window buttons —
      the last using Windows' own wording (*Minimise*, *Maximise*,
      *Restore Down*, *Close*), so a screen-reader user hears the same names
      here as on every other window on the machine
- [x] Tooltips are used where a label may be elided, and never as the only
      route to essential information — a long project title in the footer
      elides with its full value in the tooltip, and the version beside it
      is never pushed off the row

## Not yet done

- [ ] 🟠 **Screen reader testing.** Accessible names, descriptions and status
      tips are set throughout and asserted by tests, but **no screen reader
      has been used to work through the application**. Announcement order,
      the usefulness of the announcements, and live-region behaviour during a
      batch are all unverified.
- [ ] 🟠 **Testing with people who rely on assistive technology.** No user
      testing has been done.
- [ ] 🟠 **High-contrast and forced-colours modes.** OMRFlow applies its own
      stylesheet; behaviour under a Windows high-contrast theme is untested
      and may well be poor.
- [ ] 🟠 **Reduced-motion.** Not applicable today — the interface has no
      animation — but not deliberately handled either.
- [ ] ⚪ No dark theme.
- [ ] ⚪ English only.

These are recorded as Phase 11B/11C work in the
[Development Roadmap](../wiki/Development-Roadmap.md) and in
[Known Limitations](../wiki/Known-Limitations.md).

## Related

- [Release Checklist](RELEASE_CHECKLIST.md)
- [Known Limitations](../wiki/Known-Limitations.md)
- `docs/TESTING.md` — the GUI testing policy
