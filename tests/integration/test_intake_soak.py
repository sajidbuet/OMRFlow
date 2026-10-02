"""Randomised intake soak: 2,400 arrivals over three sources (``stress``).

Seeded, so a failure reproduces. Scanners write in random patterns - direct
writes in chunks with random pauses, ``.tmp`` then rename, zero-byte
placeholders, held-open files, the odd deletion and counter reset - while
intake polls at random intervals and is restarted at random. At the end every
file that exists completely is registered exactly once with its final bytes,
no partial file ever became ready, and nothing was read twice into a scan.

Run with ``pytest -m stress -k intake``.
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import select
from tests.intake_fakes import jpeg
from tests.integration.test_intake_ledger import Rig

from omr_scanner.database.models import BatchScan
from omr_scanner.domain.intake import IntakeState
from omr_scanner.services import intake as intake_service
from omr_scanner.services.scan_provenance import hash_bytes

pytestmark = pytest.mark.stress

ROOTS = (r"\\scanner-a\scans", r"D:\Scanner B", r"E:\scanner c")


def test_randomised_arrivals(project_session, answer_sheet_template):
    rng = random.Random(20261002)
    rig = Rig(project_session, answer_sheet_template)
    sources = {root: rig.source(root) for root in ROOTS}
    fs = rig.fs
    complete: set[str] = set()
    pending: list[tuple[str, str, bytes, int, bool]] = []  # root, name, data, written, via tmp
    counters = dict.fromkeys(ROOTS, 0)
    produced = 0
    target = 2400

    def start_file() -> None:
        nonlocal produced
        root = rng.choice(ROOTS)
        counters[root] += 1
        if rng.random() < 0.01:
            counters[root] = max(1, counters[root] - 50)  # counter reset: path reuse
        name = f"{counters[root]:06d}.jpg"
        data = jpeg(produced + 1)
        produced += 1
        via_tmp = rng.random() < 0.2
        written = 0 if rng.random() < 0.1 else rng.randint(1, len(data) - 1)
        fs.write(root, f"{name}.tmp" if via_tmp else name, data[:written])
        pending.append((root, name, data, written, via_tmp))

    def step_writers() -> None:
        for index in range(len(pending) - 1, -1, -1):
            root, name, data, written, via_tmp = pending[index]
            current = f"{name}.tmp" if via_tmp else name
            if rng.random() < 0.3:
                continue  # paused writer
            written = min(len(data), written + rng.randint(200, 3000))
            fs.write(root, current, data[:written])
            if written < len(data):
                pending[index] = (root, name, data, written, via_tmp)
                continue
            if via_tmp:
                if name in fs.roots[root]:
                    fs.delete(root, name)
                fs.rename(root, current, name)
            pending.pop(index)
            if rng.random() < 0.02:
                fs.delete(root, name)  # operator deleted it again
            else:
                complete.add(f"{root}|{name}")

    while produced < target or pending:
        for _ in range(rng.randint(0, 12)):
            if produced < target:
                start_file()
        step_writers()
        rig.clock.advance(rng.uniform(0.5, 4.0))
        rig.service.reconcile_all()
        for row in intake_service.ledger(rig.database):
            if row.state in (IntakeState.READY, IntakeState.REGISTERED):
                assert row.file_size > 0
        if rng.random() < 0.3:
            for source in sources.values():
                rig.register_all(source)
        if rng.random() < 0.02:
            rig.restart()

    for _ in range(6):  # settle everything still waiting
        rig.clock.advance(20)
        rig.service.reconcile_all()
        for source in sources.values():
            rig.register_all(source)

    expected = {}
    for key in complete:
        root, name = key.split("|")
        if name in fs.roots[root]:
            expected[key] = hash_bytes(fs.roots[root][name].data)
    labels = {source: root for root, source in sources.items()}
    current = {
        f"{labels[row.source_id]}|{row.relative_path}": row
        for row in intake_service.ledger(rig.database, current_only=True)
        if not row.relative_path.endswith(".tmp")
    }
    for key, digest in expected.items():
        row = current[key]
        assert row.content_sha256 == digest, key
        consumed = (IntakeState.REGISTERED, IntakeState.DUPLICATE_CONTENT)
        assert row.state in consumed, (key, row.state)
    with rig.database.session() as session:
        read = [
            digest
            for digest, status in session.execute(
                select(BatchScan.content_sha256, BatchScan.status)
            ).all()
            if status != "duplicate"
        ]
    assert len(read) == len(set(read)), "the same bytes became two scans"
    valid = {hash_bytes(jpeg(index)) for index in range(1, produced + 1)}
    assert set(read) <= valid, "a partial file was registered"
    # 2,400 arrivals; counter resets overwrite paths, so fewer files remain.
    assert produced == target
    assert len(expected) > 1000
