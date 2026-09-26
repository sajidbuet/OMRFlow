# Scan quality: is the paper's geometry still trustworthy?

A sheet can register perfectly and still be read wrongly. If the paper was
folded, curled, or lifted off the platen, the scanner still produces an image,
most of the page still looks normal, and the four corner markers are still
crisp — but the bubbles in the damaged region are no longer where the template
says they are. Every value measured there is confidently wrong.

This document describes the check that catches that, what it deliberately does
*not* do, and the numbers behind its thresholds.

## Why marker residuals cannot tell you

`OmrTemplate` mandates exactly four registration markers. Four correspondences
determine a homography exactly, so the markers' own reprojection residual is
zero *by construction*. Measured on this repository's real sample sheet:

```
mean_reprojection_error_px: 0.000
max_reprojection_error_px:  0.000
```

It is a measure of numerical conditioning and says nothing about whether the
paper was flat. `AlignmentMetrics` has always said so in its own docstring:
**interior** control points are what measure geometric accuracy. This feature
provides them.

## The measurement

`omr_scanner.imaging.page_geometry` works on the **already rectified** page.

1. The template's bubble lattice supplies the probe geometry — the only dense,
   precisely located printing a template is guaranteed to describe. No OCR is
   involved; the letters inside the bubbles are treated as ink, never read.
2. Each probe site — a 4×4 block of bubbles — is rendered as a small expected
   pattern and located in the page by normalised cross-correlation
   (`TM_CCOEFF_NORMED`), refined to sub-pixel accuracy by a parabolic fit.
3. The measured displacement field is then **refitted with a global
   homography**, and only the residual that survives that refit is treated as
   evidence of a bend.

Step 3 is what keeps the check quiet on ordinary scans. Rotation, scanner skew,
scale and genuine perspective are all projective — a flat page at an angle is
still a flat page — so a global refit absorbs them entirely. Measured on a
synthetic lattice, projective distortions producing up to 0.37 pitch of raw
displacement leave a non-projective remainder of **0.000 to 0.001**. A real
bend does not vanish that way.

### Two conditions, both required

A distortion is reported only when **both** hold:

* enough probes moved — at least `min_affected_sites`, or two saturated ones;
* the movement is one no flat page could produce — page-level non-projective
  residual at or above `min_nonprojective_pitch`.

Either alone is a false positive waiting to happen. A slightly mis-detected
marker moves every probe without the paper being bent; one or two moved probes
is a mis-match.

### Units

Everything is expressed in **bubble pitch**, never pixels, because pitch is the
length at which a geometric error starts to matter: at half a pitch the sample
window has moved onto the neighbouring bubble. The same sheet at 200 and
600 dpi therefore earns the same verdict.

Row and column pitch are carried **separately** all the way down. An OMR grid
is routinely anisotropic, and collapsing the two axes into one number resamples
the lattice unevenly: one axis keeps a longer period in working space than the
isotropic search window and peak-exclusion radius assume, the true correlation
peak's own shoulder is then counted as a rival, and the site is discarded as
ambiguous. Measured on a 50-by-25 lattice, that cost 12 of 40 probes on a
perfectly flat page.

## The verdict

| Status | Meaning |
|---|---|
| `PASS` | The template-to-paper mapping holds everywhere that matters. |
| `REVIEW` | Part of the page is no longer reliably registered. The recognised values are **kept**; the affected area is named. |
| `UNUSABLE` | What is damaged is what identifies the script, or registration is untrustworthy across most of the page. |

### `evaluated` is not optional reading

`status is PASS` is **not** sufficient to conclude a sheet is sound. A sheet
whose geometry could not be measured also carries `PASS` when nothing about the
*template* is at fault, and "not contradicted" is not "confirmed". Use
`ScanQualityAssessment.is_confirmed_clean`, which is `evaluated and status is
PASS`.

Where the printing itself could not be located on a sheet the template *can*
describe, the assessment carries `GEOMETRY_NOT_VERIFIED` and goes to review.
That is deliberate: a fold destroys the very printing the check needs, so
"could not measure" is one of the shapes a folded sheet arrives in, and a
silent pass there would let exactly the wrong sheet through.

