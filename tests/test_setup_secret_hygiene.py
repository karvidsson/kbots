"""Secret-hygiene regression tests for the setup wizard.

Covers the guarantees PR fix/setup-secret-hygiene introduced: sensitive files
are 0600 from their first byte, token entry never echoes, the overwrite guard
is keyed on the file it guards, and a rejected token is distinguishable from
an unreachable Discord.
"""

import builtins
import importlib.util
import io
import json
import stat
import urllib.error
from pathlib import Path

import pytest

from src.core.base import write_private_file

_spec = importlib.util.spec_from_file_location(
    "setup_hygiene", Path(__file__).resolve().parent.parent / "setup.py")
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)


@pytest.fixture
def feed(monkeypatch):
    def _feed(*answers):
        it = iter(answers)
        monkeypatch.setattr(builtins, "input", lambda *a, **k: next(it))
    return _feed


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- write_private_file ---

def test_write_private_file_creates_0600(tmp_path):
    target = tmp_path / "keyfile"
    write_private_file(target, "hunter2-but-long\n")
    assert target.read_text() == "hunter2-but-long\n"
    assert _mode(target) == 0o600


def test_write_private_file_creates_private_parent(tmp_path):
    target = tmp_path / "sub" / "keyfile"
    write_private_file(target, "s")
    assert _mode(target.parent) == 0o700


def test_write_private_file_repairs_loose_existing(tmp_path):
    target = tmp_path / "keyfile"
    target.write_text("old")
    target.chmod(0o644)
    write_private_file(target, "new")
    assert target.read_text() == "new"
    assert _mode(target) == 0o600


# --- wizard-written configs ---

def test_write_yaml_is_owner_only(tmp_path):
    p = tmp_path / "config.yaml"
    setup._write_yaml(p, {"a": 1}, {})
    assert _mode(p) == 0o600


def test_write_json_is_owner_only(tmp_path):
    p = tmp_path / "team.json"
    setup._write_json(p, {"a": 1}, {})
    assert _mode(p) == 0o600


# --- overwrite guard ---

def test_overwrite_prompts_even_without_preexisting_vault(tmp_path, feed):
    """The guard used to key on state['vault_existed'], so a re-run after the
    vault was deleted clobbered config.yaml silently."""
    p = tmp_path / "config.yaml"
    p.write_text("original: true\n")
    feed("n")  # decline the overwrite
    setup._write_yaml(p, {"new": True}, {})  # note: no vault_existed in state
    assert p.read_text() == "original: true\n"


def test_overwrite_accepted_replaces_file(tmp_path, feed):
    p = tmp_path / "team.json"
    p.write_text("{}")
    feed("y")
    setup._write_json(p, {"new": True}, {})
    assert json.loads(p.read_text()) == {"new": True}


# --- passphrase policy ---

def test_min_passphrase_length_is_meaningful():
    assert setup.MIN_PASSPHRASE_LEN >= 12


# --- token entry never echoes ---

def test_ask_discord_token_uses_getpass(monkeypatch):
    seen = {}

    def fake_getpass(prompt=""):
        seen["prompt"] = prompt
        return "tok"

    monkeypatch.setattr(setup.getpass, "getpass", fake_getpass)
    monkeypatch.setattr(
        builtins, "input",
        lambda *a, **k: pytest.fail("token prompt fell back to echoing input()"))
    assert setup.ask_discord_token("Paste your bot token") == "tok"
    assert "hidden" in seen["prompt"]


# --- validate_discord_token failure reasons ---

def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "msg", hdrs=None, fp=io.BytesIO(b""))


def test_validate_token_distinguishes_rejection_from_network(monkeypatch):
    monkeypatch.setattr(
        setup.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(_http_error(401)))
    assert setup.validate_discord_token("bad") == (None, "invalid")

    monkeypatch.setattr(
        setup.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down")))
    assert setup.validate_discord_token("any") == (None, "network")
