"""Child processes for the real-process intake tests (0.1.1 revised phase 5).

Two roles, chosen by the first argument:

``writer FOLDER COUNT SEED [--start N] [--chunks K] [--delay S] [--manifest FILE]``
    A scanner that writes ``COUNT`` counter-named JPEGs (``000001.jpg`` ...)
    **directly to their final names**, each in ``K`` chunks with ``S``
    seconds between chunks - the non-atomic writer intake must survive. After
    each file's last byte it appends ``name sha256`` to the manifest: the
    ground truth of what was completely written.

``intake PROJECT [--until-idle S] [--max-seconds S]``
    The intake engine for one project, as phase 6 will run it: open the
    project (taking over a lock whose holder is dead, as an operator would),
    construct :class:`~omr_scanner.services.intake.IntakeService` (restart
    recovery runs), then loop: reconcile every source, register each
    source's ready files into its attached session, sleep briefly. With
    ``--until-idle`` it exits once nothing has changed for that long; the
    tests also kill it hard mid-loop.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path


def _writer(arguments: argparse.Namespace) -> int:
    from tests.intake_fakes import jpeg

    folder = Path(arguments.folder)
    folder.mkdir(parents=True, exist_ok=True)
    manifest = Path(arguments.manifest) if arguments.manifest else None
    for index in range(arguments.start, arguments.start + arguments.count):
        data = jpeg(arguments.seed * 100_000 + index)
        name = f"{index:06d}.jpg"
        size = max(1, len(data) // arguments.chunks + 1)
        with (folder / name).open("wb") as handle:
            for offset in range(0, len(data), size):
                handle.write(data[offset : offset + size])
                handle.flush()
                time.sleep(arguments.delay)
        if manifest is not None:
            with manifest.open("a", encoding="utf-8") as record:
                record.write(f"{name} {hashlib.sha256(data).hexdigest()}\n")
    return 0


def _intake(arguments: argparse.Namespace) -> int:
    from tests.conftest import build_answer_sheet_template

    from omr_scanner.services import batch_store, open_project
    from omr_scanner.services import intake as intake_service
    from omr_scanner.services.project_lock import ProjectLockHeldError

    root = Path(arguments.project)
    try:
        project = open_project(root)
    except ProjectLockHeldError as exc:
        if not exc.holder.likely_stale:
            raise
        project = open_project(root, force_lock=True)
    identity = batch_store.BatchIdentity.of(build_answer_sheet_template())
    started = time.monotonic()
    quiet_since = time.monotonic()
    try:
        database = project.database
        service = intake_service.IntakeService(database, root)
        print("recovered", service.last_recovery, flush=True)
        while True:
            changed = False
            for report in service.reconcile_all():
                changed |= bool(
                    report.new_rows or report.changed or report.ready or report.vanished
                    or report.reappeared or report.unreadable
                )
            for source in intake_service.list_sources(database):
                if source.attached_session_id is None:
                    continue
                items = service.ready_items(
                    scan_session_id=source.attached_session_id, source_id=source.source_id
                )
                if items:
                    outcome = service.register(
                        scan_session_id=source.attached_session_id,
                        source_id=source.source_id,
                        intake_file_ids=[item.intake_file_id for item in items],
                        identity=identity,
                        acknowledge_template_change=True,
                    )
                    changed = True
                    print("registered", len(outcome.registered), flush=True)
            now = time.monotonic()
            if changed:
                quiet_since = now
            if arguments.until_idle and now - quiet_since >= arguments.until_idle:
                return 0
            if arguments.max_seconds and now - started >= arguments.max_seconds:
                return 3
            time.sleep(0.1)
    finally:
        project.close()


def main(argv: list[str] | None = None) -> int:
    """Run one child role."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    roles = parser.add_subparsers(dest="role", required=True)
    writer = roles.add_parser("writer")
    writer.add_argument("folder")
    writer.add_argument("count", type=int)
    writer.add_argument("seed", type=int)
    writer.add_argument("--start", type=int, default=1)
    writer.add_argument("--chunks", type=int, default=4)
    writer.add_argument("--delay", type=float, default=0.05)
    writer.add_argument("--manifest", default="")
    intake = roles.add_parser("intake")
    intake.add_argument("project")
    intake.add_argument("--until-idle", type=float, default=0.0)
    intake.add_argument("--max-seconds", type=float, default=0.0)
    arguments = parser.parse_args(argv)
    return _writer(arguments) if arguments.role == "writer" else _intake(arguments)


if __name__ == "__main__":
    sys.exit(main())
