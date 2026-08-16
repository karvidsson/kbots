"""Routing filters — in particular the per-agent `users` sender allowlist."""

from src.core.base import IncomingMessage
from src.core.router import Router


class _StubAgentManager:
    def __init__(self, agent_configs):
        self.agent_configs = agent_configs


def _router(routing: dict) -> Router:
    return Router(_StubAgentManager({"a1": {"routing": {"discord": routing}}}))


def _msg(user_id: str = "111", channel_id: str = "c1") -> IncomingMessage:
    return IncomingMessage(
        connector="discord", channel_id=channel_id,
        user_id=user_id, user_name="tester", content="hi",
    )


def test_no_users_filter_routes_everyone():
    r = _router({"channels": []})
    assert r._find_agents(_msg(user_id="anyone")) == ["a1"]


def test_users_filter_allows_listed_sender():
    r = _router({"channels": [], "users": ["111", "222"]})
    assert r._find_agents(_msg(user_id="111")) == ["a1"]


def test_users_filter_blocks_unlisted_sender():
    r = _router({"channels": [], "users": ["111"]})
    assert r._find_agents(_msg(user_id="999")) == []


def test_users_filter_coerces_int_ids():
    """YAML users may parse as ints; message user_ids are strings."""
    r = _router({"channels": [], "users": [111]})
    assert r._find_agents(_msg(user_id="111")) == ["a1"]


def test_users_filter_blocks_empty_sender_id():
    """A listed agent must not answer system posts that carry no sender ID."""
    r = _router({"channels": [], "users": ["111"]})
    assert r._find_agents(_msg(user_id="")) == []


def test_empty_users_list_means_no_filter():
    r = _router({"channels": [], "users": []})
    assert r._find_agents(_msg(user_id="999")) == ["a1"]
