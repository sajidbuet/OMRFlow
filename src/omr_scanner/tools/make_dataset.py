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

    (each example is one command; the options are wrapped for legibility)

Output::

    <out>/images/SYN_000001.png ...
    <out>/ground_truth/SYN_000001.json ...
    <out>/manifest.json
    <out>/manifest.csv
    <out>/dataset_summary.json

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
    plan_population,
)
from omr_scanner.evaluation.synthetic_dataset import (
    DEFAULT_DPI,
    DEFAULT_JPEG_QUALITY,
    CaseFamily,
    DatasetProfile,
    GenerationProgress,
    ImageFormat,
    describe_template,
    generate_dataset,
    page_render_size,
    validate_template,
)
from omr_scanner.services.template_service import load_template

EXIT_OK = 0
EXIT_FAILED = 1

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
        help="Rendering resolution, derived from the template's physical page size.",
    )
    parser.add_argument(
        "--sets",
        default="",
        help=(
            "Comma-separated question-paper sets, e.g. 10,11,12. Given, the run "
            "also writes one attendance workbook per set and the reconciliation "
            "ground truth, and --count becomes the size of the candidate roster "
            "rather than the number of images."
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

    set_codes = tuple(
        code.strip() for code in arguments.sets.split(",") if code.strip()
    )
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
        print(
            f"Rendering {sheets} sheet(s) at {render.width}x{render.height} px "
            f"({render.dpi} dpi, from the template's {render.derived_from} page size) "
            f"as {image_format.value.upper()}"
        )
        if population is not None:
            print(
                f"  {len(population.candidates)} candidate(s) across "
                f"{len(population.by_set())} set(s); attendance workbooks and "
                "reconciliation ground truth will be written alongside the images"
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
            on_progress=None if arguments.quiet else report,
        )
    except (ValueError, OSError) as exc:
        print(f"Could not generate the dataset: {exc}", file=sys.stderr)
        return EXIT_FAILED

    print(
        f"{len(manifest.entries)} sheet(s) written to {arguments.output} "
        f"(profile '{arguments.profile}', seed {arguments.seed}, template '{template.name}')"
    )
    print("Synthetic data measures regressions, not real-world accuracy.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

