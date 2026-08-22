"""Shortened replies: cut at a boundary, say so, and give the rest back.

Measured across 1136 turns in turns.jsonl: median agent reply 1918 characters,
46% over 2000. The reply contract shipped on 2026-08-21 asking agents to be
brief; over the 11 turns after it the median moved to 1423 and the p90 barely
at all. This is the version that does not depend on the model agreeing.

The properties that matter are all about not lying to the reader: the cut is
announced, the amount held back is stated, and the rest is retrievable two
ways. A cut that is silent, or a "rest" that has gone missing by the time
somebody taps for it, is worse than a long message.
"""

import pytest

from src.core.reply_shorten import (
    MIN_TAIL,
    OverflowStore,
    ReplyShortener,
    footer,
    split_reply,
    wants_more,
)

LONG = (
    "The deploy landed clean and the service is healthy on 15ea9b4.\n\n"
    "## What ran\n\n"
    "933 tests passed, ruff clean, and the MCP probe registered 169 tools "
    "with no failures. The gate is ruff plus the full pytest suite.\n\n"
    "## What is still open\n\n"
    "The version banner reads a stale file, so it will tell you a deploy has "
    "not taken when it has. There is also an unmerged PR.\n\n"
    "## What I did not do\n\n"
    "I did not touch the tier model, because that needs asking first."
)


def test_the_conclusion_survives_the_cut():
    head, rest = split_reply(LONG, threshold=300)
    assert head.startswith("The deploy landed clean")
    assert "## What is still open" in rest


def test_a_short_reply_is_left_alone():
    assert split_reply("Done. 933 tests pass.", threshold=700) is None


def test_the_cut_lands_on_a_section_boundary_not_mid_sentence():
    head, rest = split_reply(LONG, threshold=300)
    assert not head.endswith(("the", "and", "a")), f"cut mid-sentence: {head[-40:]!r}"
    assert rest.startswith("#") or rest.startswith("**")


def test_the_last_boundary_under_the_threshold_wins():
    """The head should be as complete as it can be, not as short as possible.

    Cutting at the first boundary would post one sentence and hide four
    paragraphs, which is the behaviour that makes people stop reading the first
    message at all.
    """
    head, _ = split_reply(LONG, threshold=400)
    assert "## What ran" in head, "cut earlier than it needed to"


def test_a_reply_with_no_boundary_is_sent_whole():
    """A hard cut mid-paragraph is worse than a long message. The agent wrote
    an unstructured wall, and it wears it rather than the reader getting a
    fragment that ends mid-thought.
    """
    wall = "word " * 400
    assert split_reply(wall, threshold=700) is None


def test_the_cut_never_lands_inside_a_code_fence():
    """Half a fenced block renders as broken markdown, which reads as a bug
    rather than as a shortened message.
    """
    content = ("Intro paragraph about the run, written long enough to clear "
               "the minimum head on its own, because a cut before it would "
               "not be taken at all and this test would then pass for a "
               "reason that has nothing to do with code fences.\n\n"
               "```\n" + "log line\n" * 8 + "```\n\n"
               "## After\n\n" + "tail text. " * 30)
    result = split_reply(content, threshold=400)
    assert result is not None
    head, rest = result
    assert head.count("```") % 2 == 0, "cut inside a code fence"


def test_a_scrap_is_not_worth_hiding():
    """Holding back two sentences buys nothing and costs a round trip."""
    content = "A paragraph that is quite long indeed. " * 20 + "\n\nShort tail."
    result = split_reply(content, threshold=700)
    assert result is None or len(result[1]) >= MIN_TAIL


def test_a_head_never_ends_on_a_bare_heading():
    """It reads as a message truncated by accident rather than shortened on
    purpose, and the heading's own section is the thing that was cut.
    """
    for threshold in range(120, 460, 10):
        result = split_reply(LONG, threshold=threshold)
        if result:
            assert not result[0].rstrip().split("\n")[-1].startswith("#"), \
                f"threshold {threshold} cut straight after a heading"


def test_a_threshold_with_no_boundary_near_it_sends_whole():
    """A tight threshold must not produce a cut in a worse place; it produces
    no cut. The only candidate under 150 leaves the head ending on a bare
    heading, which promises a section and delivers nothing.
    """
    assert split_reply(LONG, threshold=150) is None


# --- the footer, which is the whole affordance ---

def test_the_footer_says_how_much_is_held_back():
    """Without a number the reader cannot judge whether expanding is worth it."""
    line = footer("x" * 1840)
    assert "1,840" in line


def test_the_footer_names_both_ways_to_expand():
    """A cut with no marker is a feature that never announces itself. The
    reaction is the one-tap path; "more" is the fallback for a client where
    reacting is awkward.
    """
    line = footer("## a\n\ncontent")
    assert "🔍" in line and '"more"' in line


