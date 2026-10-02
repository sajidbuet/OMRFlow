"""Revised phase 5 acceptance: three sources, every intake hazard, exact ledger.

One deterministic headless scenario (fake filesystem, fake clock, real project
database and the real intake service) covering, in one session:

* the same file name on A and B with different bytes;
* identical bytes on A and C under different names;
* a slowly growing file (B), a file held open (C), a ``.tmp`` renamed into
  place (A), a zero-byte placeholder later completed (B), a permanently
  truncated image (C), a file that disappears before it is ready (A), a path
  reused for new bytes after registration (A);
* source A becoming unreachable and returning, with a file arriving during the
  outage, while B and C carry on;
* a restart mid-stabilisation, and files created while intake is stopped;
* a recursive source (C) with a folder exclusion, an excluded name and an
  unsupported file; a multi-page TIFF (B).

The end state is compared as **exact sets** of ledger identities and states,
not counts, and every intermediate pass checks that no partial file is ever
ready.
"""

from __future__ import annotations

from sqlalchemy import select
from tests.intake_fakes import header_only, jpeg, multipage_tiff, png
from tests.integration.test_intake_ledger import POLICY, Rig

from omr_scanner.database.models import BatchScan
from omr_scanner.domain.intake import Exclusions, IntakeState, Reachability
from omr_scanner.services import intake as intake_service
from omr_scanner.services.scan_provenance import hash_bytes

A = r"\\scanner-a\scans"
B = r"D:\exam\Scanner B (room 2)"
C = r"E:\ছবি\scanner c"

IMAGES = {f"img{index}": jpeg(1000 + index) for index in range(1, 14)}
IMAGES["img4"] = png(1004)  # B's slowly written file is a PNG
COMPLETE = {hash_bytes(data): name for name, data in IMAGES.items()}
TIFF_BOOK = multipage_tiff(3, seed=55)
BROKEN = header_only(jpeg(1008), 0.6)
CONTENT_STATES = (IntakeState.READY, IntakeState.REGISTERED, IntakeState.DUPLICATE_CONTENT)


class Scenario(Rig):
    def __init__(self, project_session, template) -> None:
        super().__init__(project_session, template)
        self.a = self.source(A, "Scanner A")
        self.b = self.source(B, "Scanner B")
        self.c = self.source(
            C, "Scanner C", recursive=True,
            exclusions=Exclusions(files=("*_preview.jpg",), folders=("excluded",)),
        )
        self.labels = {self.a: "A", self.b: "B", self.c: "C"}
        self.passes = 0

    def poll_all(self, advance: float = 0.0) -> list[intake_service.ReconcileReport]:
        if advance:
            self.clock.advance(advance)
        reports = list(self.service.reconcile_all())
        self.passes += 1
        self.assert_no_partial_file_is_ready()
        return reports

    def assert_no_partial_file_is_ready(self) -> None:
        for row in intake_service.ledger(self.database):
            if row.state in CONTENT_STATES:
                assert row.content_sha256 in COMPLETE, (row.relative_path, row.state)

    def register_everything(self) -> list[intake_service.Registration]:
        return [self.register_all(source) for source in (self.a, self.b, self.c)]

    def identities(self) -> set[tuple[str, str, IntakeState, str | None, bool]]:
        found = set()
        for row in intake_service.ledger(self.database):
            content = None
            if row.content_sha256 is not None:
                known = {
                    hash_bytes(TIFF_BOOK): "tiff-book",
                    hash_bytes(BROKEN): "broken",
                    hash_bytes(header_only(IMAGES["img4"], 2 / 3)): "img4-two-thirds",
                }
                content = COMPLETE.get(row.content_sha256) or known.get(row.content_sha256)
                assert content is not None, (
                    self.labels[row.source_id], row.relative_path, row.state, row.detail,
                )
            found.add((self.labels[row.source_id], row.relative_path, row.state, content,
                       row.path_reused))
        return found


