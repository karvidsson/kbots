"""First-install correctness tests for the setup wizard.

The bugs here shared a shape: a fresh install behaved differently from a
re-run, because the wizard configured things for FUTURE shells while its own
process (and the very first boot) never saw them.
"""

import builtins
import importlib.util
import os
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "setup_first_run", Path(__file__).resolve().parent.parent / "setup.py")
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)


@pytest.fixture
def feed(monkeypatch):
    def _feed(*answers):
        it = iter(answers)
        monkeypatch.setattr(builtins, "input", lambda *a, **k: next(it))
    return _feed


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Sandbox Path.home() so step_overlay's shell-profile write stays local."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    return fake_home


def _run_step_overlay(tmp_path, feed):
    overlay = tmp_path / "overlay"
    feed(str(overlay))
    state = {}
    setup.step_overlay(state)
    return overlay, state


def test_step_overlay_exports_env_into_own_process(tmp_path, home, feed, monkeypatch):
    """scaffold_agent (a later step, same process) resolves KBOTS_OVERLAY at
    call time — the shell-profile export alone left it unset, and every agent
    sandbox rule pointed at /tmp instead of <overlay>/tmp until setup was
    re-run from a fresh shell."""
    monkeypatch.delenv("KBOTS_OVERLAY", raising=False)
    overlay, _ = _run_step_overlay(tmp_path, feed)
    assert os.environ["KBOTS_OVERLAY"] == str(overlay)


def test_overlay_gitignore_covers_all_vault_files(tmp_path, home, feed, monkeypatch):
    monkeypatch.delenv("KBOTS_OVERLAY", raising=False)
    overlay, _ = _run_step_overlay(tmp_path, feed)
    content = (overlay / ".gitignore").read_text()
    for name in ("secrets.enc", "secrets.salt", "secrets.kdf"):
        assert f"config/{name}" in content, name


def test_step_modules_exports_env(monkeypatch, tmp_path, feed):
    monkeypatch.delenv("KBOTS_MODULES", raising=False)
    mod = tmp_path / "modules" / "acme"
    (mod / "tools").mkdir(parents=True)
    (mod / "tools" / "frob.py").write_text("")
    # ENGINE_ROOT.parent/modules is the auto-scanned location, so the wizard
    # finds the module itself: pattern 2 (3-layer) → select module 1.
    monkeypatch.setattr(setup, "ENGINE_ROOT", tmp_path / "engine")
    feed("2", "1")
    state = {}
    setup.step_modules(state)
    assert os.environ["KBOTS_MODULES"] == str(mod)


def test_fresh_vault_rollback_tracks_kdf_sidecar(tmp_path, monkeypatch):
    """An orphaned secrets.kdf from an aborted run would be read as
    authoritative iteration metadata for the NEXT vault."""
    overlay = tmp_path / "overlay"
    (overlay / "config").mkdir(parents=True)
    monkeypatch.setenv("KBOTS_VAULT_KEY_FILE", str(tmp_path / "vault-key"))
    monkeypatch.setattr(
        setup.getpass, "getpass", lambda *a, **k: "a-long-enough-passphrase")
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "")
    state = {"overlay": overlay}
    setup.step_vault(state)
    tracked = state["_created_paths"]
    assert overlay / "config" / "secrets.kdf" in tracked


def test_load_config_warns_when_nothing_found(tmp_path, monkeypatch, caplog):
    from src import main as kbots_main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KBOTS_OVERLAY", raising=False)
    monkeypatch.delenv("KBOTS_MODULES", raising=False)
    with caplog.at_level("WARNING"):
        cfg = kbots_main.load_config()
    assert cfg == {"agents": {}}
    assert any("No config.yaml found" in r.message for r in caplog.records)