def test_the_footer_counts_sections():
    assert "2 sections" in footer("## one\n\ntext\n\n## two\n\ntext")
    assert "1 section" in footer("## one\n\ntext")


def test_the_head_carries_the_footer():
    shortener = ReplyShortener({"shorten": {"enabled": True, "threshold_chars": 300}})
    head, rest = shortener.shorten(LONG)
    assert "shortened" in head
    assert "shortened" not in rest, "the footer must not be repeated in the rest"


# --- the toggle ---

def test_shortening_is_off_unless_configured():
    assert ReplyShortener({}).shorten(LONG) is None
    assert ReplyShortener({"shorten": {"enabled": False}}).shorten(LONG) is None


def test_the_threshold_is_configurable():
    assert ReplyShortener(
        {"shorten": {"enabled": True, "threshold_chars": 100000}}).shorten(LONG) is None
    assert ReplyShortener(
        {"shorten": {"enabled": True, "threshold_chars": 300}}).shorten(LONG) is not None


def test_the_emoji_is_configurable():
    s = ReplyShortener({"shorten": {"enabled": True, "emoji": "📖"}})
    assert s.emoji == "📖"


def test_the_setting_is_reachable_from_the_settings_manager():
    """A feature the owner asked to be able to toggle, that is only togglable by
    hand-editing YAML, is not the feature they asked for.
    """
    from pathlib import Path

    from src.core.base import PROJECT_ROOT
    settings = Path(PROJECT_ROOT / "scripts" / "settings.py").read_text()
    assert "def edit_reply(" in settings
    assert '"Reply Length"' in settings


def test_the_config_keys_are_documented():
    from pathlib import Path

    import yaml

    from src.core.base import PROJECT_ROOT
    cfg = yaml.safe_load(
        Path(PROJECT_ROOT / "config" / "config.yaml.example").read_text())
    shorten = cfg["defaults"]["reply"]["shorten"]
    assert shorten["enabled"] is False, "must ship off"
    assert {"threshold_chars", "emoji", "ttl_hours"} <= set(shorten)


# --- getting the rest back ---

@pytest.fixture
def store(tmp_path):
    return OverflowStore(tmp_path / "overflow")


def test_the_rest_can_be_taken_by_message_id(store):
    store.put("123", "the rest of it")
    assert store.take("123") == "the rest of it"


def test_the_rest_survives_a_restart(tmp_path):
    """On disk, not in memory. This deployment redeploys several times a day,
    and a restart between the short message and the tap on the reaction would
    otherwise strand the rest with no way to ask for it again.
    """
    OverflowStore(tmp_path / "overflow").put("123", "the rest of it")
    assert OverflowStore(tmp_path / "overflow").take("123") == "the rest of it"


def test_taking_the_rest_twice_gives_nothing_the_second_time(store):
    store.put("123", "the rest")
    store.take("123")
    assert store.take("123") is None


def test_more_works_without_pointing_at_a_message(store):
    """Typing "more" has no message id attached, so the channel keeps a pointer
    to its most recent shortened reply.
    """
    store.put("123", "the rest", channel_id="chan")
    assert store.take_latest_for_channel("chan") == "the rest"
    assert store.take_latest_for_channel("chan") is None


def test_a_channel_with_nothing_held_back_returns_nothing(store):
    assert store.take_latest_for_channel("chan") is None
    assert store.take("nope") is None


def test_a_hostile_message_id_cannot_escape_the_store(store, tmp_path):
    """The id is interpolated into a filename. It arrives from Discord, so it
    is not attacker-controlled today, but a path traversal here would write
    outside the data dir and that is not a property to leave to luck.
    """
    store.put("../../etc/passwd", "payload")
    assert not (tmp_path / "etc").exists()
    assert list((tmp_path / "overflow").glob("*.md"))


def test_old_remainders_are_dropped(tmp_path):
    import os
    import time
    store = OverflowStore(tmp_path / "overflow", ttl_hours=1)
    store.put("old", "stale text")
    path = (tmp_path / "overflow" / "old.md")
    os.utime(path, (time.time() - 7200, time.time() - 7200))
    store.put("new", "fresh text")     # any write prunes
    assert store.take("old") is None
    assert store.take("new") == "fresh text"


def test_the_store_is_bounded(tmp_path):
    store = OverflowStore(tmp_path / "overflow", max_files=5)
    for i in range(20):
        store.put(str(i), f"rest {i}")
    assert len(list((tmp_path / "overflow").glob("*.md"))) <= 5


# --- asking for it in words ---

