r"""Stage: install, launch, uninstall - and prove user data survived.

Safety first, because this stage is the destructive one:
    * It refuses to run if OMRFlow is already installed, unless
      ``--replace-installation`` says otherwise. Somebody's working
      installation is not something a test may remove because it was
      convenient, and an operator who runs this on their own machine must not
      lose it.
    * It never deletes user data. ``%LOCALAPPDATA%\\OMRFlow`` is inspected, and
      what remains after an uninstall is *recorded*, not cleaned - the
      installer promises to leave it, and checking that promise means leaving
      it alone.
    * Everything it installs, it uninstalls, in a ``finally``.

Silent switches:
    Inno Setup's ``/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER``.
    Preferred over UI automation because they are what a managed rollout uses
    and what ``docs/wiki/Installation.md`` documents; the licence page, the
    Alpha warning and SmartScreen are not observable this way and are recorded
    as not-performed rather than assumed.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from tools.release_validation import config as cfg
from tools.release_validation.process_utils import ProcessRegistry, run_command
from tools.release_validation.results import StageResult, Status

SILENT_INSTALL = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER")
SILENT_UNINSTALL = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")


def find_installer(config: cfg.ValidationConfig) -> Path | None:
    """The installer to test: the one given, or the newest that was built."""
    if config.installer_path is not None:
        return config.installer_path if config.installer_path.is_file() else None
    if not cfg.INSTALLER_DIR.is_dir():
        return None
    candidates = sorted(
        cfg.INSTALLER_DIR.glob("*Setup*.exe"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inventory(directory: Path) -> set[str]:
    """Every file under ``directory``, relative, for a before/after comparison."""
    if not directory.is_dir():
        return set()
    return {
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file()
    }


def _start_menu_shortcuts() -> list[Path]:
    if not cfg.START_MENU_DIR.is_dir():
        return []
    return list(cfg.START_MENU_DIR.rglob("OMRFlow*.lnk"))


def run(
    config: cfg.ValidationConfig,
    registry: ProcessRegistry,
) -> StageResult:
    """Install, verify, uninstall, and report what remained."""
    started = time.monotonic()
    stage = StageResult(name="Installer qualification")

    installer = find_installer(config)
    if installer is None:
        stage.skipped_reason = (
            "no installer found. Build one with "
            ".\\scripts\\release\\Build-Installer.ps1, or pass "
            "--installer <path>."
        )
        return stage

    stage.record(
        "the installer exists",
        Status.PASS,
        detail=f"{installer.name} ({installer.stat().st_size / 1_048_576:.1f} MB)",
    )
    stage.record("installer SHA-256", Status.PASS, detail=_sha256(installer))

    # ------------------------------------------- an existing installation?
    if cfg.INSTALLED_EXE.is_file():
        if not config.allow_replace_installation:
            stage.skipped_reason = (
                f"OMRFlow is already installed at {cfg.INSTALLED_APP_DIR}. This "
                "stage will not remove somebody's working installation. Re-run "
                "with --replace-installation to allow it, or uninstall by hand "
                "first."
            )
            stage.record(
                "existing installation detected",
                Status.WARNING,
                detail=str(cfg.INSTALLED_APP_DIR),
                reason="refusing to modify it without --replace-installation",
            )
            return stage
        stage.record(
            "existing installation detected",
            Status.WARNING,
            detail=str(cfg.INSTALLED_APP_DIR),
            reason="--replace-installation was given; it will be replaced",
        )

    # User data before the run, so "the uninstall kept it" is measurable.
    user_data_before = _inventory(cfg.USER_DATA_DIR)
    stage.record(
        "user data recorded before installing",
        Status.PASS,
        detail=f"{len(user_data_before)} file(s) under {cfg.USER_DATA_DIR}",
    )

    installed = False
    try:
        # ------------------------------------------------------- install
        code, output = run_command(
            [installer, *SILENT_INSTALL],
            timeout_seconds=config.timeouts.installer_seconds,
        )
        (config.logs_dir / "installer.log").write_text(output, encoding="utf-8", errors="replace")
        stage.ok(
            "the installer completed",
            code == 0,
            detail=f"exit {code}",
            reason=f"the installer exited {code}",
        )
        installed = cfg.INSTALLED_EXE.is_file()
        stage.ok(
            "the application was installed to the per-user location",
            installed,
            detail=str(cfg.INSTALLED_APP_DIR),
            reason=f"nothing at {cfg.INSTALLED_EXE} after a successful install",
        )
        if not installed:
            return stage

        stage.ok(
            "the licence was installed beside the application",
            (cfg.INSTALLED_APP_DIR / "LICENSE.txt").is_file(),
            detail="LICENSE.txt",
            reason="LICENSE.txt is missing from the installation",
        )
        stage.ok(
            "the bundled runtime was installed",
            (cfg.INSTALLED_APP_DIR / cfg.INTERNAL_DIR_NAME).is_dir(),
            detail=cfg.INTERNAL_DIR_NAME,
            reason=f"{cfg.INTERNAL_DIR_NAME} is missing, so the application cannot run",
        )
        for label, relative in (
            ("application icon", r"_internal\omr_scanner\gui\resources\branding\icon.ico"),
            ("Qt platform plugin", r"_internal\PySide6\plugins\platforms\qwindows.dll"),
        ):
            stage.ok(
                f"installed: {label}",
                (cfg.INSTALLED_APP_DIR / relative).exists(),
                detail=relative,
                reason=f"{relative} was not installed",
            )

        shortcuts = _start_menu_shortcuts()
        stage.ok(
            "a Start menu shortcut was created",
            bool(shortcuts),
            detail=shortcuts[0].name if shortcuts else "",
            reason="the installer creates one by default and none was found",
        )
        # The desktop icon is an unticked task in omrflow.iss, so its absence
        # after a silent install is correct behaviour and is recorded as such.
        desktop = Path.home() / "Desktop" / "OMRFlow.lnk"
        stage.record(
            "desktop shortcut matches the installer's configuration",
            Status.PASS if not desktop.exists() else Status.WARNING,
            detail="absent, as the unticked 'desktopicon' task intends"
            if not desktop.exists()
            else "present although the task is unticked by default",
        )

        uninstaller = next(iter(cfg.INSTALLED_APP_DIR.glob("unins*.exe")), None)
        stage.ok(
            "an uninstaller was created",
            uninstaller is not None,
            detail=uninstaller.name if uninstaller else "",
            reason="no unins*.exe - the application cannot be removed",
        )

        # ---------------------------------- the installed app must run
        from tools.release_validation import packaged_app_tests

        launch = packaged_app_tests.run_installed(config, registry)
        stage.checks.extend(launch.checks)

        # ------------------------------------------------------ uninstall
        if uninstaller is None:
            return stage
        code, output = run_command(
            [uninstaller, *SILENT_UNINSTALL],
            timeout_seconds=config.timeouts.uninstaller_seconds,
        )
        (config.logs_dir / "uninstaller.log").write_text(
            output, encoding="utf-8", errors="replace"
        )
        stage.ok(
            "the uninstaller completed",
            code == 0,
            detail=f"exit {code}",
            reason=f"the uninstaller exited {code}",
        )

        # Inno's uninstaller returns before its last files are gone, because it
        # has to delete itself last.
        from tools.release_validation.process_utils import wait_until

        wait_until(lambda: not cfg.INSTALLED_EXE.is_file(), 90)
        installed = cfg.INSTALLED_EXE.is_file()

        stage.ok(
            "the application binary was removed",
            not cfg.INSTALLED_EXE.is_file(),
            reason=f"{cfg.INSTALLED_EXE} is still there after uninstalling",
        )
        stage.ok(
            "the bundled runtime was removed",
            not (cfg.INSTALLED_APP_DIR / cfg.INTERNAL_DIR_NAME).is_dir(),
            reason="the runtime directory survived the uninstall",
        )
        stage.ok(
            "the Start menu shortcut was removed",
            not _start_menu_shortcuts(),
            reason="a Start menu shortcut survived the uninstall",
        )

        # --------------------------------- and the user data must survive
        user_data_after = _inventory(cfg.USER_DATA_DIR)
        lost = sorted(user_data_before - user_data_after)
        stage.ok(
            "no pre-existing user file was deleted",
            not lost,
            detail=f"{len(user_data_after)} file(s) remain",
            reason=f"the uninstall removed user data it promised to keep: {lost[:5]}",
        )

        remaining = sorted(user_data_after)
        stage.record(
            "what remains after uninstalling",
            Status.PASS,
            detail=(
                f"{cfg.USER_DATA_DIR}: {len(remaining)} file(s) - "
                "settings and logs are documented as retained"
            ),
        )

        # ------------------------------------------------------ reinstall
        code, _ = run_command(
            [installer, *SILENT_INSTALL],
            timeout_seconds=config.timeouts.installer_seconds,
        )
        installed = cfg.INSTALLED_EXE.is_file()
        stage.ok(
            "the application can be reinstalled",
            code == 0 and installed,
            detail=f"exit {code}",
            reason=f"reinstalling failed (exit {code})",
        )
        if installed:
            after_reinstall = _inventory(cfg.USER_DATA_DIR)
            stage.ok(
                "retained user data is still reachable after reinstalling",
                not (set(remaining) - after_reinstall),
                detail=f"{len(after_reinstall)} file(s)",
                reason="reinstalling lost data the uninstall had kept",
            )

        # ------------------------------------------- what needs a person
        for step, what in (
            ("5", "the SmartScreen warning and its wording (a silent install cannot see it)"),
            ("6", "the licence page shows the MIT licence"),
            ("7", "the Alpha warning is shown during installation"),
        ):
            stage.skip(
                f"CLEAN_MACHINE_TEST.md step {step}",
                f"not performed: {what}. Run the installer interactively.",
            )

    finally:
        # Whatever happened above, do not leave the machine with an
        # installation this stage put there.
        if cfg.INSTALLED_EXE.is_file():
            uninstaller = next(iter(cfg.INSTALLED_APP_DIR.glob("unins*.exe")), None)
            if uninstaller is not None:
                run_command(
                    [uninstaller, *SILENT_UNINSTALL],
                    timeout_seconds=config.timeouts.uninstaller_seconds,
                )
                from tools.release_validation.process_utils import wait_until

                wait_until(lambda: not cfg.INSTALLED_EXE.is_file(), 90)
            stage.record(
                "cleanup: the test installation was removed",
                Status.PASS if not cfg.INSTALLED_EXE.is_file() else Status.WARNING,
                detail=str(cfg.INSTALLED_APP_DIR),
                reason=""
                if not cfg.INSTALLED_EXE.is_file()
                else "an installation this run created could not be removed - "
                "uninstall it by hand",
            )

    stage.duration_seconds = time.monotonic() - started
    return stage
