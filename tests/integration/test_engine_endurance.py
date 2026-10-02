"""Targeted endurance of the continuous engine (``stress``; revised phase 6).

Not the phase 9 synthetic intake campaign and not a 100,000-sheet
qualification: a seeded, deterministic workload sized to exercise the
architecture for a long run on this machine -

* three sources, several thousand files arriving in waves;
* many finite units (one source each, sealed);
* registration interleaved with processing;
* a seeded kill at a random durable boundary after every wave, then restart;
* the final session compared with an uninterrupted run of the same workload.

Recognition is replayed from 60 genuinely recognised sheets: file *i* returns
sheet *i mod 60*'s real result (so Student IDs repeat - a heavy, realistic
load for the session-wide duplicate pass). The files are small distinct images,
so intake's hash, decode and copy are real. Sizes come from
``OMRFLOW_ENDURANCE_SHEETS`` (default 3,000) and ``OMRFLOW_ENDURANCE_WAVES``
(default 10). Timing and database size are printed for the handoff.
"""

from __future__ import annotations

import dataclasses
import os
import random
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from tests.crash.harness import integrity
from tests.engine_rig import (
    OPTIONS,
    EngineRig,
    Killed,
    KillAt,
    committed_contents,
    digest,
    durable_view,
    readable_sheets,
    structure,
)
from tests.intake_fakes import png

from omr_scanner.database.models import BatchScan, BatchSupersession, ScanJobStatus
from omr_scanner.domain.processing import EngineLimits, UnitPolicy
from omr_scanner.services import create_project, session_population
from omr_scanner.services.recognition_models import ScanResult
from omr_scanner.services.recognition_pool import InlineRecogniser
from omr_scanner.services.recognition_service import recognise_scan

pytestmark = pytest.mark.stress

SHEETS = int(os.environ.get("OMRFLOW_ENDURANCE_SHEETS", "3000"))
WAVES = int(os.environ.get("OMRFLOW_ENDURANCE_WAVES", "10"))
BOUNDARIES = ["claimed", "submitted", "recognised", "before_commit", "committed", "finalising"]


@pytest.fixture(scope="module")
def workload(tmp_path_factory) -> tuple[list[bytes], list[ScanResult]]:
    """Distinct small images, and 60 real results to replay onto them."""
    base_dir = tmp_path_factory.mktemp("endurance-base")
    from tests.crash.harness import template

    real = []
    for index, data in enumerate(readable_sheets(70)[:60]):
        path = base_dir / f"{index:03d}.png"
        path.write_bytes(data)
        real.append(recognise_scan(path, template(), options=OPTIONS))
    files: list[bytes] = []
    seen: set[str] = set()
    seed = 0
    while len(files) < SHEETS:
        data = png(seed)
        seed += 1
        if digest(data) not in seen:
            seen.add(digest(data))
            files.append(data)
    return files, real


class Replay:
    """File *i*'s recognition is base result *i mod 60*; every call counted."""

    def __init__(self, files: list[bytes], base: list[ScanResult]) -> None:
        self.index = {digest(data): number for number, data in enumerate(files)}
        self.base = base
        self.calls: Counter[str] = Counter()

    def __call__(self, path: Path, template, options) -> ScanResult:  # type: ignore[no-untyped-def]
        key = digest(path.read_bytes())
        self.calls[key] += 1
        return dataclasses.replace(self.base[self.index[key] % len(self.base)], source_path=path)


@pytest.fixture
def projects(workspace) -> Iterator[list]:
    opened: list = []
    yield opened
    for project in opened:
        project.close()


def run(projects, workspace, name: str, workload, *, kill_seed: int | None) -> dict:
    files, base = workload
    project = create_project(workspace, name)
    projects.append(project)
    rig = EngineRig(project)
    replay = Replay(files, base)
    rig.recognise = replay  # type: ignore[assignment]
    for source in "abc":
        rig.source(source)
    chooser = random.Random(kill_seed) if kill_seed is not None else None
    limits = EngineLimits(max_in_flight=16, claim_window=16, max_units_per_poll=2)
    policy = UnitPolicy(max_unit_size=50, trickle_seconds=0)
    per_wave = -(-len(files) // WAVES)
    kills: list[tuple[set[str], Counter[str]]] = []
    max_in_flight = 0
    started = time.perf_counter()
    for wave in range(WAVES):
        for index in range(wave * per_wave, min(len(files), (wave + 1) * per_wave)):
            rig.write("abc"[index % 3], [(f"{index:06d}.png", files[index])])
        hooks = None
        if chooser is not None:
            hooks = KillAt(chooser.choice(BOUNDARIES), chooser.randint(1, 40))
        engine = rig.new_engine(
            hooks=hooks, limits=limits, unit_policy=policy,
            recogniser=InlineRecogniser(rig.template, recognise=replay, per_poll=4),
        )
        rig.make_ready()
        try:
            while True:
                engine.form_units()
                report = engine.step()
                max_in_flight = max(max_in_flight, engine.in_flight)
                if report.idle and not engine.in_flight:
                    # Reconcile only when idle - as a poll interval would.
                    engine.poll_intake()
                    engine.form_units()
                    if engine.status().caught_up:
                        break
            engine.shutdown()
        except Killed:
            kills.append((committed_contents(rig.database), Counter(replay.calls)))
    # Finish whatever the last kill left.
    engine = rig.new_engine(
        limits=limits, unit_policy=policy,
        recogniser=InlineRecogniser(rig.template, recognise=replay, per_poll=4),
    )
    rig.make_ready()
    rig.run()
    engine.shutdown()
    elapsed = time.perf_counter() - started
    for committed, calls_then in kills:
        for key in committed:
            assert replay.calls[key] == calls_then[key], "re-read after its commit"
    size = (project.root / "database.sqlite").stat().st_size
    units = len(structure(rig.database, rig.session_id)["batches"])
    print(
        f"\n{name}: {len(files)} files, {WAVES} waves, {units} units, {len(kills)} kills, "
        f"{elapsed:.1f} s ({len(files) / elapsed:.1f} files/s end to end, replayed recognition), "
        f"max in flight {max_in_flight}, database {size / 1_048_576:.1f} MiB"
    )
    assert max_in_flight <= limits.max_in_flight
    with rig.database.session() as session:
        statuses = Counter(session.scalars(select(BatchScan.status)).all())
        supersessions = session.scalar(select(func.count()).select_from(BatchSupersession))
    assert ScanJobStatus.PROCESSING.value not in statuses
    assert ScanJobStatus.PENDING.value not in statuses
    assert not supersessions
    population = session_population.session_population(rig.database, rig.session_id)
    view = durable_view(rig.database, rig.session_id)
    assert len(population.effective) == len(view["results"])
    checks = integrity(project.root)
    assert checks.sqlite_ok and checks.health_errors == [], checks
    return {"view": view, "kills": len(kills), "calls": replay.calls}


def test_long_session_with_waves_and_kills_equals_an_uninterrupted_run(projects, workspace,
                                                                        workload):
    control = run(projects, workspace, "Control", workload, kill_seed=None)
    killed = run(projects, workspace, "Killed", workload, kill_seed=20261002)
    assert killed["kills"] >= WAVES // 2
    assert killed["view"] == control["view"]
    assert set(control["calls"].values()) == {1}
    assert sum(killed["calls"].values()) - sum(control["calls"].values()) <= 16 * killed["kills"]