@pytest.mark.parametrize("text", ["more", "More", "  more  ", "go on", "the rest",
                                  "expand", "elaborate", "more please", "more?"])
def test_these_ask_for_the_rest(text):
    assert wants_more(text)


@pytest.mark.parametrize("text", [
    "can you tell me more about the deploy?",
    "more tests failed than I expected",
    "",
    "no",
    "what does 'the rest' mean here",
])
def test_these_are_real_messages_and_must_reach_the_agent(text):
    """A message that merely contains "more" is usually a question. Answering
    it from a file instead of with a turn would be a silent dropped message,
    which is a far worse failure than a reply that was too long.
    """
    assert not wants_more(text)


# --- the wiring, which is where features like this usually die ---
#
# Every part above can be correct while nothing calls it. That has been the
# actual failure mode on this deployment three times running: decay complete
# with no caller, a graph built and never read, a reaction handler registered
# for an intent that was never requested. So these drive the connector.

import types  # noqa: E402

import pytest  # noqa: E402,F811


class _FakeMessage:
    def __init__(self, mid=999):
        self.id = mid
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content, files=None):
        msg = _FakeMessage(mid=100 + len(self.sent))
        self.sent.append(content)
        return msg


@pytest.fixture
def connector(tmp_path, monkeypatch):
    from src.connectors.discord import DiscordConnector

    conn = DiscordConnector(config={})
    conn.set_setup_context(
        {"defaults": {"reply": {"shorten": {"enabled": True,
                                            "threshold_chars": 300}}}},
        str(tmp_path))
    channel = _FakeChannel()
    bot = types.SimpleNamespace(client=types.SimpleNamespace(
        get_channel=lambda _id: channel))
    monkeypatch.setattr(conn, "_get_bot", lambda _name=None: bot)

    async def passthrough(content, _channel):
        return content
    monkeypatch.setattr(conn, "_linkify_mentions", passthrough)
    return conn, channel


async def test_sending_a_long_reply_posts_the_head_and_keeps_the_rest(connector):
    conn, channel = connector
    sent = await conn.send("555", LONG)

    assert len(channel.sent) == 1, "the whole reply went out despite shortening"
    assert "shortened" in channel.sent[0]
    assert "## What I did not do" not in channel.sent[0]
    assert conn._shortener.store.take(str(sent.id)), "the rest was not kept"


async def test_the_expand_reaction_is_added_by_the_bot(connector):
    """The affordance the owner asked about: how would they know they can
    react. The button is already there, under the message.
    """
    conn, _ = connector
    sent = await conn.send("555", LONG)
    assert sent.reactions == ["🔍"]


async def test_a_short_reply_is_untouched_and_gets_no_reaction(connector):
    conn, channel = connector
    sent = await conn.send("555", "Done. 933 tests pass, service healthy.")
    assert channel.sent == ["Done. 933 tests pass, service healthy."]
    assert sent.reactions == []


async def test_the_rest_is_addressed_to_the_channel_it_came_from(connector):
    conn, _ = connector
    sent = await conn.send("555", LONG)
    assert conn._shortener.store.take_latest_for_channel("555")
    assert conn._shortener.store.take(str(sent.id)) is None, "taken twice"


async def test_shortening_is_skipped_when_the_reply_carries_a_file(connector, tmp_path):
    """An artefact and the text explaining it arrive together, or the
    attachment reads as unexplained.
    """
    conn, channel = connector
    artefact = tmp_path / "chart.png"
    artefact.write_bytes(b"x")
    await conn.send("555", LONG, files=[str(artefact)])
    assert "shortened" not in channel.sent[0]


async def test_the_expanded_text_is_not_shortened_again(connector):
    """Otherwise the rest arrives with its own footer and a second remainder,
    and expanding becomes a loop the reader has to keep tapping.
    """
    conn, channel = connector
    await conn.send("555", LONG, no_shorten=True)
    assert "shortened" not in channel.sent[0]


# --- per agent ---
#
# Not a refinement. On a fleet where one agent writes deploy reports and
# another writes publishable copy, a global length limit is right for the first
# and actively wrong for the second: cutting a draft in half and hiding the
# rest behind a reaction breaks the deliverable.

AGENTS = {
    "engineer": {},                                        # inherits the default
    "writer": {"reply": {"shorten": {"enabled": False}}},   # deliverable is the prose
    "finance": {"reply": {"shorten": {"threshold_chars": 2000}}},
}


def _fleet(tmp_path, **defaults):
    return ReplyShortener({"shorten": {"enabled": True, "threshold_chars": 300,
                                       **defaults}},
                          store_dir=tmp_path, agent_configs=AGENTS)


