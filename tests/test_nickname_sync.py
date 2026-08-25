"""Deterministic guild-nickname alignment — mechanical identity is engine
work, not an agent turn. The account username may be rate-limited (2/hour);
a per-guild nickname is always available with Change Nickname."""

from types import SimpleNamespace

from src.connectors.discord import DiscordBot


def _bot(name="Atlas"):
    b = DiscordBot.__new__(DiscordBot)
    b.account_name = "test"
    b._self_mention_name = name
    return b


class _Me:
    def __init__(self, name="APP", nick=None):
        self.name = name
        self.nick = nick
        self.edits = []

    async def edit(self, nick=None):
        self.edits.append(nick)


def _guild(me):
    return SimpleNamespace(id=42, name="P2", me=me)


async def test_mismatched_nickname_is_set():
    me = _Me(name="APP-Two", nick=None)
    await _bot()._sync_nickname(_guild(me))
    assert me.edits == ["Atlas"]


async def test_matching_account_name_needs_no_nick():
    me = _Me(name="Atlas", nick=None)
    await _bot()._sync_nickname(_guild(me))
    assert me.edits == []


async def test_matching_nick_is_left_alone():
    me = _Me(name="APP-Two", nick="Atlas")
    await _bot()._sync_nickname(_guild(me))
    assert me.edits == []


async def test_missing_member_or_name_is_a_noop():
    await _bot()._sync_nickname(SimpleNamespace(id=42, name="P2"))  # no .me
    me = _Me()
    await _bot(name="")._sync_nickname(_guild(me))  # on_ready not run yet
    assert me.edits == []


async def test_api_refusal_never_raises():
    class _Refusing(_Me):
        async def edit(self, nick=None):
            raise RuntimeError("403 Missing Permissions")

    await _bot()._sync_nickname(_guild(_Refusing()))
