"""AlertSender — bot-token resolution via the connector's account map."""

from src.core.alerts import AlertSender


class _FakeVault:
    def __init__(self, entries):
        self._entries = entries

    def get(self, key):
        return self._entries.get(key)


def _config(alert_bot):
    return {
        "security": {"alert_channel": "123", "alert_bot": alert_bot},
        "connectors": {"discord": {"accounts": {
            "main": {"token_key": "discord-token"},
            "helper": {"token_key": "discord-helper"},
        }}},
    }


def test_alert_bot_resolves_via_account_map():
    # "main" must map to discord-token (NOT discord-main) — the reported bug.
    a = AlertSender(_config("main"), _FakeVault({"discord-token": "tok-main"}))
    assert a.bot_token == "tok-main"
    assert a.enabled is True


def test_alert_bot_falls_back_to_discord_name():
    # A bot not in accounts falls back to the discord-<name> convention.
    a = AlertSender(_config("custom"), _FakeVault({"discord-custom": "tok-c"}))
    assert a.bot_token == "tok-c"


def test_disabled_without_channel_or_token():
    cfg = _config("main")
    cfg["security"]["alert_channel"] = ""
    assert AlertSender(cfg, _FakeVault({"discord-token": "x"})).enabled is False
    assert AlertSender(_config("main"), _FakeVault({})).enabled is False  # no token
