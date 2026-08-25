"""Tests for setup UX polish: live step numbering, legacy-vault rekey offer,
atomic vault persistence, and the optional-extras step."""

import builtins
import importlib.util
import json
import os
from pathlib import Path

import pytest

from src.vault.fernet import PBKDF2_ITERATIONS, FernetVault

_spec = importlib.util.spec_from_file_location(
    "setup_ux_polish", Path(__file__).resolve().parent.parent / "setup.py")
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)


@pytest.fixture
def feed(monkeypatch):
    def _feed(*answers):
        it = iter(answers)
        monkeypatch.setattr(builtins, "input", lambda *a, **k: next(it))
    return _feed


def _legacy_vault(tmp_path: Path) -> FernetVault:
    """A vault whose .kdf sidecar claims the legacy iteration count."""
    path = tmp_path / "secrets.enc"
    v = FernetVault(str(path))
    v.unlock("a-long-enough-passphrase")
    v.set("k", "v")  # persists → writes current .kdf
    path.with_suffix(".kdf").write_text(json.dumps({"iterations": 100_000}))
    return FernetVault(str(path))


# --- kdf_current / rekey offer ---

def test_kdf_current_false_for_legacy_iterations(tmp_path):
    v = _legacy_vault(tmp_path)
    assert v.kdf_current() is False


def test_kdf_current_true_after_persist(tmp_path):
    v = FernetVault(str(tmp_path / "secrets.enc"))
    v.unlock("a-long-enough-passphrase")
    v.set("k", "v")
    assert v.kdf_current() is True


def test_offer_kdf_upgrade_rekeys_on_yes(tmp_path, feed):
    # The .kdf write above rekeyed with 100k-claimed metadata, so re-derive:
    # unlock still succeeds only if the sidecar matches how it was encrypted.
    # Encrypt properly at legacy parameters first.
    path = tmp_path / "secrets.enc"
    v = FernetVault(str(path))
    path.with_suffix(".kdf").write_text(json.dumps({"iterations": 100_000}))
    v.unlock("a-long-enough-passphrase")
    v.set("k", "v")
    assert v.kdf_current() is False

    feed("y")
    setup._offer_kdf_upgrade(v, "a-long-enough-passphrase")
    assert v.kdf_current() is True
    # And the data survived the re-encryption.
    v2 = FernetVault(str(path))
    v2.unlock("a-long-enough-passphrase")
    assert v2.get("k") == "v"


def test_offer_kdf_upgrade_noop_when_current(tmp_path, monkeypatch):
    v = FernetVault(str(tmp_path / "secrets.enc"))
    v.unlock("a-long-enough-passphrase")
    v.set("k", "v")
    monkeypatch.setattr(
        builtins, "input",
        lambda *a, **k: pytest.fail("current vault must not prompt"))
    setup._offer_kdf_upgrade(v, "a-long-enough-passphrase")


# --- atomic persist ---

def test_persist_leaves_no_temp_file(tmp_path):
    path = tmp_path / "secrets.enc"
    v = FernetVault(str(path))
    v.unlock("a-long-enough-passphrase")
    v.set("k", "v")
    assert path.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_persist_failure_preserves_old_vault(tmp_path, monkeypatch):
    """A crash mid-write must not destroy the deployment's secrets."""
    path = tmp_path / "secrets.enc"
    v = FernetVault(str(path))
    v.unlock("a-long-enough-passphrase")
    v.set("k", "v1")

    monkeypatch.setattr(os, "replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        v.set("k", "v2")

    monkeypatch.undo()
    v2 = FernetVault(str(path))
    v2.unlock("a-long-enough-passphrase")
    assert v2.get("k") == "v1"


# --- live step numbering ---

def test_header_renumbers_steps_live(capsys):
    old = dict(setup._PROGRESS)
    try:
        setup._PROGRESS.update(current=3, total=21)
        setup.header("Step 12b: Local Models")
        out = capsys.readouterr().out
        assert "Step 3/21: Local Models" in out
        assert "12b" not in out
    finally:
        setup._PROGRESS.update(old)


def test_header_untouched_outside_step_list(capsys):
    old = dict(setup._PROGRESS)
    try:
        setup._PROGRESS.update(current=0, total=0)
        setup.header("Step 6: Vault Setup")
        assert "Step 6: Vault Setup" in capsys.readouterr().out
    finally:
        setup._PROGRESS.update(old)


# --- optional extras step ---

def test_step_pyextras_writes_selection(tmp_path, feed):
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    feed("1,3")
    setup.step_pyextras({"overlay": overlay})
    names = (overlay / "extras").read_text().split()
    assert names == [setup._PY_EXTRAS[0][0], setup._PY_EXTRAS[2][0]]


def test_step_pyextras_blank_keeps_existing(tmp_path, feed):
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "extras").write_text("data\n")
    feed("")
    setup.step_pyextras({"overlay": overlay})
    assert (overlay / "extras").read_text() == "data\n"


def test_py_extras_match_pyproject():
    """Every offered extra must actually exist in pyproject.toml."""
    import tomllib
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["optional-dependencies"]
    for name, _ in setup._PY_EXTRAS:
        assert name in declared, name


def test_min_iterations_constant_sane():
    assert PBKDF2_ITERATIONS >= 600_000