def test_an_agent_can_opt_out_entirely(tmp_path):
    s = _fleet(tmp_path)
    assert s.shorten(LONG, agent_id="engineer") is not None
    assert s.shorten(LONG, agent_id="writer") is None


def test_an_agent_can_raise_its_own_threshold(tmp_path):
    s = _fleet(tmp_path)
    assert s.threshold_for("finance") == 2000
    assert s.threshold_for("engineer") == 300
    assert s.shorten(LONG, agent_id="finance") is None


def test_an_unknown_agent_gets_the_fleet_default(tmp_path):
    s = _fleet(tmp_path)
    assert s.threshold_for("someone-new") == 300
    assert s.enabled_for("someone-new") is True


def test_the_expand_gesture_is_the_same_everywhere(tmp_path):
    """Per-agent thresholds, one reader-facing control. An expand emoji that
    varied by agent would make the reader learn a different gesture per bot.
    """
    s = _fleet(tmp_path, emoji="📖")
    assert s.emoji == "📖"


# --- the runtime override ---

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from src.core import runtime_state
    monkeypatch.setattr(runtime_state, "_STATE_FILE", tmp_path / "runtime.json",
                        raising=False)
    flags = {}
    monkeypatch.setattr(runtime_state, "get_flag",
                        lambda k, d=None: flags.get(k, d))
    monkeypatch.setattr(runtime_state, "set_flag",
                        lambda k, v: flags.__setitem__(k, v))
    return flags


def test_a_runtime_override_beats_config(tmp_path, runtime):
    s = _fleet(tmp_path)
    runtime["reply_shorten"] = {"enabled": False}
    assert s.shorten(LONG, agent_id="engineer") is None


def test_a_per_agent_override_beats_the_fleet_override(tmp_path, runtime):
    """Otherwise turning it off fleet-wide to try something would silently
    override the one agent that was deliberately configured differently.
    """
    s = _fleet(tmp_path)
    runtime["reply_shorten"] = {"enabled": False}
    runtime["reply_shorten:engineer"] = {"enabled": True}
    assert s.shorten(LONG, agent_id="engineer") is not None
    assert s.shorten(LONG, agent_id="finance") is None


def test_an_override_can_change_only_the_threshold(tmp_path, runtime):
    s = _fleet(tmp_path)
    runtime["reply_shorten"] = {"threshold_chars": 100000}
    assert s.enabled_for("engineer") is True, "enabled must survive a threshold-only change"
    assert s.shorten(LONG, agent_id="engineer") is None


async def test_only_an_admin_can_change_it(monkeypatch, runtime):
    """An agent that can switch off its own length limit will switch it off.
    Same reasoning as set_hitl, which carries it in a comment.
    """
    from src.core.base import ToolContext
    from src.tools import reply_admin

    monkeypatch.setattr(reply_admin, "_is_admin", lambda uid: uid == "owner")

    denied = await reply_admin.set_reply_shorten(
        ToolContext(agent_id="engineer", user_id="someone"), enabled=False)
    assert "only an admin" in denied
    assert runtime == {}, "a non-admin changed the setting"

    allowed = await reply_admin.set_reply_shorten(
        ToolContext(agent_id="engineer", user_id="owner"), enabled=True)
    assert "ON" in allowed
    assert runtime["reply_shorten"] == {"enabled": True}


async def test_reading_the_setting_needs_no_admin(monkeypatch, runtime):
    """Knowing whether your reply will be cut is not a privilege."""
    from src.core.base import ToolContext
    from src.tools import reply_admin

    monkeypatch.setattr(reply_admin, "_is_admin", lambda uid: False)
    out = await reply_admin.set_reply_shorten(ToolContext(agent_id="e", user_id="x"))
    assert "No runtime override" in out


async def test_the_tool_scopes_to_one_agent(monkeypatch, runtime):
    from src.core.base import ToolContext
    from src.tools import reply_admin

    monkeypatch.setattr(reply_admin, "_is_admin", lambda uid: True)
    await reply_admin.set_reply_shorten(
        ToolContext(agent_id="e", user_id="owner"), enabled=False, agent="writer")
    assert runtime == {"reply_shorten:writer": {"enabled": False}}


async def test_an_unusable_threshold_is_refused(monkeypatch, runtime):
    """Below ~100 chars there is no room for a conclusion before the cut, so
    every reply would arrive as a fragment and the feature would look broken
    rather than misconfigured.
    """
    from src.core.base import ToolContext
    from src.tools import reply_admin

    monkeypatch.setattr(reply_admin, "_is_admin", lambda uid: True)
    out = await reply_admin.set_reply_shorten(
        ToolContext(agent_id="e", user_id="owner"), enabled=True, threshold_chars=20)
    assert "too small" in out
    assert runtime == {}
