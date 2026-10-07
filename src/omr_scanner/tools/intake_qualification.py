r"""Command line for the revised phase 9 intake qualification campaign.

Examples (PowerShell, from the repository root)::

    .venv\Scripts\python.exe -m omr_scanner.tools.intake_qualification --self-test `
        --output Scratch\Qualification\phase9

    .venv\Scripts\python.exe -m omr_scanner.tools.intake_qualification --release-scale `
        --output Scratch\Qualification\phase9

Exactly one of ``--self-test`` / ``--release-scale`` / ``--config`` is required,
so a release qualification is never started (or claimed) by default settings.
The mode and the scale are printed before anything runs.

Exit codes:
    ``0`` the requested campaign completed with a passing verdict - ``QUALIFIED``
    for ``--release-scale``, ``ALL RUNS PASSED — NOT THE RELEASE QUALIFICATION``
    for a self-test or custom campaign; ``1`` ``FAILED``; ``2`` bad arguments;
    ``3`` ``--release-scale`` was requested and the campaign passed but did not
    measure up to the release scale (never success); ``130`` interrupted.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

from omr_scanner.evaluation.intake_qualification.config import (
    VERDICT_QUALIFIED,
    VERDICT_SMALL,
    CampaignConfig,
    Mode,
    release_config,
    self_test_config,
)


def build_parser() -> argparse.ArgumentParser:
    """The command-line parser."""
    parser = argparse.ArgumentParser(
        prog="python -m omr_scanner.tools.intake_qualification",
        description="Revised phase 9 automated intake qualification (synthetic, source build).",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-test", action="store_true",
                      help="the small harness self-test (minutes; never QUALIFIED)")
    mode.add_argument("--release-scale", action="store_true",
                      help="the >= 3-source, >= 10,000-arrival release campaign")
    mode.add_argument("--config", type=Path, help="a custom campaign configuration (JSON)")
    parser.add_argument("--output", type=Path, required=True,
                        help="parent folder; each campaign gets its own new sub-folder")
    parser.add_argument("--template", type=Path,
                        default=Path("examples") / "templates" / "synthetic_answer_sheet.omrt")
    parser.add_argument("--seed", type=int, help="cohort seed (default: the mode's)")
    parser.add_argument("--timing-seed", type=int, help="arrival-timing seed")
    parser.add_argument("--workers", type=int, help="recognition worker processes")
    parser.add_argument("--no-control", action="store_true", help="skip the uninterrupted control")
    parser.add_argument("--no-finite", action="store_true", help="skip the finite-mode control")
    parser.add_argument("--coordinator-exe", type=Path,
                        help="run every coordinator incarnation inside this packaged OMRFlow.exe "
                             "(the installed-build run) instead of from source")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the requested campaign; return the exit code."""
    from omr_scanner.evaluation.intake_qualification.campaign import run_campaign

    arguments = build_parser().parse_args(argv)
    if arguments.self_test:
        config = self_test_config()
    elif arguments.release_scale:
        config = release_config()
    else:
        try:
            config = CampaignConfig.from_json(json.loads(arguments.config.read_text("utf-8")))
        except (OSError, ValueError, TypeError) as exc:
            print(f"Could not read the configuration: {exc}", file=sys.stderr)
            return 2
        config = replace(config, mode=Mode.CUSTOM) if config.mode is Mode.RELEASE else config
    overrides = {}
    if arguments.seed is not None:
        overrides["seed"] = arguments.seed
    if arguments.timing_seed is not None:
        overrides["timing_seed"] = arguments.timing_seed
    if arguments.workers is not None:
        overrides["workers"] = max(1, arguments.workers)
    if arguments.no_control:
        overrides["run_control"] = False
    if arguments.no_finite:
        overrides["run_finite"] = False
    config = replace(config, **overrides)
    if not arguments.template.is_file():
        print(f"Template not found: {arguments.template}", file=sys.stderr)
        return 2
    if arguments.coordinator_exe is not None and not arguments.coordinator_exe.is_file():
        print(f"Packaged executable not found: {arguments.coordinator_exe}", file=sys.stderr)
        return 2
    print(f"Intake qualification - mode {config.mode.value}: {config.sources} sources, "
          f"{config.candidates_per_set:,} candidates per set x {len(config.sets)} sets, "
          f"{config.duration_seconds:.0f} s arrival timeline, {config.workers} workers, "
          f"seed {config.seed} / timing seed {config.timing_seed}.")
    if config.mode is not Mode.RELEASE:
        print(f"This is not the release qualification; its best verdict is '{VERDICT_SMALL}'.")
    if arguments.coordinator_exe is not None:
        print(f"Coordinator: the packaged executable {arguments.coordinator_exe}.")
    began = time.monotonic()

    def say(message: str) -> None:
        print(f"[{time.monotonic() - began:8.1f}s] {message}", flush=True)

    try:
        packaged = (
            {} if arguments.coordinator_exe is None
            else {"coordinator_executable": arguments.coordinator_exe}
        )
        root, payload = run_campaign(config, arguments.output, template_path=arguments.template,
                                     progress=say, **packaged)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    verdict = payload.get("verdict")
    print(f"\nVerdict: {verdict}\nReport: {root / 'report.md'}")
    for item in payload.get("assertions", []):
        print(f"  {item['status'].upper():14s} {item['name']}")
    if payload.get("error", {}).get("stage") == "interrupted by the operator":
        return 130
    if verdict == VERDICT_QUALIFIED:
        return 0
    if verdict == VERDICT_SMALL:
        return 3 if config.mode is Mode.RELEASE else 0
    return 1


if __name__ == "__main__":  # pragma: no cover - command line
    sys.exit(main())
