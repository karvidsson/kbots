"""Mention rendering — raw Discord mention markup becomes readable @names."""

from types import SimpleNamespace

from src.connectors.discord import DiscordBot


def _bot():
    b = DiscordBot.__new__(DiscordBot)
    b.account_name = "test"
    return b


def _msg(content, mentions=(), roles=(), channels=()):
    return SimpleNamespace(
        content=content,
        mentions=list(mentions),
        role_mentions=list(roles),
        channel_mentions=list(channels),
    )


def _user(uid, name):
    return SimpleNamespace(id=uid, display_name=name, name=name)


def test_all_user_mentions_resolve_to_names():
    # The exact shape that confused Atlas: two bots addressed, own mention
    # stripped, the other left as a raw ID.
    atlas = _user(1, "Atlas")
    databot = _user(2, "Data.Bot")
    msg = _msg("<@2> and <@1> are you both here?", mentions=[databot, atlas])
    assert _bot()._render_mentions(msg) == "@Data.Bot and @Atlas are you both here?"


def test_nickname_form_resolves():
    u = _user(7, "Robin")
    msg = _msg("<@!7> ping", mentions=[u])
    assert _bot()._render_mentions(msg) == "@Robin ping"


def test_role_and_channel_mentions_resolve():
    role = SimpleNamespace(id=9, name="agents")
    chan = SimpleNamespace(id=5, name="migrate-agent")
    msg = _msg("<@&9> meet in <#5>", roles=[role], channels=[chan])
    assert _bot()._render_mentions(msg) == "@agents meet in #migrate-agent"


def test_unresolvable_mention_stays_raw():
    msg = _msg("<@999> hello")
    assert _bot()._render_mentions(msg) == "<@999> hello"


def test_mention_only_message_keeps_name():
    u = _user(1, "Atlas")
    msg = _msg("<@1>", mentions=[u])
    assert _bot()._render_mentions(msg) == "@Atlas"
