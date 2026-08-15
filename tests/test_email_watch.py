"""Email watcher — history parsing, delivery, opt-in filtering, state."""

import asyncio
import json

import pytest

from src.auth.oauth2 import OAuth2AuthRevokedError
from src.core.email_watch import EmailWatcher


class FakeManager:
    def __init__(self, configs):
        self.agent_configs = configs
        self.handled = []

    async def handle_message(self, agent_id, msg):
        self.handled.append((agent_id, msg))


def _watcher(tmp_path, configs):
    return EmailWatcher(FakeManager(configs), vault=None, data_dir=str(tmp_path))


def test_watched_agents_requires_enabled_and_channel(tmp_path):
    w = _watcher(tmp_path, {
        "a": {"email_watch": {"enabled": True, "channel": "1"}},
        "b": {"email_watch": {"enabled": True}},                # no channel
        "c": {"email_watch": {"enabled": False, "channel": "2"}},
        "d": {},
    })
    assert set(w.watched_agents) == {"a"}


async def test_new_inbox_messages_parses_and_filters(tmp_path, monkeypatch):
    w = _watcher(tmp_path, {})

    async def fake_api(account, url):
        assert "startHistoryId=100" in url
        return {"historyId": "150", "history": [
            {"messagesAdded": [
                {"message": {"id": "m1", "labelIds": ["INBOX", "UNREAD"]}},
                {"message": {"id": "m2", "labelIds": ["SENT"]}},       # not inbox
                {"message": {"id": "m1", "labelIds": ["INBOX"]}},      # dup
            ]},
        ]}

    monkeypatch.setattr(w, "_api_get", fake_api)
    ids, new_hid = await w._new_inbox_messages("acct", "100")
    assert ids == ["m1"] and new_hid == "150"


async def test_expired_history_returns_reset_signal(tmp_path, monkeypatch):
    w = _watcher(tmp_path, {})

    async def fake_api(account, url):
        return {"error": True, "status": 404, "detail": "expired"}

    monkeypatch.setattr(w, "_api_get", fake_api)
    ids, new_hid = await w._new_inbox_messages("acct", "100")
    assert ids == [] and new_hid == ""


async def test_fire_delivers_to_agent_channel(tmp_path):
    cfg = {"email_watch": {"enabled": True, "channel": "999",
                           "connector": "discord"},
           "routing": {"discord": {"account": "pixel-fox"}}}
    w = _watcher(tmp_path, {"husky": cfg})
    await w._fire("husky", cfg["email_watch"],
                  '- from Curator <c@example.com> — "Re: Submission" (id m1)', 1)
    await asyncio.sleep(0)  # let the created task run
    mgr = w.agent_manager
    assert len(mgr.handled) == 1
    agent_id, msg = mgr.handled[0]
    assert agent_id == "husky"
    assert msg.channel_id == "999"
    assert msg.bot_account == "pixel-fox"
    assert "1 new email" in msg.content and "Re: Submission" in msg.content


def test_state_round_trips(tmp_path):
    w = _watcher(tmp_path, {})
    w._save_state({"husky": "12345"})
    assert w._load_state() == {"husky": "12345"}
    assert json.loads((tmp_path / "email_watch.json").read_text()) == {"husky": "12345"}


class OverrideStorage:
    def __init__(self, value=None):
        self.value = value

    async def get_agent_override(self, agent_id, setting):
        assert setting == "email_watch_interval"
        return self.value


async def test_interval_override_wins_and_clamps(tmp_path):
    cfg = {"interval": 60}
    w = _watcher(tmp_path, {})
    w.agent_manager.storage = OverrideStorage("30")
    assert await w._effective_interval("husky", cfg) == 30
    w.agent_manager.storage = OverrideStorage("5")     # below floor
    assert await w._effective_interval("husky", cfg) == 15
    w.agent_manager.storage = OverrideStorage(None)    # no override
    assert await w._effective_interval("husky", cfg) == 60
    w.agent_manager.storage = OverrideStorage("junk")  # bad value ignored
    assert await w._effective_interval("husky", cfg) == 60


async def test_interval_without_storage_uses_config(tmp_path):
    w = _watcher(tmp_path, {})
    assert await w._effective_interval("husky", {"interval": 120}) == 120
    assert await w._effective_interval("husky", {}) == 60


# --- a revoked grant must stop the loop, not slow it down --------------------

async def test_revoked_auth_stops_watching_and_names_the_fix(tmp_path, monkeypatch, caplog):
    """A dead grant produced 839 identical ERROR lines over three days while the
    agent silently stopped being woken by mail. It must stop and say so."""
    w = _watcher(tmp_path, {})
    calls = []

    async def dead_baseline(account):
        calls.append(account)
        raise OAuth2AuthRevokedError("GOOGLE[husky]", "invalid_grant",
                                "Token has been expired or revoked.")

    monkeypatch.setattr(w, "_baseline", dead_baseline)
    slept = []

    async def no_sleep(seconds):
        slept.append(seconds)
        raise AssertionError("the loop slept instead of giving up")

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    # Returns rather than looping: without the fix this never terminates.
    await asyncio.wait_for(
        w._watch_agent("husky", {"google_account": "husky", "channel": "1"}), timeout=5)

    assert len(calls) == 1, "retried a credential that cannot recover"
    assert slept == []
    text = caplog.text
    assert "STOPPED watching husky" in text
    assert "google-reauth.py --account husky" in text, "the fix must be in the message"


async def test_transient_failure_keeps_retrying(tmp_path, monkeypatch):
    """The counterpart: an ordinary error must NOT stop the watcher, or a Gmail
    blip would silently disable mail for good."""
    w = _watcher(tmp_path, {})
    calls = []

    async def flaky_baseline(account):
        calls.append(account)
        raise RuntimeError("Gmail 503")

    monkeypatch.setattr(w, "_baseline", flaky_baseline)

    async def stop_after_three(seconds):
        if len(calls) >= 3:
            raise asyncio.CancelledError
    monkeypatch.setattr(asyncio, "sleep", stop_after_three)

    with pytest.raises(asyncio.CancelledError):
        await w._watch_agent("husky", {"google_account": "husky", "channel": "1"})
    assert len(calls) == 3
