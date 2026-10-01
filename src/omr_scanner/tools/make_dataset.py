"""Generate a reproducible synthetic OMR dataset with ground truth.

Purpose:
    Produce test data for Phase 3 on demand, from a real template, with the
    correct answers written beside every image - so that a regression suite has
    something to run against today, and a calibration experiment has something
    to sweep over tomorrow.

Usage::

    python -m omr_scanner.tools.make_dataset out/dataset
        --template examples/templates/ece_0000_sample.omrt
        --count 24 --profile mixed --seed 20260918

    python -m omr_scanner.tools.make_dataset out/big
        --template examples/templates/ece_0000_sample.omrt
        --count 1000 --profile degradation --format jpg

    python -m omr_scanner.tools.make_dataset out/answers
        --template sheet.omrt --profile custom
        --families answers mark_styles intensity

    python -m omr_scanner.tools.make_dataset out/real
        --template sheet.omrt --render-mode reference_scan
        --reference-scan scans/blank_form.png --color-mode color

    python -m omr_scanner.tools.make_dataset out/folded
        --template sheet.omrt --folds --fold-frequency 0.2
        --fold-severity random --fold-corners top_left top_right

    (each example is one command; the options are wrapped for legibility)

Rendering modes:
    ``--render-mode template`` (the default) draws the whole page. ``--render-mode
    reference_scan`` draws only the candidate's marks and lays them on a scan of
    a real blank form, which must be given with ``--reference-scan``. In that
    mode ``--dpi`` has nothing to act on and is not applied: the output is the
    reference scan at its own resolution.

Output::

    <out>/images/SYN_000001.png ...
    <out>/ground_truth/SYN_000001.json ...
    <out>/solution/Set_<code>_Solution.png ...   one clean solution sheet per set
    <out>/solution/Set_<code>_Answer_Key.txt ... its key, as the Answer Key stage reads it
    <out>/manifest.json
    <out>/manifest.csv
    <out>/dataset_summary.json

Answer keys and candidate performance:
    One key is drawn per set and every candidate answers against the key of the
    paper they sat: a target score from a truncated normal distribution
    (``--mean-correct 0.65 --sd-correct 0.15 --min-correct 0 --max-correct 1``
    by default), exactly that many answers right, the rest valid but wrong.
    ``--performance-distribution random`` restores answers chosen independently
    of any key; ``--no-solutions`` skips writing ``solution/``.

Exit codes:
    ``0`` written, ``1`` the template could not be used, ``2`` bad arguments.

Identifiers are fictional by construction - derived from the sheet index, never
from any real numbering - so a generated dataset can be committed or shared
without carrying anybody's data.

Do not commit a large generated dataset. Commit the generator, the seed and the
manifest: a dataset that can be regenerated exactly is not worth the repository
space, and one that cannot be regenerated is not worth trusting.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from omr_scanner.errors import OMRScannerError
from omr_scanner.evaluation.attendance_dataset import (
    ConflictProfile,
    ConflictRates,
    parse_set_spec,
    plan_population,
)
from omr_scanner.evaluation.fold_plans import FoldCorner, FoldPolicy, FoldSeverity
from omr_scanner.evaluation.performance import (
    DEFAULT_PERFORMANCE,
    PerformanceDistribution,
    PerformancePolicy,
)
from omr_scanner.evaluation.reference_scan import ReferenceScanError
from omr_scanner.evaluation.synthetic_dataset import (
    DEFAULT_DPI,
    DEFAULT_JPEG_QUALITY,
    CaseFamily,
    ColorMode,
    DatasetProfile,
    GenerationProgress,
    ImageFormat,
    RenderMode,
    describe_template,
    generate_dataset,
    page_render_size,
    validate_template,
)
from omr_scanner.services.template_service import load_template

EXIT_OK = 0
EXIT_FAILED = 1

RANDOM_SEVERITY = "random"
"""What ``--fold-severity`` accepts for a mixture of all four bands.

