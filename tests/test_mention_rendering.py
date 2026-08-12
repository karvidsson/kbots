"""Mention rendering — raw Discord mention markup becomes readable @names
(incoming) and plain-text @names become real mention markup (outgoing)."""

from types import SimpleNamespace

from src.connectors.discord import DiscordBot, DiscordConnector


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


# === Outgoing: plain-text @Name -> real mention markup ===


def _connector():
    c = DiscordConnector.__new__(DiscordConnector)
    c._mention_cache = {}
    return c


def _guild(members=(), roles=(), searchable=()):
    async def search_members(query, limit=10):
        q = query.lower()
        return [
            m for m in searchable
            if m.display_name.lower().startswith(q) or m.name.lower().startswith(q)
        ]
    return SimpleNamespace(
        id=100, members=list(members), roles=list(roles),
        search_members=search_members,
    )


def _channel(guild):
    return SimpleNamespace(guild=guild)


async def test_outgoing_name_resolves_via_member_search():
    # Member cache is disabled in this client — resolution must work
    # through the HTTP search fallback alone.
    databot = _user(2, "Data.Bot")
    ch = _channel(_guild(searchable=[databot]))
    out = await _connector()._linkify_mentions("@Data.Bot — hand-over list", ch)
    assert out == "<@2> — hand-over list"


async def test_outgoing_two_word_display_name():
    eng = _user(3, "Engineer Bot")
    ch = _channel(_guild(searchable=[eng]))
    out = await _connector()._linkify_mentions("@Engineer Bot please deploy", ch)
    assert out == "<@3> please deploy"


async def test_outgoing_falls_back_to_first_word():
    atlas = _user(1, "Atlas")
    ch = _channel(_guild(searchable=[atlas]))
    out = await _connector()._linkify_mentions("@Atlas are you here?", ch)
    assert out == "<@1> are you here?"


async def test_outgoing_role_mention():
    role = SimpleNamespace(id=9, name="agents")
    ch = _channel(_guild(roles=[role]))
    out = await _connector()._linkify_mentions("@agents standup time", ch)
    assert out == "<@&9> standup time"


async def test_outgoing_unresolvable_stays_plain():
    ch = _channel(_guild())
    text = "@Nobody knows this name"
    assert await _connector()._linkify_mentions(text, ch) == text


async def test_outgoing_skips_code_blocks_and_specials():
    databot = _user(2, "Data.Bot")
    ch = _channel(_guild(searchable=[databot]))
    text = "@Data.Bot run `git log @Data.Bot` and ```\n@Data.Bot\n``` — @everyone stays"
    out = await _connector()._linkify_mentions(text, ch)
    assert out == "<@2> run `git log @Data.Bot` and ```\n@Data.Bot\n``` — @everyone stays"


async def test_outgoing_ignores_emails_and_existing_markup():
    databot = _user(2, "Data.Bot")
    ch = _channel(_guild(searchable=[databot]))
    text = "mail someone@example.com, ping <@555> directly"
    assert await _connector()._linkify_mentions(text, ch) == text


async def test_outgoing_no_guild_is_untouched():
    ch = SimpleNamespace()  # DM channel — no guild attribute
    text = "@Data.Bot hello"
    assert await _connector()._linkify_mentions(text, ch) == text
