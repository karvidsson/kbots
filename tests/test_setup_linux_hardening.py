"""Regression tests for wizard input validation and Linux portability.

Found by driving ./setup end-to-end on Arch Linux with no passwordless sudo.
"""

import builtins
import importlib.util
import os
import shutil
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "setup_linux", Path(__file__).resolve().parent.parent / "setup.py")
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)


@pytest.fixture
def feed(monkeypatch):
    """Feed a queue of keystrokes to input()."""
    def _feed(*answers):
        it = iter(answers)
        monkeypatch.setattr(builtins, "input", lambda *a, **k: next(it))
    return _feed


# --- deployment pattern -----------------------------------------------------

def test_step_modules_rejects_garbage_then_accepts(feed, monkeypatch, tmp_path):
    monkeypatch.setattr(setup, "ENGINE_ROOT", tmp_path / "engine")
    state = {}
    feed("/some/path", "yes", "1")
    setup.step_modules(state)
    assert state["deployment_pattern"] == "2-layer"


def test_step_modules_blank_takes_default(feed, monkeypatch, tmp_path):
    monkeypatch.setattr(setup, "ENGINE_ROOT", tmp_path / "engine")
    state = {}
    feed("")
    setup.step_modules(state)
    assert state["deployment_pattern"] == "2-layer"


# --- owner name -------------------------------------------------------------

def test_step_team_requires_a_name(feed):
    state = {"owner_discord_id": "1000000000000000001"}
    feed("", "", "Owner Person", "", "", "")
    setup.step_team(state)
    assert state["owner"]["name"] == "Owner Person"
    assert state["owner"]["id"] == "owner-person"


# --- jq on non-Debian Linux -------------------------------------------------

def test_linux_pkg_install_cmd_picks_available_manager(monkeypatch):
    monkeypatch.setattr(shutil, "which",
                        lambda b: "/usr/bin/pacman" if b == "pacman" else None)
    assert setup._linux_pkg_install_cmd("jq") == \
        ["sudo", "pacman", "-S", "--noconfirm", "jq"]


def test_linux_pkg_install_cmd_none_when_unknown(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda b: None)
    assert setup._linux_pkg_install_cmd("jq") is None


# --- install dir ------------------------------------------------------------

def test_step_install_accepts_precreated_dir_under_readonly_parent(
        feed, monkeypatch, tmp_path):
    """`sudo mkdir /opt/kbots && sudo chown $USER /opt/kbots` must be enough."""
    parent = tmp_path / "opt"
    target = parent / "kbots"
    target.mkdir(parents=True)
    real_access = os.access

    def fake_access(path, mode):
        if Path(path) == parent and mode & os.W_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr(os, "access", fake_access)
    monkeypatch.setattr(setup, "_check_sudo", lambda: False)
    cloned = []
    monkeypatch.setattr(setup.subprocess, "run",
                        lambda cmd, **kw: cloned.append(cmd) or
                        type("R", (), {"returncode": 0})())
    monkeypatch.setattr(setup, "_track_path", lambda *a, **k: None)
    state = {}
    feed(str(target))
    setup.step_install(state)
    assert any("clone" in c for c in cloned), "should have cloned into the target"
    assert state["engine_root"] == target.resolve()
