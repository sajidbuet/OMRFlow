"""The vocabulary of scan quality: is the paper's geometry still trustworthy?

Purpose:
    Name the ways a *physically damaged sheet* can defeat registration, so that
    the rest of the application can branch on an enum rather than on a boolean
    ``bad_scan`` that says nothing about what went wrong or where.

Responsibilities:
    * :class:`ScanQualityStatus` - the three-way verdict for one sheet.
    * :class:`ScanQualityIssueCode` - the taxonomy of what was wrong.
    * :class:`PageArea` - a coarse "where on the page", for a human sentence.
    * :class:`ScanQualityIssue` - one finding, with the evidence behind it.
    * :class:`ScanQualityAssessment` - every finding for one sheet, plus the
      measurements they were derived from.
    * :class:`ScanQualityThresholds` - every tunable number, in one place.

What does NOT belong here:
    Pixels, templates, Qt or persistence. Measuring a page is
    :mod:`omr_scanner.imaging.page_geometry`; deciding what the measurements
    mean for a given template is :mod:`omr_scanner.services.scan_quality`.

Why this is not a :class:`~omr_scanner.domain.review.ConflictType`:
    A conflict is a dispute about *a value*: which digit, which option, which
    set. A scan-quality issue is a statement about *the coordinate system* - it
    says the mapping from template to paper stopped being trustworthy in some
    part of the page, which makes every value measured there suspect at once.
    The two answer different questions and are resolved differently (a conflict
    is corrected, a bent sheet is re-scanned), so they stay separate types. One
    sheet-scope conflict carries the assessment into the existing review queue;
    see :mod:`omr_scanner.services.conflict_policy`.

Why a three-way status rather than a score:
    A single blended number cannot be acted on. "0.62" tells an operator
    nothing, whereas "the lower-right answer block is no longer aligned" tells
    them to re-scan that sheet. The numbers are all still recorded - they are
    what makes a false positive debuggable - but the *decision* is one of three
    named outcomes.

The bias, stated once:
    Start conservative. A mildly imperfect but correctly readable sheet passing
    is a far better failure than a queue full of warnings nobody can action,
    because the second teaches an operator to ignore the feature entirely. Every
    default in :class:`ScanQualityThresholds` is chosen on that basis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite


class ScanQualityStatus(StrEnum):
    """How far one sheet's geometry can be trusted.

    Ordered by severity, which :meth:`worse_of` relies on.
    """

    PASS = "pass"
    """The template-to-paper mapping holds everywhere that matters. Ordinary
    rotation, scanner skew and correctable perspective all land here: they are
    explained by the transform, which is precisely what the transform is for."""

    REVIEW = "review"
    """Part of the page is no longer reliably registered, but the sheet still
    carries usable recognition. The values are kept and the affected area is
    named, so a human can judge whether the result stands or the sheet needs
    re-scanning. **Not** a synonym for "discard": throwing away a sheet whose
    identity and ninety of whose answers are perfectly legible, because one
    corner curled, loses real data."""

    UNUSABLE = "unusable"
    """The sheet cannot be trusted as a record. Reserved for the cases where
    what is damaged is what identifies the script, or where registration has
    failed across so much of the page that no part of it is dependable."""

    @property
    def needs_attention(self) -> bool:
        """Whether this status should reach a human at all."""
        return self is not ScanQualityStatus.PASS

    @property
    def rank(self) -> int:
        """Severity order, ``0`` best. Only :meth:`worse_of` should need this."""
        return _STATUS_RANK[self]

    @classmethod
    def worse_of(cls, *statuses: ScanQualityStatus) -> ScanQualityStatus:
        """Return the most severe of ``statuses``, or :attr:`PASS` if empty.

        A sheet is as bad as its worst finding: one unusable identity region is
        not averaged away by nine healthy answer blocks.
        """
        worst = cls.PASS
        for status in statuses:
            if status.rank > worst.rank:
                worst = status
        return worst


_STATUS_RANK: dict[ScanQualityStatus, int] = {
    ScanQualityStatus.PASS: 0,
    ScanQualityStatus.REVIEW: 1,
    ScanQualityStatus.UNUSABLE: 2,
}


class ScanQualityIssueCode(StrEnum):
    """What was wrong with the sheet, as a stable machine-readable code.

    These are a stable API in the same sense as
    :class:`~omr_scanner.services.recognition_models.StatusCode`: a report, a
    filter or a benchmark branches on the code, never on the English sentence,
    so the sentence can be rewritten or translated freely.
    """

    PAGE_GEOMETRY_DISTORTION = "PAGE_GEOMETRY_DISTORTION"
    """The page is not flat. Local features are displaced from where the fitted
    transform puts them by an amount **no global projective transform can
    explain** - the signature of a curled, folded or lifted sheet, as opposed to
    a tilted one. This is the finding the whole module exists for."""

    PARTIAL_PAGE = "PARTIAL_PAGE"
    """Part of the template's area is not present on the usable sheet: the page
    was cropped, torn, or folded far enough that some of it never reached the
    platen."""

    MARKER_GEOMETRY_ERROR = "MARKER_GEOMETRY_ERROR"
    """The registration markers themselves are inconsistent - one sits where the
    other three say it should not. Distinct from "a marker was not found", which
    the imaging layer already raises as a registration failure."""

    REGION_REGISTRATION_ERROR = "REGION_REGISTRATION_ERROR"
    """One named template region is locally misaligned, while the page as a
    whole is not. Narrower than :attr:`PAGE_GEOMETRY_DISTORTION` and more
    actionable: it names the region rather than an area of paper."""

    GEOMETRY_NOT_VERIFIED = "GEOMETRY_NOT_VERIFIED"
    """The check ran and could not reach a conclusion about this sheet.

    Too little of the template's printing could be located to measure the page
    against it - not because the printing moved, but because it could not be
    found at all. **This is not a pass.** "We looked and the geometry is sound"
    and "we looked and could not tell" are different statements, and collapsing
    the second into the first is how a folded sheet slips through: the fold
    destroys the very printing the check needs, the probes fail, nothing is
    reported as displaced, and an unexamined sheet is recorded as an examined
    one. A sheet carrying this code is sent for review.

    Distinct from a template that simply offers too little printing to probe.
    That is a property of the *document*, says nothing about any particular
    sheet, and would flag an entire batch for a decision nobody made - so it is
    recorded as not evaluated without raising this."""

    CRITICAL_REGION_UNREADABLE = "CRITICAL_REGION_UNREADABLE"
    """A region the record cannot do without - the candidate identifier or the
    set code - is missing or unregistered. This is the one issue that makes a
    sheet :attr:`ScanQualityStatus.UNUSABLE` on its own, because a script whose
    owner cannot be established is not a result, whatever else was read."""


class PageArea(StrEnum):
    """A coarse location on the page, for a sentence a human reads.

    Nine cells rather than four quadrants: "lower-right" and "bottom edge" are
    different physical accidents, and a curl almost always starts at a corner.
    Deliberately *not* a coordinate - the precise numbers are in
    :class:`ScanQualityIssue.metrics`, and an operator asked to re-scan a sheet
    needs "the lower-right corner", not a residual in pixels.
    """

    TOP_LEFT = "top-left"
    TOP = "top"
    TOP_RIGHT = "top-right"
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    BOTTOM_LEFT = "bottom-left"
    BOTTOM = "bottom"
    BOTTOM_RIGHT = "bottom-right"

    @property
    def label(self) -> str:
        """A human-readable spelling, for a review sentence."""
        return _AREA_LABELS[self]

    @classmethod
    def containing(cls, x: float, y: float) -> PageArea:
        """Return the area holding the normalised point ``(x, y)``.

        Args:
            x: Horizontal position on the page, ``0`` left to ``1`` right.
            y: Vertical position, ``0`` top to ``1`` bottom.

        Values outside ``[0, 1]`` are clamped: a probe just off the edge of the
        page belongs to the edge cell, not to nothing. A non-finite coordinate
        is reported as :attr:`CENTER`, which is the only honest answer to "where
        is this point" when the point is not a point.
        """
        return _AREA_GRID[_cell_index(y)][_cell_index(x)]


def _cell_index(value: float) -> int:
    """Map a normalised coordinate onto one of three page thirds."""
    if not isfinite(value):
        return 1
    return min(2, max(0, int(value * 3.0)))


_AREA_LABELS: dict[PageArea, str] = {
    PageArea.TOP_LEFT: "upper-left",
    PageArea.TOP: "top",
    PageArea.TOP_RIGHT: "upper-right",
    PageArea.LEFT: "left",
    PageArea.CENTER: "centre",
    PageArea.RIGHT: "right",
    PageArea.BOTTOM_LEFT: "lower-left",
    PageArea.BOTTOM: "bottom",
    PageArea.BOTTOM_RIGHT: "lower-right",
}

_AREA_GRID: tuple[tuple[PageArea, ...], ...] = (
    (PageArea.TOP_LEFT, PageArea.TOP, PageArea.TOP_RIGHT),
    (PageArea.LEFT, PageArea.CENTER, PageArea.RIGHT),
    (PageArea.BOTTOM_LEFT, PageArea.BOTTOM, PageArea.BOTTOM_RIGHT),
)


@dataclass(frozen=True, slots=True)
class ScanQualityIssue:
    """One finding about one sheet, with the evidence that produced it.

    Attributes:
        code: What was wrong.
        status: How bad this finding alone makes the sheet. A sheet's overall
            status is the worst of its issues' - see
            :meth:`ScanQualityStatus.worse_of`.
        detail: One short sentence for a human. Written at detection, because
            the numbers that justify it are in scope there and nowhere else.
        areas: Coarse page locations the finding covers, in reading order.
        zone_ids: Template zones the finding affects, by
            :attr:`~omr_scanner.domain.template.Zone.id`. Empty when the finding
            is about paper rather than about a named region.
        zone_labels: Those zones' display labels, captured at detection so a
            stored assessment still reads correctly if a template is later
            renamed.
        question_range: ``"81-100"`` when the affected zones carry a contiguous
            run of questions, else ``""``. The single most useful sentence
            fragment for an invigilator, and cheap to compute here.
        metrics: The numbers behind the finding, already normalised and
            rounded for storage. Named rather than positional so that a metric
            added later does not invalidate a stored assessment.
    """

    code: ScanQualityIssueCode
    status: ScanQualityStatus
    detail: str = ""
    areas: tuple[PageArea, ...] = ()
    zone_ids: tuple[str, ...] = ()
    zone_labels: tuple[str, ...] = ()
    question_range: str = ""
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def area_label(self) -> str:
        """The affected areas as one phrase, or ``""`` when page-wide."""
        if not self.areas:
            return ""
        return ", ".join(area.label for area in self.areas)

    def summary(self) -> str:
        """One line naming the issue, where it is, and what it means."""
        where = self.area_label
        parts = [_ISSUE_LABELS[self.code]]
        if where:
            parts.append(f"({where})")
        if self.question_range:
            parts.append(f"questions {self.question_range}")
        head = " ".join(parts)
        return f"{head}: {self.detail}" if self.detail else head


_ISSUE_LABELS: dict[ScanQualityIssueCode, str] = {
    ScanQualityIssueCode.PAGE_GEOMETRY_DISTORTION: "Page geometry distortion",
    ScanQualityIssueCode.PARTIAL_PAGE: "Partial page",
    ScanQualityIssueCode.MARKER_GEOMETRY_ERROR: "Marker geometry error",
    ScanQualityIssueCode.REGION_REGISTRATION_ERROR: "Region registration error",
    ScanQualityIssueCode.GEOMETRY_NOT_VERIFIED: "Page geometry not verified",
    ScanQualityIssueCode.CRITICAL_REGION_UNREADABLE: "Critical region unreadable",
}


def issue_label(code: ScanQualityIssueCode) -> str:
    """Return the short human-readable name of ``code``."""
    return _ISSUE_LABELS[code]


@dataclass(frozen=True, slots=True)
class ScanQualityAssessment:
    """Every scan-quality finding for one sheet, plus what they came from.

    Attributes:
        status: The sheet's overall verdict - the worst of :attr:`issues`, or
            :attr:`ScanQualityStatus.PASS` when there are none.
        issues: The findings, most severe first.
        probe_count: Interior probe points the geometry check attempted.
        matched_count: How many of them were located confidently. A low ratio
            is itself evidence - a page whose printed features cannot be found
            is not a page whose geometry has been confirmed.
        affected_count: Probes whose displacement reached
            :attr:`ScanQualityThresholds.review_displacement_pitch`, or which
            saturated. **This is what the verdict is actually made on.**
        unmatched_count: Probes that could not be located at all. Distinct from
            :attr:`affected_count` on purpose: printing that cannot be found has
            been covered or destroyed, which is a different accident from
            printing that has moved, and an operator acts on them differently.
        displacement_median_pitch: Median displacement of the located probes
            from where the template puts them, in units of bubble pitch, after
            the engine's own homography has been applied. This is the primary
            signal, because it is the quantity that decides whether a bubble
            sample window still covers the bubble it is supposed to measure.
            Rotation, skew, scale and perspective do **not** inflate it: the
            homography has already removed them by the time it is measured -
            confirmed on this repository's real sheet, where flat, 7-degree
            rotated, 0.85-scaled and 5-per-cent perspective scans all read
            within 0.005 of one another.
        displacement_p95_pitch: The 95th percentile of the same quantity. A curl
            affects a minority of the page, so the tail is where it shows; the
            median stays low precisely because most of the sheet is fine. On the
            real sample this is 0.107 flat and unchanged under every projective
            distortion tried.
        displacement_max_pitch: The largest single displacement.
        nonprojective_p95_pitch: The 95th percentile displacement **remaining
            after a global projective refit**. This is what separates *why* the
            geometry is wrong: a page that is merely mis-registered has a large
            displacement and a small non-projective remainder, because one
            better homography would fix it; a page that is physically bent keeps
            its remainder, because no plane can explain it.
        global_fit_rms_pitch: RMS residual of that refit. Recorded to make the
            claim auditable rather than asserted.
        coverage_ratio: Fraction of the template's zone area that lies inside
            the usable, registered page. ``1.0`` for an intact sheet.
        marker_residual_max_px: Largest marker reprojection residual, in
            canonical pixels. Near zero whenever the marker count equals the
            homography's degrees of freedom, which is why it is reported and
            never relied upon - see the module docstring of
            :mod:`omr_scanner.imaging.page_geometry`.
        missing_markers: Roles the detector did not find, as plain strings.
        evaluated: Whether the geometry check actually ran. ``False`` when the
            sheet never registered, or when the template offers too few probe
            points to measure anything - in which case :attr:`status` is
            :attr:`ScanQualityStatus.PASS` and means "not contradicted", not
            "confirmed good". Recorded explicitly so that the two can never be
            confused by a later reader.
        reason: One short sentence summarising the sheet, for a queue row.
    """

    status: ScanQualityStatus = ScanQualityStatus.PASS
    issues: tuple[ScanQualityIssue, ...] = ()
    probe_count: int = 0
    matched_count: int = 0
    affected_count: int = 0
    unmatched_count: int = 0
    displacement_median_pitch: float = 0.0
    displacement_p95_pitch: float = 0.0
    displacement_max_pitch: float = 0.0
    nonprojective_p95_pitch: float = 0.0
    global_fit_rms_pitch: float = 0.0
    coverage_ratio: float = 1.0
    marker_residual_max_px: float = 0.0
    missing_markers: tuple[str, ...] = ()
    evaluated: bool = False
    reason: str = ""

    @property
    def needs_attention(self) -> bool:
        """Whether this sheet should reach a human for a geometry reason."""
        return self.status.needs_attention

    @property
    def matched_ratio(self) -> float:
        """Fraction of attempted probes that were located, ``0`` when none were.

        Reported alongside :attr:`evaluated` because the two together are what
        distinguish a clean sheet from an unexamined one: ``evaluated`` says
        whether a conclusion was reached, this says how much evidence it rests
        on.
        """
        if self.probe_count <= 0:
            return 0.0
        return self.matched_count / self.probe_count

    @property
    def affected_ratio(self) -> float:
        """Fraction of attempted probes found displaced, ``0`` when none were."""
        if self.probe_count <= 0:
            return 0.0
        return self.affected_count / self.probe_count

    @property
    def is_confirmed_clean(self) -> bool:
        """Whether this sheet was actually checked *and* found sound.

        The predicate any caller should use before treating a sheet's geometry
        as trustworthy. ``status is PASS`` alone is not enough: a sheet the
        check could not evaluate also carries ``PASS`` when nothing about the
        *template* is at fault, and "not contradicted" is not "confirmed".
        """
        return self.evaluated and self.status is ScanQualityStatus.PASS

    @property
    def codes(self) -> tuple[str, ...]:
        """Every issue code that applies, in order, as plain strings."""
        return tuple(issue.code.value for issue in self.issues)

    @property
    def affected_zone_ids(self) -> tuple[str, ...]:
        """Every template zone named by any issue, de-duplicated, in order."""
        seen: dict[str, None] = {}
        for issue in self.issues:
            for zone_id in issue.zone_ids:
                seen.setdefault(zone_id, None)
        return tuple(seen)

    def issue(self, code: ScanQualityIssueCode) -> ScanQualityIssue | None:
        """Return the first issue carrying ``code``, or ``None``."""
        for item in self.issues:
            if item.code is code:
                return item
        return None


@dataclass(frozen=True, slots=True)
class ScanQualityThresholds:
    """Every tunable number the scan-quality verdict uses, in one object.

    Nothing here is a magic constant scattered through the implementation, and
    nothing here is a guess. Every default was chosen after measuring this
    repository's own real scan (``Sample-Project/1.Template/ECE-0000.png``,
    2480x3508, against ``BUET100q.omrt``) both undamaged and under deliberate
    distortion, and the numbers those runs produced are quoted below so that a
    later reader can tell a tuned value from an invented one.

    Scale independence is the point of the units. Displacements are expressed in
    **bubble pitch** - the centre-to-centre distance between adjacent bubbles -
    never in pixels, because pitch is the length at which a geometric error
    starts to matter: at half a pitch a sample window has moved onto the
    neighbouring bubble and is simply measuring the wrong thing. The same sheet
    at 200 and at 600 dpi therefore earns the same verdict, which raw pixels
    could never deliver.

    Attributes:
        review_displacement_pitch: Displacement at or above which one probe
            counts as affected. Measured basis: on the real sheet the *worst*
            probe reads 0.143 flat, and reads within 0.005 of that under 2, 7
            and -8 degrees of rotation, 0.85 scale, 2 and 5 per cent
            perspective, added noise, blur and a 30 per cent exposure drop. The
            default sits well above all of those and well below the half-pitch
            point where the reading is certainly of the wrong bubble.
        min_affected_sites: How many probes must be affected before a
            distortion is reported. One outlying probe is a mis-match; a
            physical curl always moves a contiguous group. Measured basis: every
            undistorted and projectively-distorted variant produced **zero**
            affected probes, while a lower-right curl produced 2 probes at 0.52
            pitch of local displacement, 6 at 0.75 and 9 at 1.08. Three is
            therefore a real margin rather than a hair's breadth, and it
            deliberately lets the mildest case pass - see the module docstring
            on which way to be wrong.
        min_nonprojective_pitch: How much of the page's displacement must
            survive a global projective refit before a distortion is reported.
            **The false-positive guard that makes the feature safe**, and the
            direct expression of the rule that an ordinary transformation
            explaining the geometry is not a fault. A homography fitted to four
            corner markers can be slightly off - a marker detected a pixel
            late, a sheet very slightly out of plane at the clamps - and that
            error displaces printing across the whole sheet without the paper
            having been bent at all. Such a field is *entirely* absorbed by the
            refit: measured on a synthetic lattice, translation, rotation, scale
            and perspective producing up to 0.37 pitch of raw displacement all
            leave a non-projective remainder of 0.000 to 0.001. A real bend does
            not vanish that way - the same sheet reads 0.089 flat and 0.149 to
            0.56 curled - so the remainder is what separates "mis-registered"
            from "not flat".
        saturated_sites_for_review: Saturated probes that alone justify review,
            even below :attr:`min_affected_sites`. A saturated probe is one
            whose displacement ran past the search window, so its magnitude is
            unknown but is certainly large; two of them agreeing is stronger
            evidence than three marginal ones.
        unusable_affected_ratio: Fraction of all probes that must be affected or
            unmatched before registration is treated as having failed across the
            sheet rather than degraded in one place.
        region_affected_sites: Affected probes within one zone before that zone
            is named individually by
            :attr:`ScanQualityIssueCode.REGION_REGISTRATION_ERROR`.
        min_matched_ratio: Fraction of probes that must be located before the
            geometry verdict is trusted at all. Below this the assessment
            records that it was not evaluated, rather than recording a pass.
        min_probes: Fewest probes that can support a verdict. Below this the
            sheet is not judged: a handful of points cannot separate a local
            deformation from a global one.
        critical_coverage_ratio: Fraction of a critical region - the candidate
            identifier or the set code - that must lie inside the usable page
            before that region is trusted. Below it the sheet is unusable,
            because a script whose owner cannot be established is not a result.
        answer_coverage_ratio: The same for an answer region, where the
            consequence is losing some questions rather than the whole script,
            so the sheet is sent for review instead of being rejected.
        marker_residual_pitch: Marker reprojection residual above which the
            marker set is called inconsistent. Only ever meaningful when a
            template supplies more markers than the transform needs; with the
            four that the current format mandates the homography is exactly
            determined and this residual is zero by construction, which is why
            no verdict may rest on it alone.
    """

    review_displacement_pitch: float = 0.34
    min_affected_sites: int = 3
    min_nonprojective_pitch: float = 0.12
    saturated_sites_for_review: int = 2
    unusable_affected_ratio: float = 0.45
    region_affected_sites: int = 2
    min_matched_ratio: float = 0.55
    min_probes: int = 8
    critical_coverage_ratio: float = 0.75
    answer_coverage_ratio: float = 0.90
    marker_residual_pitch: float = 1.20

    def __post_init__(self) -> None:
        """Reject a set of thresholds that could never behave sensibly."""
        if not 0.0 < self.review_displacement_pitch < 0.5:
            raise ValueError(
                "review_displacement_pitch must lie in (0, 0.5): at half a "
                "pitch the sample window is already on the neighbouring bubble, "
                "and the probe cannot measure past that unambiguously"
            )
        if self.min_affected_sites < 1:
            raise ValueError("min_affected_sites must be at least 1")
        if self.min_nonprojective_pitch <= 0.0:
            raise ValueError("min_nonprojective_pitch must be positive")
        if self.saturated_sites_for_review < 1:
            raise ValueError("saturated_sites_for_review must be at least 1")
        if not 0.0 < self.unusable_affected_ratio <= 1.0:
            raise ValueError("unusable_affected_ratio must lie in (0, 1]")
        if self.region_affected_sites < 1:
            raise ValueError("region_affected_sites must be at least 1")
        if not 0.0 < self.min_matched_ratio <= 1.0:
            raise ValueError("min_matched_ratio must lie in (0, 1]")
        if self.min_probes < 4:
            raise ValueError(
                "min_probes must be at least 4: fewer points cannot separate a "
                "local deformation from a global one"
            )
        for name, value in (
            ("critical_coverage_ratio", self.critical_coverage_ratio),
            ("answer_coverage_ratio", self.answer_coverage_ratio),
        ):
            if not 0.0 < value <= 1.0:
                raise ValueError(f"{name} must lie in (0, 1]")


DEFAULT_THRESHOLDS = ScanQualityThresholds()
"""The conservative defaults every caller gets unless it says otherwise."""


__all__ = [
    "DEFAULT_THRESHOLDS",
    "PageArea",
    "ScanQualityAssessment",
    "ScanQualityIssue",
    "ScanQualityIssueCode",
    "ScanQualityStatus",
    "ScanQualityThresholds",
    "issue_label",
]
