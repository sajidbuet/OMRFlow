r"""Feed three local "scanner" folders over time, for a manual operational-GUI run.

Purpose:
    A reproducible way to watch the Scan stage's session mode do its work on
    this machine: three folders standing for *Scanner A*, *Scanner B* and
    *Scanner C* receive synthetic answer sheets gradually - some written in
    partial steps, as a slow scanner or a network copy would - while OMRFlow
    runs from source. Optionally one folder disappears for a while (an
    unreachable scanner), and a few sheets carry damage that the quality
    policy suggests for a rescan; their clean rescans arrive later at *another*
    scanner.

    Operator-style local validation only. **Not** the phase 9 synthetic
    qualification and not network-share or real-scanner evidence.

Usage::

    # 1. Prepare a project whose template matches the sheets, and the folders:
    python scripts/dev_feed_scanner_folders.py prepare --root C:\Temp\omr-feed

    # 2. Open the printed project in OMRFlow (python -m omr_scanner), Scan stage:
    #    Session > Add Scanner Source... three times, one per printed folder,
    #    then Start Continuous Scan.

    # 3. Feed the folders (in another terminal):
    python scripts/dev_feed_scanner_folders.py feed --root C:\Temp\omr-feed ^
        --sheets 150 --rate 2 --outage 30 --damage 4

Nothing is written outside ``--root``; generated sheets are never committed.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for entry in (REPOSITORY_ROOT / "src", REPOSITORY_ROOT):
    if str(entry) not in sys.path:  # pragma: no cover - script bootstrap
        sys.path.insert(0, str(entry))

SCANNERS = ("Scanner A", "Scanner B", "Scanner C")


def _folders(root: Path) -> dict[str, Path]:
    return {name: root / "scanners" / name.replace(" ", "_") for name in SCANNERS}


def prepare(root: Path) -> int:
    """Create the project (with the synthetic sheets' own template) and the folders."""
    from tests.crash.harness import create_project_with_template

    root.mkdir(parents=True, exist_ok=True)
    project = create_project_with_template(root, "Phase8 Manual Run")
    for folder in _folders(root).values():
        folder.mkdir(parents=True, exist_ok=True)
    print(f"Project: {project}")
    for name, folder in _folders(root).items():
        print(f"{name}: {folder}")
    return 0


def _write(path: Path, data: bytes, *, partial: bool, step_seconds: float) -> None:
    """Write a file - in three steps when ``partial`` (a scanner still writing)."""
    if not partial:
        path.write_bytes(data)
        return
    third = max(1, len(data) // 3)
    with path.open("wb") as handle:
        for start in range(0, len(data), third):
            handle.write(data[start:start + third])
            handle.flush()
            time.sleep(step_seconds)


def feed(
    root: Path, *, sheets: int, rate: float, seed: int, outage: float, damage: int,
    partial_every: int,
) -> int:
    """Write ``sheets`` sheets round-robin into the three folders, ``rate`` per second."""
    from tests.engine_rig import readable_sheets
    from tests.quality_rig import displaced_id, rescan_of

    folders = _folders(root)
    for folder in folders.values():
        folder.mkdir(parents=True, exist_ok=True)
    data = readable_sheets(max(sheets, 48), seed=seed)[:sheets]
    names = list(folders)
    pause = 1.0 / rate if rate > 0 else 0.0
    outage_at = sheets // 3 if outage > 0 else -1
    hidden: Path | None = None
    hidden_until = 0.0
    for index, payload in enumerate(data):
        scanner = names[index % len(names)]
        folder = folders[scanner]
        if index == outage_at:
            # Scanner B disappears: its folder is moved aside (as an unplugged
            # share would vanish), then comes back with whatever it holds.
            target = folders["Scanner B"]
            hidden = target.with_name(target.name + "_offline")
            shutil.move(str(target), str(hidden))
            hidden_until = time.monotonic() + outage
            print(f"[{index}] Scanner B unreachable for {outage:.0f} s")
        if hidden is not None and time.monotonic() >= hidden_until:
            shutil.move(str(hidden), str(folders["Scanner B"]))
            hidden = None
            print(f"[{index}] Scanner B reachable again")
        if hidden is not None and scanner == "Scanner B":
            scanner, folder = "Scanner A", folders["Scanner A"]
        partial = partial_every > 0 and index % partial_every == 0
        _write(folder / f"{scanner[-1]}-{index:04d}.png", payload, partial=partial,
               step_seconds=0.4)
        if index % 25 == 0:
            print(f"[{index}] {scanner}: {folder.name}")
        time.sleep(pause)
    if hidden is not None:
        shutil.move(str(hidden), str(folders["Scanner B"]))
    for number in range(damage):
        # A displaced identifier block: the quality policy suggests a rescan.
        (folders["Scanner A"] / f"A-damaged-{number}.png").write_bytes(displaced_id(number))
    if damage:
        print(f"Wrote {damage} damaged sheet(s) at Scanner A; rescans follow at Scanner C")
        time.sleep(max(5.0, pause * 10))
        for number in range(damage):
            (folders["Scanner C"] / f"C-rescan-{number}.png").write_bytes(rescan_of(number))
    print("Feeding finished; the folders keep their files (originals are never moved by OMRFlow).")
    return 0


def main() -> int:
    """Command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    first = commands.add_parser("prepare", help="create the project and the three folders")
    first.add_argument("--root", type=Path, required=True)
    second = commands.add_parser("feed", help="write sheets into the folders over time")
    second.add_argument("--root", type=Path, required=True)
    second.add_argument("--sheets", type=int, default=150)
    second.add_argument("--rate", type=float, default=2.0, help="sheets per second")
    second.add_argument("--seed", type=int, default=42)
    second.add_argument("--outage", type=float, default=0.0,
                        help="seconds Scanner B is unreachable (0: never)")
    second.add_argument("--damage", type=int, default=0,
                        help="damaged sheets at Scanner A, rescanned later at Scanner C")
    second.add_argument("--partial-every", type=int, default=10,
                        help="every n-th file is written in three steps (0: never)")
    arguments = parser.parse_args()
    if arguments.command == "prepare":
        return prepare(arguments.root)
    return feed(
        arguments.root, sheets=arguments.sheets, rate=arguments.rate, seed=arguments.seed,
        outage=arguments.outage, damage=arguments.damage, partial_every=arguments.partial_every,
    )


if __name__ == "__main__":
    raise SystemExit(main())
