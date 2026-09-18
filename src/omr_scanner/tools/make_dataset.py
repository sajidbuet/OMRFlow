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
        --count 1000 --profile difficult

    (each example is one command; the options are wrapped for legibility)

Output::

    <out>/images/SYN_000001.png ...
    <out>/ground_truth/SYN_000001.json ...
    <out>/manifest.json

Exit codes:
    ``0`` written, ``1`` the template could not be used, ``2`` bad arguments.

Do not commit a large generated dataset. Commit the generator, the seed and the
manifest: a dataset that can be regenerated exactly is not worth the repository
space, and one that cannot be regenerated is not worth trusting.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omr_scanner.errors import OMRScannerError
from omr_scanner.evaluation.synthetic_dataset import DatasetProfile, generate_dataset
from omr_scanner.services.template_service import load_template

EXIT_OK = 0
EXIT_FAILED = 1

DEFAULT_COUNT = 24
DEFAULT_SEED = 20260918


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
        help="How hard the sheets should be.",
    )
    parser.add_argument("--name", default="synthetic", help="Dataset name for the manifest.")
    parser.add_argument("--version", default="1", help="Dataset revision for the manifest.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the tool and return a shell exit code."""
    arguments = build_parser().parse_args(argv)

    try:
        template = load_template(arguments.template)
    except OMRScannerError as exc:
        print(f"Could not load the template: {exc.user_message}", file=sys.stderr)
        return EXIT_FAILED

    try:
        manifest = generate_dataset(
            arguments.output,
            template,
            count=arguments.count,
            seed=arguments.seed,
            profile=DatasetProfile(arguments.profile),
            name=arguments.name,
            version=arguments.version,
            template_path=str(arguments.template),
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
