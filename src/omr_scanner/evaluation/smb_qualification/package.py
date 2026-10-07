r"""Preparing an SMB qualification: the plan, the images and one writer package per scanner PC.

The workload is the Phase 9 intake campaign's (``cohort.plan_campaign``) -
the same synthetic sheets with exact ground truth, the same write patterns
(stepped, long pause, header first, held open, ``.part`` + rename), the same
names written at both scanners, the same byte copies - planned for two (or
more) sources and enough candidates that every source receives at least
``files_per_source`` files. No second synthetic-data system.

``prepare`` lays out, under one new campaign folder::

    smb.json                       what was prepared: sources, logs, plan digest, scale
    manifest.json, pool/           the Phase 9 plan and rendered images (OMRFlow machine)
    packages/writer_A/             copy this folder to scanner PC A
        schedule.json              the arrivals for source A (offsets, names, patterns, hashes)
        pool/*.png                 only the images source A writes
        Invoke-OmrflowScannerWriter.ps1
        RUN-ON-SCANNER-PC.txt      the exact command for that machine
    OPERATOR_STEPS.md              the whole procedure, with this campaign's paths filled in

``load`` reopens a prepared campaign for the run (re-planning from the stored
configuration - the plan is deterministic - and refusing a different digest).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from omr_scanner.evaluation.intake_qualification.config import (
    CampaignConfig,
    Mode,
    StabilitySettings,
)

SMB_FILE = "smb.json"
WRITER_SCRIPT_NAME = "Invoke-OmrflowScannerWriter.ps1"
MIN_FILES_PER_SOURCE = 1_000
"""``ACCEPTANCE_CRITERIA.md`` §6: at least 1,000 files per source."""
MIN_SOURCES = 2


def repository_root() -> Path:
    """The repository this module runs from (the writer script lives in ``scripts/smb``)."""
    return Path(__file__).resolve().parents[4]


def writer_script() -> Path:
    """The PowerShell scanner writer copied into every package."""
    return repository_root() / "scripts" / "smb" / WRITER_SCRIPT_NAME


def network_stability() -> StabilitySettings:
    """The production starting policy for a UNC share (``domain.intake.NETWORK_POLICY``)."""
    from omr_scanner.domain.intake import NETWORK_POLICY

    return StabilitySettings(
        min_observations=NETWORK_POLICY.min_observations,
        quiet_seconds=NETWORK_POLICY.quiet_seconds,
        max_decode_attempts=NETWORK_POLICY.max_decode_attempts,
        retry_backoff_seconds=NETWORK_POLICY.retry_backoff_seconds,
        poll_interval_seconds=NETWORK_POLICY.poll_interval_seconds,
    )


@dataclass(frozen=True, slots=True)
class SourceSpec:
    r"""One scanner source as the OMRFlow machine sees it.

    Attributes:
        label: ``A``, ``B``, ... (the project source is named ``Scanner <label>``).
        root: The watched folder - for genuine SMB, ``\\scanner-pc\share\folder``.
        writer_log: Where the writer's evidence log can be read from this
            machine (``\\scanner-pc\share\omrflow-smb-logs\writer_A.jsonl``).
        writer_target: The ``-Target`` the writer is told to use on its own
            machine (normally the shared folder's local path, e.g. ``D:\Scans\omr``).
        writer_log_local: The ``-Log`` the writer is told to use on its machine.
    """

    label: str
    root: str
    writer_log: str
    writer_target: str = ""
    writer_log_local: str = ""


def smb_config(*, sources: int, files_per_source: int, seed: int, timing_seed: int,
               duration_seconds: float, workers: int,
               stability: StabilitySettings | None = None,
               candidates_per_set: int | None = None) -> CampaignConfig:
    """The campaign configuration: the Phase 9 cohort for ``sources`` scanners.

    The kill / outage / checkpoint fields of the Phase 9 supervisor are not
    used by the SMB run (its restart and its outage are driven by
    :mod:`~omr_scanner.evaluation.smb_qualification.run`); they keep their
    defaults so the plan - and its digest - stays the cohort planner's.
    ``candidates_per_set`` defaults to an estimate; :func:`sized_config`
    plans and grows it until every source really gets its minimum.
    """
    # About 1.03 arrivals per candidate (byte copies, rescans, stray pages)
    # over four sets.
    per_set = candidates_per_set or max(30, -(-int(files_per_source * sources * 1.05) // 4))
    return replace(
        CampaignConfig(mode=Mode.CUSTOM),
        seed=seed,
        timing_seed=timing_seed,
        sources=sources,
        candidates_per_set=per_set,
        duration_seconds=duration_seconds,
        control_duration_seconds=duration_seconds,
        workers=workers,
        unit_size=200 if per_set >= 500 else 25,
        trickle_seconds=30.0 if per_set >= 500 else 4.0,
        stability=stability or network_stability(),
        run_control=False,
        run_finite=False,
        reprocess=False,
        stage_timeout_seconds=7_200.0,
    )


def per_source_counts(plan: Any) -> dict[str, int]:
    """Main-timeline files each source writes."""
    return dict(Counter(item.source for item in plan.main_arrivals))


def sized_config(template: Any, *, sources: int, files_per_source: int,
                 **settings: Any) -> CampaignConfig:
    """:func:`smb_config` with the roster grown until every source gets ``files_per_source``.

    The cohort planner assigns arrivals to scanners at random, so the split is
    uneven (found by the plan test: 945 / 1,148 for a 2,000-file estimate).
    Planning is deterministic, so the grown configuration plans identically
    when the run re-plans it.
    """
    from omr_scanner.evaluation.intake_qualification.cohort import plan_campaign

    config = smb_config(sources=sources, files_per_source=files_per_source, **settings)
    for _attempt in range(8):
        counts = per_source_counts(plan_campaign(config, template))
        fewest = min((counts.get(label, 0) for label in config.source_labels), default=0)
        if fewest >= files_per_source:
            return config
        grown = -(-config.candidates_per_set * files_per_source * 103 // (max(1, fewest) * 100))
        config = replace(config, candidates_per_set=max(grown, config.candidates_per_set + 1))
    raise ValueError(f"could not plan {files_per_source} files for every source")


def _schedule(campaign_id: str, label: str, plan: Any, pool: dict[str, Any]) -> dict[str, Any]:
    items = sorted((item for item in plan.main_arrivals if item.source == label),
                   key=lambda value: (value.at, value.seq))
    return {
        "campaign": campaign_id,
        "source": label,
        "plan_digest": plan.digest(),
        "arrivals": [
            {
                "seq": item.seq, "name": item.name, "content": item.content, "at": item.at,
                "pattern": item.pattern.value, "pauses": list(item.pauses),
                "hold_after": item.hold_after, "pool": f"pool/{item.content}.png",
                "sha256": pool[item.content].sha256, "size": pool[item.content].size,
            }
            for item in items
        ],
    }


def write_package(folder: Path, campaign_id: str, spec: SourceSpec, plan: Any,
                  pool: dict[str, Any]) -> dict[str, Any]:
    """One scanner PC's package; returns its summary (files, bytes, SHA-256 of the schedule)."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pool").mkdir(exist_ok=True)
    schedule = _schedule(campaign_id, spec.label, plan, pool)
    needed = sorted({item["content"] for item in schedule["arrivals"]})
    total = 0
    for content in needed:
        source = Path(pool[content].path)
        target = folder / "pool" / f"{content}.png"
        shutil.copyfile(source, target)
        total += target.stat().st_size
    text = json.dumps(schedule, indent=1)
    (folder / "schedule.json").write_text(text, encoding="utf-8")
    shutil.copyfile(writer_script(), folder / WRITER_SCRIPT_NAME)
    writer_target = spec.writer_target or r"D:\Scans\omr"
    log = spec.writer_log_local or r"D:\Scans\omrflow-smb-logs" + f"\\writer_{spec.label}.jsonl"
    (folder / "RUN-ON-SCANNER-PC.txt").write_text(
        f"OMRFlow SMB qualification - scanner {spec.label}, campaign {campaign_id}\r\n\r\n"
        "Run this on the scanner PC (NOT on the OMRFlow machine), from this folder,\r\n"
        "when the OMRFlow machine's supervisor says it is waiting for the writers:\r\n\r\n"
        f"powershell -NoProfile -ExecutionPolicy Bypass -File .\\{WRITER_SCRIPT_NAME} "
        f"-Package . -Target \"{writer_target}\" -Log \"{log}\"\r\n\r\n"
        f"-Target must be the folder OMRFlow watches as {spec.root}\r\n"
        f"-Log must be readable from the OMRFlow machine as {spec.writer_log}\r\n"
        "and must not be inside -Target.\r\n",
        encoding="utf-8",
    )
    return {"label": spec.label, "files": len(schedule["arrivals"]), "images": len(needed),
            "bytes": total, "schedule_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def prepare(output_root: Path, sources: list[SourceSpec], *, template_path: Path,
            files_per_source: int = MIN_FILES_PER_SOURCE, seed: int = 20261007,
            timing_seed: int = 7102026, duration_seconds: float = 1_800.0, workers: int = 6,
            stability: StabilitySettings | None = None, progress: Any = None) -> Path:
    """Plan, render and package a new SMB qualification campaign; return its folder."""
    from omr_scanner.evaluation.intake_qualification.supervisor import prepare_campaign

    if len({item.label for item in sources}) != len(sources):
        raise ValueError("source labels must be distinct")
    labels = [item.label for item in sources]
    if labels != list("ABCDEFGH"[: len(sources)]):
        raise ValueError(f"source labels must be A, B, ... in order, not {labels}")
    from omr_scanner.services.template_service import load_template

    config = sized_config(load_template(template_path), sources=len(sources),
                          files_per_source=files_per_source, seed=seed, timing_seed=timing_seed,
                          duration_seconds=duration_seconds, workers=workers,
                          stability=stability)
    campaign = prepare_campaign(config, output_root, template_path, progress=progress)
    counts = per_source_counts(campaign.plan)
    short = {label: count for label, count in counts.items() if count < files_per_source}
    if short:
        raise ValueError(f"the plan gives too few files to {short} (minimum {files_per_source})")
    packages = {}
    for spec in sources:
        packages[spec.label] = write_package(
            campaign.root / "packages" / f"writer_{spec.label}", campaign.campaign_id, spec,
            campaign.plan, campaign.pool,
        )
    record = {
        "campaign_id": campaign.campaign_id,
        "config": config.to_json(),
        "plan_digest": campaign.plan.digest(),
        "template": str(campaign.template_path),
        "files_per_source_required": files_per_source,
        "files_per_source_planned": counts,
        "sources": [asdict(item) for item in sources],
        "packages": packages,
        "writer_script_sha256": hashlib.sha256(writer_script().read_bytes()).hexdigest(),
    }
    (campaign.root / SMB_FILE).write_text(json.dumps(record, indent=1), encoding="utf-8")
    from omr_scanner.evaluation.smb_qualification.instructions import write_operator_steps

    write_operator_steps(campaign.root, record)
    return campaign.root


def load(root: Path) -> tuple[Any, dict[str, Any]]:
    """Reopen a prepared campaign: ``(Campaign, smb.json record)``."""
    from omr_scanner.evaluation.intake_qualification import cohort
    from omr_scanner.evaluation.intake_qualification.evidence import EvidenceLog
    from omr_scanner.evaluation.intake_qualification.render import render_pool
    from omr_scanner.evaluation.intake_qualification.supervisor import Campaign
    from omr_scanner.services.template_service import load_template

    root = root.resolve()
    record = json.loads((root / SMB_FILE).read_text(encoding="utf-8"))
    config = CampaignConfig.from_json(record["config"])
    template_path = Path(record["template"])
    plan = cohort.plan_campaign(config, load_template(template_path))
    if plan.digest() != record["plan_digest"]:
        raise ValueError("the re-planned campaign differs from the prepared one")
    pool = render_pool(plan, template_path, root / "pool", workers=2)
    log = EvidenceLog(root / "supervisor.jsonl", campaign_id=record["campaign_id"],
                      role="supervisor")
    campaign = Campaign(config=config, campaign_id=record["campaign_id"], root=root,
                        template_path=template_path, plan=plan, pool=pool, log=log)
    return campaign, record


__all__ = [
    "MIN_FILES_PER_SOURCE",
    "MIN_SOURCES",
    "SMB_FILE",
    "SourceSpec",
    "load",
    "network_stability",
    "per_source_counts",
    "prepare",
    "smb_config",
    "write_package",
    "writer_script",
]
