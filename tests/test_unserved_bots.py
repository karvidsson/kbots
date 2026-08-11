"""A Discord bot account with no agent routing to it must be flagged."""

from src.main import unserved_discord_accounts


def _connectors(*accounts, enabled=True):
    return {"discord": {"enabled": enabled, "accounts": {a: {} for a in accounts}}}


def _agent(account):
    return {"routing": {"discord": {"account": account}}}


def test_all_accounts_served():
    conn = _connectors("main", "builder")
    agents = {"atlas": _agent("main"), "builder": _agent("builder")}
    assert unserved_discord_accounts(conn, agents) == set()


def test_ops_bot_left_unserved():
    # engineer bot in config, but no agent routes to it (the reported bug)
    conn = _connectors("main", "builder")
    agents = {"atlas": _agent("main")}
    assert unserved_discord_accounts(conn, agents) == {"builder"}


def test_discord_disabled_flags_nothing():
    conn = _connectors("main", "builder", enabled=False)
    assert unserved_discord_accounts(conn, {}) == set()


def test_no_accounts_uses_default():
    conn = {"discord": {"enabled": True}}  # single-bot mode → 'default'
    assert unserved_discord_accounts(conn, {}) == {"default"}
    served = {"a": _agent("default")}
    assert unserved_discord_accounts(conn, served) == set()