## Issue codes

| Code | Raised when |
|---|---|
| `PAGE_GEOMETRY_DISTORTION` | Printing is displaced by more than a flat page could explain. |
| `REGION_REGISTRATION_ERROR` | One named region is locally misaligned while the page as a whole is not. |
| `PARTIAL_PAGE` | Part of the template's area was never captured by the scan. |
| `CRITICAL_REGION_UNREADABLE` | The identifier or set code is missing or unregistered. |
| `MARKER_GEOMETRY_ERROR` | The markers themselves are mutually inconsistent. |
| `GEOMETRY_NOT_VERIFIED` | The check ran and could not reach a conclusion. |

## What this is not

A scan-quality issue is **not** a conflict about a value. A conflict says
"which digit is this?"; a scan-quality issue says "the coordinate system
stopped being trustworthy here", which makes every value in that area suspect
at once. They are resolved differently — a conflict is corrected, a bent sheet
is re-scanned — so they are separate types.

Concretely, the existing policy is untouched:

* identity and set-code conflicts behave exactly as before;
* an ambiguous or multiply-marked **answer is still not a conflict**;
* scan quality enters the existing queue as one sheet-scope
  `ConflictType.SCAN_QUALITY`, which cannot be answered with a value and is
  acknowledged or deferred instead.

Recognition is **not** abandoned on a flagged sheet. Discarding a script whose
identity and most of whose answers are perfectly legible, because one corner
curled, loses more than it protects.

## Thresholds

All of them live in `ScanQualityThresholds` and `LocalRegistrationConfig` —
nothing is scattered through the implementation. Every default was chosen
against measurements of real scans, and those measurements are quoted in the
docstrings beside each field.

Two constraints are enforced rather than documented, because violating them
makes the measurement meaningless rather than merely aggressive:

* `search_pitch` must stay below 0.5. A lattice is periodic, so a displacement
  of one whole pitch looks like none at all. With a full-pitch window, 22 of 26
  probes on the real sample sheet landed on the alias at exactly ±1.000 pitch.
* `review_displacement_pitch` must stay below 0.5, since the probe cannot
  measure unambiguously past that.

## Cost

About 5 ms on a 2480×3508 sheet, against the ~130 ms recognition already
costs. The probe runs on a page downscaled so one feature pitch is a fixed
number of pixels, so the work is proportional to the number of probe sites
rather than to scan resolution. It holds no state between calls and is safe
inside a multiprocessing worker. It can be turned off with
`RecognitionOptions(check_page_geometry=False)`, which the calibration loop
does because it re-decides already-measured pixels.

## Where it surfaces

* **Scan and Calibration pages** — affected regions are hatched over the
  preview, amber for review and magenta where the identity is in doubt.
  Nothing is drawn on a clean sheet.
* **Review page** — one `Scan quality` entry in the queue, with the verdict,
  the affected areas, the question range, and the measured displacement and
  non-projective figures under the evidence panel.
* **Stored results** — the full assessment rides in `BatchScan.result_json`, so
  a flagged sheet is still explainable after the project is reopened. No schema
  migration was needed and projects written before the feature load unchanged,
  with `scan_quality` absent meaning "never checked".

## Known limitations

* A lattice whose features nearly touch along one axis carries little
  positional information along it. Such sites are reported as *not located*
  rather than guessed at, which is safe but costs sensitivity. Real sheets
  print option letters inside their bubbles, which supplies the missing
  structure; a plain unlettered ring grid at ~86 per cent packing does not.
* Detection begins at roughly 0.7 bubble pitch of local displacement with the
  conservative defaults. A milder bend passes, by design — see the bias note in
  `omr_scanner.domain.scan_quality`.
* A fold severe enough to destroy a registration marker fails Phase 1
  registration before this check runs. That is a stronger rejection, not a
  gap, but it means the geometry codes are not what such a sheet reports.
