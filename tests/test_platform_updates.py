"""Platform updates: where the version is read from, and where the notice goes.

`platform_version` reported the commit from a stale copy and answered "an update
is on disk but NOT running yet" about a deploy that had taken. The one check you
would run to confirm a deploy was the one that could not see it.
"""

import asyncio
import json

import pytest

from src.core import alerts, runtime_state, version


@pytest.fixture(autouse=True)
def _unpin():
    version.set_data_dir(None)
    yield
    version.set_data_dir(None)


# --- where version.json is read from ---------------------------------------

def test_writer_and_reader_agree_when_the_dir_is_pinned(tmp_path):
    version.write_running_version(tmp_path)
    version.set_data_dir(tmp_path)

    running = version.read_running_version()
    assert running and running["commit"]
    assert (tmp_path / "version.json").exists()


def test_an_overlay_copy_beats_the_repo_default(tmp_path, monkeypatch):
    """The engine writes the configured data_dir, which on an overlay install is
    the overlay's data/. A reader that defaults to PROJECT_ROOT/data reads
    whatever was last written there, which can be months old."""
    overlay = tmp_path / "overlay"
    (overlay / "data").mkdir(parents=True)
    (overlay / "data" / "version.json").write_text(json.dumps(
        {"commit": "abc123", "short": "abc123", "version": "v9.9.9"}))
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))

    assert version.read_running_version()["short"] == "abc123"


def test_the_repo_default_still_applies_with_no_overlay(tmp_path, monkeypatch):
    monkeypatch.delenv("KBOTS_OVERLAY", raising=False)
    monkeypatch.setattr(version, "PROJECT_ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "version.json").write_text(json.dumps({"short": "local"}))

    assert version.read_running_version()["short"] == "local"


def test_an_explicit_dir_still_wins_over_the_pin(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "version.json").write_text(json.dumps({"short": "explicit"}))
    version.set_data_dir(tmp_path)

    assert version.read_running_version(other)["short"] == "explicit"


# --- the alert channel is resolvable live ----------------------------------

def test_alert_channel_follows_a_runtime_override(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    sender = alerts.AlertSender({"security": {"alert_channel": "111"}})
    assert sender.channel_id == "111"

    runtime_state.set_flag("alert_channel", "222")
    assert sender.channel_id == "222", "resolved once at boot, so setup could not wire it"


def test_alert_sender_is_disabled_without_a_token(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    sender = alerts.AlertSender({"security": {"alert_channel": "111"}})
    assert sender.enabled is False


# --- where the platform-update notice is posted ----------------------------

class _Vault:
    def get(self, key):
        return "tok" if key == "discord-main" else None


def _sender(tmp_path, monkeypatch, sent):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))

    async def _send(message, channel_id, token):
        sent.append((channel_id, message))
        return True

    monkeypatch.setattr(alerts, "send_alert", _send)
    return alerts.AlertSender(
        {"security": {"alert_channel": "111", "alert_bot": "main"}}, _Vault())


async def _drain():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_the_notice_goes_to_its_own_channel_when_one_is_wired(tmp_path, monkeypatch):
    sent = []
    _sender(tmp_path, monkeypatch, sent).post_bg("999", "updated")
    await _drain()

    assert sent == [("999", "updated")]


async def test_the_notice_falls_back_to_the_alert_channel(tmp_path, monkeypatch):
    """An install that never ran server setup has no updates channel. Losing
    the notice entirely would be worse than sharing the alert channel, which is
    where it has always gone."""
    sent = []
    _sender(tmp_path, monkeypatch, sent).post_bg("", "updated")
    await _drain()

    assert sent == [("111", "updated")]


async def test_a_wired_updates_channel_does_not_move_security_alerts(tmp_path, monkeypatch):
    sent = []
    sender = _sender(tmp_path, monkeypatch, sent)
    sender.post_bg("999", "updated")
    sender.send_bg("injection detected")
    await _drain()

    assert sent == [("999", "updated"), ("111", "injection detected")]


async def test_post_bg_without_a_token_does_not_raise(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))

    async def _send(message, channel_id, token):
        sent.append((channel_id, message))
        return True

    monkeypatch.setattr(alerts, "send_alert", _send)
    alerts.AlertSender({"security": {"alert_channel": "111"}}).post_bg("999", "x")
    await _drain()

    assert sent == []