def test_three_source_acceptance(project_session, answer_sheet_template):
    rig = Scenario(project_session, answer_sheet_template)
    fs, img = rig.fs, IMAGES
    # --- t0: what the three scanners have written so far -----------------
    fs.write(A, "000001.jpg", img["img1"])
    fs.write(A, "000002.jpg", img["img2"])
    fs.write(A, "scan.tmp", header_only(img["img6"]))
    fs.write(A, "gone.jpg", header_only(img["img2"], 0.3))
    fs.write(B, "000001.jpg", img["img3"])  # same name as A's, other bytes
    fs.write(B, "000002.png", header_only(img["img4"], 1 / 3))  # growing slowly
    fs.write(B, "000004.jpg", b"")  # zero-byte placeholder
    fs.write(B, "book.tif", TIFF_BOOK)
    fs.write(C, "sub/000010.jpg", img["img1"])  # A's 000001 bytes, another name
    fs.write(C, "held.jpg", img["img5"])
    fs.file(C, "held.jpg").locked = True
    fs.write(C, "broken.jpg", BROKEN)
    fs.write(C, "notes.txt", b"scanner log")
    fs.write(C, "thumb_preview.jpg", img["img2"])
    fs.write(C, "excluded/x.jpg", img["img3"])
    rig.poll_all()
    rig.poll_all(advance=POLICY.quiet_seconds + 1)
    assert rig.state(rig.a, "000001.jpg") is IntakeState.READY
    assert rig.state(rig.b, "000002.png") is IntakeState.STABILIZING  # header only
    assert rig.state(rig.c, "held.jpg") is IntakeState.STABILIZING  # locked
    assert rig.state(rig.b, "book.tif") is IntakeState.UNSUPPORTED
    first = rig.register_everything()
    a_scan = dict(first[0].registered)[rig.row(rig.a, "000001.jpg").intake_file_id]
    assert first[2].duplicates == ((rig.row(rig.c, "sub/000010.jpg").intake_file_id, a_scan),)

    # --- the writers carry on -------------------------------------------
    fs.delete(A, "gone.jpg")  # vanishes before it was ever ready
    fs.write(A, "scan.tmp", img["img6"])
    fs.rename(A, "scan.tmp", "000003.jpg")
    fs.write(B, "000004.jpg", img["img7"])
    fs.write(B, "000002.png", header_only(img["img4"], 2 / 3))
    fs.file(C, "held.jpg").locked = False
    rig.poll_all(advance=1)
    assert rig.state(rig.a, "gone.jpg") is IntakeState.VANISHED
    rig.poll_all(advance=POLICY.quiet_seconds + 1)
    rig.register_everything()

    # --- A goes offline; a file arrives there meanwhile; B and C carry on ---
    fs.unreachable.add(A)
    fs.write(A, "000004.jpg", img["img12"])
    fs.write(C, "sub/000012.jpg", img["img13"])
    outage = [rig.poll_all(advance=6), rig.poll_all(advance=6), rig.poll_all(advance=12)]
    assert {r.reachability for reports in outage for r in reports if r.source_id == rig.a} == {
        Reachability.UNREACHABLE
    }
    during = rig.register_all(rig.c)
    assert len(during.registered) == 1  # C kept registering while A was down
    a_rows = intake_service.ledger(rig.database, source_id=rig.a)
    assert IntakeState.VANISHED not in {r.state for r in a_rows if r.relative_path != "gone.jpg"}
    assert rig.state(rig.c, "broken.jpg") is IntakeState.UNREADABLE

    # --- restart while B's file is mid-stabilisation ---------------------
    fs.write(B, "000002.png", img["img4"])
    rig.poll_all(advance=1)
    rig.restart()
    rig.poll_all(advance=30)  # long after - but stability is not trusted across a restart
    assert rig.state(rig.b, "000002.png") is IntakeState.STABILIZING
    rig.poll_all(advance=POLICY.quiet_seconds + 0.5)
    assert rig.state(rig.b, "000002.png") is IntakeState.READY

    # --- A returns; its counter is reset and 000001 is overwritten -------
    fs.unreachable.discard(A)
    rig.clock.advance(60)
    fs.write(A, "000001.jpg", img["img9"])
    rig.poll_all(advance=1)
    assert rig.row(rig.a, "000004.jpg").state is IntakeState.DISCOVERED  # found on return
    rig.poll_all(advance=POLICY.quiet_seconds + 1)
    rig.register_everything()

    # --- intake stopped; scanners keep writing; intake restarts ----------
    fs.write(C, "sub/000011.jpg", img["img10"])
    fs.write(B, "000005.jpg", img["img11"])
    rig.clock.advance(300)
    rig.restart()
    rig.poll_all()
    rig.poll_all(advance=POLICY.quiet_seconds + 1)
    rig.register_everything()
    rig.poll_all(advance=60)
    rig.register_everything()  # nothing more: idempotent

    expected = {
        ("A", "000001.jpg", IntakeState.REGISTERED, "img1", False),
        ("A", "000001.jpg", IntakeState.REGISTERED, "img9", True),
        ("A", "000002.jpg", IntakeState.REGISTERED, "img2", False),
        ("A", "000003.jpg", IntakeState.REGISTERED, "img6", False),
        ("A", "000004.jpg", IntakeState.REGISTERED, "img12", False),
        ("A", "scan.tmp", IntakeState.IGNORED, None, False),
        ("A", "gone.jpg", IntakeState.VANISHED, None, False),
        ("B", "000001.jpg", IntakeState.REGISTERED, "img3", False),
        # The writer stalled at two thirds for longer than the decode retries
        # (through A's outage): that version is recorded unreadable; the
        # completed file is a new observation and registers normally.
        ("B", "000002.png", IntakeState.UNREADABLE, "img4-two-thirds", False),
        ("B", "000002.png", IntakeState.REGISTERED, "img4", False),
        ("B", "000004.jpg", IntakeState.REGISTERED, "img7", False),
        ("B", "000005.jpg", IntakeState.REGISTERED, "img11", False),
        ("B", "book.tif", IntakeState.UNSUPPORTED, "tiff-book", False),
        ("C", "sub/000010.jpg", IntakeState.DUPLICATE_CONTENT, "img1", False),
        ("C", "sub/000011.jpg", IntakeState.REGISTERED, "img10", False),
        ("C", "sub/000012.jpg", IntakeState.REGISTERED, "img13", False),
        ("C", "held.jpg", IntakeState.REGISTERED, "img5", False),
        ("C", "broken.jpg", IntakeState.UNREADABLE, "broken", False),
        ("C", "notes.txt", IntakeState.IGNORED, None, False),
        ("C", "thumb_preview.jpg", IntakeState.IGNORED, None, False),
    }
    assert rig.identities() == expected
    assert len(intake_service.ledger(rig.database)) == len(expected)  # no hidden extra rows

    with rig.database.session() as session:
        scans = session.execute(
            select(BatchScan.scan_id, BatchScan.content_sha256, BatchScan.status,
                   BatchScan.intake_file_id)
        ).all()
    read = [COMPLETE[digest] for _id, digest, status, _intake in scans if status != "duplicate"]
    assert sorted(read) == sorted(
        ["img1", "img9", "img2", "img6", "img12", "img3", "img4", "img7", "img11", "img10",
         "img13", "img5"]
    )  # each unique content exactly once; nothing partial; nothing twice
    assert len(set(read)) == len(read)
    duplicate_scans = [s for s in scans if s[2] == "duplicate"]
    assert len(duplicate_scans) == 1 and COMPLETE[duplicate_scans[0][1]] == "img1"
    # Provenance: every registered scan names the ledger row of its source and path.
    by_scan = {row.batch_scan_id: row for row in intake_service.ledger(rig.database)}
    for scan_id, digest, _status, intake_id in scans:
        row = by_scan[scan_id]
        assert row.intake_file_id == intake_id and row.content_sha256 == digest
    # The sources were only ever read.
    assert fs.writes_to_sources == 0
    assert fs.file(C, "excluded/x.jpg").data == img["img3"]
