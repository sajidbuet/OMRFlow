# Image fixtures

No images are committed, and after Phase 1 that is a deliberate state rather
than an unfinished one.

```text
images/
├── synthetic/    generated from a known geometric transform
├── anonymized/   real scans, all candidate identifiers removed
└── regression/   minimal inputs that reproduced a specific defect
```

## synthetic/

**Empty on purpose.** Phase 1's synthetic sheets are generated in memory by
`omr_scanner.imaging.synthetic` from a seeded specification, so the geometry
tests need no committed image at all. A generator is better than a committed
PNG here: the ground-truth marker and control-point coordinates come back with
the page, and a file would have to carry them alongside and could drift.

Commit a file here only for something the generator cannot express and a test
genuinely needs.

## anonymized/

**Empty, and this is the largest open risk in the alignment engine.** Every
accuracy number in `docs/IMAGE_PROCESSING.md` comes from synthetic sheets. No
real scan has been processed. Pencil texture, scanner shadows, print
registration error and paper texture are not represented anywhere in the suite.

Real-world regression testing is deferred until anonymised sample sheets exist.
`docs/TESTING.md` has the anonymisation checklist and the procedure for adding a
scan as a regression fixture once one is available.

## regression/

**Empty.** Nothing has regressed yet. A file arrives here only with the fix for
a specific defect, and with a one-line note below naming that defect.

## Index of committed images

Keep one line per file.

_None yet._