The same spelling the dialog uses, and for the same reason: a fold that has
been drawn is always one of the four severities, so "random" is a request
rather than a fifth kind of fold and never reaches a manifest."""

DEFAULT_COUNT = 24
DEFAULT_SEED = 20260918

PROGRESS_EVERY = 25
"""Sheets between progress lines. Enough that a thousand-sheet run says
something, few enough that it does not scroll a terminal off the screen."""


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the tool."""
    parser = argparse.ArgumentParser(
        prog="python -m omr_scanner.tools.make_dataset",
        description="Render a labelled synthetic OMR dataset from a template.",
    )
    parser.add_argument("output", type=Path, help="Folder to write the dataset into.")
    parser.add_argument(
        "--template",
        type=Path,
        required=True,
        help="The .omrt template whose geometry the sheets should use.",
    )
    parser.add_argument(
        "--count", type=int, default=DEFAULT_COUNT, help="How many sheets to render."
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="Master seed. The same seed, count, profile and template give the same dataset.",
    )
    parser.add_argument(
        "--profile",
        choices=[profile.value for profile in DatasetProfile],
        default=DatasetProfile.MIXED.value,
        help="Which families of test case the dataset draws on.",
    )
    parser.add_argument(
        "--families",
        nargs="+",
        choices=[family.value for family in CaseFamily],
        help="Families to use with '--profile custom'.",
    )
    parser.add_argument(
        "--format",
        default=ImageFormat.PNG.value,
        help="Image format: png (lossless, the default) or jpg.",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=DEFAULT_JPEG_QUALITY,
        help="JPEG quality. High by default; compression damage is its own test case.",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=DEFAULT_DPI,
        help=(
            "Rendering resolution, derived from the template's physical page size. "
            "Not applied with '--render-mode reference_scan', whose pixels come "
            "from a real scan."
        ),
    )
    parser.add_argument(
        "--render-mode",
        choices=[mode.value for mode in RenderMode],
        default=RenderMode.TEMPLATE.value,
        help=(
            "Where each page comes from: 'template' draws the whole sheet, "
            "'reference_scan' draws only the marks and lays them on a real "
            "scanned blank form."
        ),
    )
    parser.add_argument(
        "--reference-scan",
        type=Path,
        help=(
            "The scanned blank form to lay marks on. Required by "
            "'--render-mode reference_scan'."
        ),
    )
    parser.add_argument(
        "--color-mode",
        choices=[mode.value for mode in ColorMode],
        default=ColorMode.GRAYSCALE.value,
        help=(
            "Channel layout and tonal range of the written images: grayscale "
            "(the default), color, or bw for a one-bit scan."
        ),
    )
    parser.add_argument(
        "--folds",
        action="store_true",
        help=(
            "Physically fold a corner of some sheets, after everything has "
            "been printed and marked on them. Off by default."
        ),
    )
    parser.add_argument(
        "--fold-frequency",
        type=float,
        default=FoldPolicy().frequency,
        help=(
            "Fraction of sheets to fold, e.g. 0.1 for one in ten. The "
            "deliberate coverage cases are filled first, so a small dataset "
            "may exceed this rather than omit a case."
        ),
    )
    parser.add_argument(
        "--fold-corners",
        nargs="+",
        choices=[corner.value for corner in FoldCorner],
        help="Which corners may be folded. All four when not given.",
    )
    parser.add_argument(
        "--fold-severity",
        choices=[*(severity.value for severity in FoldSeverity), RANDOM_SEVERITY],
        default=RANDOM_SEVERITY,
        help="How deep the folds are; 'random' mixes all four bands.",
    )
    parser.add_argument(
        "--fold-max-per-sheet",
        type=int,
        default=FoldPolicy().max_per_sheet,
        help=(
            "Folded corners on one affected sheet, 1 to 4. Three or more are "
            "capped at moderate so they cannot consume the page."
        ),
    )
    parser.add_argument(
        "--no-fold-coverage",
        action="store_true",
        help=(
            "Do not spend part of the quota on deliberately chosen marker "
            "interactions; fold entirely at random instead."
        ),
    )
    parser.add_argument(
        "--sets",
        default="",
        help=(
            "Comma-separated question-paper sets, e.g. 10,11,12. Given, the run "
            "also writes one attendance workbook per set and the reconciliation "
            "ground truth, and --count becomes the size of the candidate roster "
            "rather than the number of images. Write 10=A for a logical Set 10 "
            "whose sheets are printed (and marked) as A."
        ),
    )
    parser.add_argument(
        "--attendance-conflict-profile",
        choices=[profile.value for profile in ConflictProfile],
        default=ConflictProfile.NORMAL.value,
        help="How much the generated attendance workbooks disagree with reality.",
    )
    parser.add_argument(
        "--true-absentee-rate",
        type=float,
        default=ConflictRates().true_absentee,
        help="Genuine non-attendance, as a fraction. Distinct from clerical error.",
    )
    parser.add_argument(
        "--no-reconciliation-edge-cases",
        action="store_true",
        help="Do not force one of every reconciliation conflict into the roster.",
    )
    parser.add_argument(
        "--no-solutions",
        action="store_true",
        help=(
            "Do not write solution/ (one solution sheet and answer-key text file "
            "per set). The keys are still drawn: candidates answer against them."
        ),
    )
    parser.add_argument(
        "--performance-distribution",
        choices=[distribution.value for distribution in PerformanceDistribution],
        default=DEFAULT_PERFORMANCE.distribution.value,
        help=(
            "How many questions each candidate gets right: 'normal' samples a "
            "truncated normal score per candidate (the default); 'random' picks "
            "every answer independently of the key, as before keys existed."
        ),
    )
    parser.add_argument(
        "--mean-correct",
        type=float,
        default=DEFAULT_PERFORMANCE.mean,
        help="Mean share of questions answered correctly, as a fraction (0.65).",
    )
    parser.add_argument(
        "--sd-correct",
        type=float,
        default=DEFAULT_PERFORMANCE.stddev,
        help="Standard deviation of that share, as a fraction (0.15).",
    )
    parser.add_argument(
        "--min-correct",
        type=float,
        default=DEFAULT_PERFORMANCE.minimum,
        help="Lowest possible share, as a fraction; the distribution is truncated here.",
    )
    parser.add_argument(
        "--max-correct",
        type=float,
        default=DEFAULT_PERFORMANCE.maximum,
        help="Highest possible share, as a fraction; the distribution is truncated here.",
    )
    parser.add_argument("--name", default="synthetic", help="Dataset name for the manifest.")
    parser.add_argument("--version", default="1", help="Dataset revision for the manifest.")
    parser.add_argument(
        "--describe",
        action="store_true",
        help="Print what the template offers a generator and exit without writing anything.",
    )
    parser.add_argument("--quiet", action="store_true", help="Do not print progress.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the tool and return a shell exit code."""
    arguments = build_parser().parse_args(argv)

    try:
        template = load_template(arguments.template)
    except OMRScannerError as exc:
        print(f"Could not load the template: {exc.user_message}", file=sys.stderr)
        return EXIT_FAILED

    problems = validate_template(template)
    if problems:
        print("This template cannot be generated from:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_FAILED

    if arguments.describe:
        for key, value in describe_template(template).items():
            print(f"{key:<22} {value}")
        return EXIT_OK

    render_mode = RenderMode(arguments.render_mode)
    color_mode = ColorMode(arguments.color_mode)
    if render_mode is RenderMode.REFERENCE_SCAN and arguments.reference_scan is None:
        print(
            "'--render-mode reference_scan' needs '--reference-scan <blank scan>'.",
            file=sys.stderr,
        )
        return EXIT_FAILED

    try:
        fold_policy = FoldPolicy(
            enabled=arguments.folds,
            corners=(
                tuple(FoldCorner(name) for name in arguments.fold_corners)
                if arguments.fold_corners
                else tuple(FoldCorner)
            ),
            frequency=arguments.fold_frequency,
            severity=(
                None
                if arguments.fold_severity == RANDOM_SEVERITY
                else FoldSeverity(arguments.fold_severity)
            ),
            max_per_sheet=arguments.fold_max_per_sheet,
            ensure_coverage=not arguments.no_fold_coverage,
        )
    except ValueError as exc:
        print(f"Those fold settings cannot be used: {exc}", file=sys.stderr)
        return EXIT_FAILED

    performance = PerformancePolicy(
        distribution=PerformanceDistribution(arguments.performance_distribution),
        mean=arguments.mean_correct,
        stddev=arguments.sd_correct,
        minimum=arguments.min_correct,
        maximum=arguments.max_correct,
    )
    try:
        performance.validate()
    except ValueError as exc:
        print(f"Those performance settings cannot be used: {exc}", file=sys.stderr)
        return EXIT_FAILED

    try:
        set_codes, physical_marks = parse_set_spec(arguments.sets)
    except ValueError as exc:
        print(f"Those sets cannot be used: {exc}", file=sys.stderr)
        return EXIT_FAILED
    try:
        image_format = ImageFormat.parse(arguments.format)
        render = page_render_size(template, arguments.dpi)
        population = (
            plan_population(
                count=arguments.count,
                set_codes=set_codes,
                seed=arguments.seed,
                rates=replace(
                    ConflictProfile(arguments.attendance_conflict_profile).rates(),
                    true_absentee=arguments.true_absentee_rate,
                ),
                include_edge_cases=not arguments.no_reconciliation_edge_cases,
            )
            if set_codes
            else None
        )
    except ValueError as exc:
        print(f"Could not generate the dataset: {exc}", file=sys.stderr)
        return EXIT_FAILED

    if not arguments.quiet:
        sheets = (
            len(population.sheets_to_render())
            if population is not None
            else arguments.count
        )
        if render_mode is RenderMode.REFERENCE_SCAN:
            print(
                f"Rendering {sheets} sheet(s) as marks laid on "
                f"'{arguments.reference_scan.name}' at its own resolution "
                f"(--dpi is not applied in this mode) "
                f"as {image_format.value.upper()}, {color_mode.value}"
            )
        else:
            print(
                f"Rendering {sheets} sheet(s) at {render.width}x{render.height} px "
                f"({render.dpi} dpi, from the template's {render.derived_from} page "
                f"size) as {image_format.value.upper()}, {color_mode.value}"
            )
        if population is not None:
            print(
                f"  {len(population.candidates)} candidate(s) across "
                f"{len(population.by_set())} set(s); attendance workbooks and "
                "reconciliation ground truth will be written alongside the images"
            )
        if fold_policy.active:
            print(
                f"  folding ~{fold_policy.frequency:.0%} of sheets at "
                f"{len(fold_policy.corners)} corner(s), "
                f"{arguments.fold_severity} severity, up to "
                f"{fold_policy.max_per_sheet} per sheet"
            )

    def report(progress: GenerationProgress) -> None:
        """Print a progress line every :data:`PROGRESS_EVERY` sheets."""
        if progress.completed % PROGRESS_EVERY == 0 or progress.completed == progress.total:
            print(f"  {progress.completed}/{progress.total}")

    try:
        manifest = generate_dataset(
            arguments.output,
            template,
            count=arguments.count,
            population=population,
            seed=arguments.seed,
            profile=DatasetProfile(arguments.profile),
            custom_families=(
                [CaseFamily(name) for name in arguments.families]
                if arguments.families
                else None
            ),
            dpi=arguments.dpi,
            image_format=image_format,
            jpeg_quality=arguments.jpeg_quality,
            name=arguments.name,
            version=arguments.version,
            template_path=str(arguments.template),
            render_mode=render_mode,
            reference_scan=arguments.reference_scan,
            color_mode=color_mode,
            fold_policy=fold_policy,
            generate_solutions=not arguments.no_solutions,
            performance=performance,
            on_progress=None if arguments.quiet else report,
            physical_marks=physical_marks or None,
        )
    except ReferenceScanError as exc:
        # Its own branch because the operator can act on it: the file they
        # chose is the problem, and the message says which of the two inputs
        # to change.
        print(f"Could not use the reference scan: {exc.user_message}", file=sys.stderr)
        return EXIT_FAILED
    except (ValueError, OSError) as exc:
        print(f"Could not generate the dataset: {exc}", file=sys.stderr)
        return EXIT_FAILED

    print(
        f"{len(manifest.entries)} sheet(s) written to {arguments.output} "
        f"(profile '{arguments.profile}', seed {arguments.seed}, template '{template.name}')"
    )
    solutions = manifest.generator.get("solutions")
    if solutions:
        print(
            f"{len(solutions['sets'])} solution sheet(s) and answer-key file(s) "
            f"written to {arguments.output / solutions['directory']}"
        )
    print(manifest.notes)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

