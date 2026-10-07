"""The release-validation run configuration hands launched applications absolute paths."""

from __future__ import annotations

from pathlib import Path

from tools.release_validation import config as cfg


def test_a_relative_results_directory_becomes_absolute(tmp_path, monkeypatch):
    """Found in revised phase 10: a relative --results-dir put the app's log elsewhere.

    The workspace's log and configuration directories are passed to the
    launched application, which runs in the workspace and would resolve a
    relative path against it.
    """
    monkeypatch.chdir(tmp_path)
    run = cfg.new_run(results_root=Path("results"))
    assert run.output_dir.is_absolute() and run.workspace.is_absolute()
    assert run.output_dir.parent == tmp_path / "results"
    environment = run.child_environment()
    assert Path(environment[cfg.ENV_LOG_DIR]).is_absolute()
    assert Path(environment[cfg.ENV_CONFIG_DIR]).is_absolute()


def test_an_explicit_workspace_is_made_absolute_too(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run = cfg.new_run(results_root=tmp_path / "r", workspace_root=Path("ws"))
    assert run.workspace == tmp_path / "ws"
