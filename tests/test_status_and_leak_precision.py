"""Two reports from a fresh Linux VPS, 2026-08-25.

`/status` printed "Active sessions: N" from a dict of RETAINED CONVERSATION
CONTEXTS. An idle agent showing 2 reads as an agent hung on two tasks, and the
natural next step is to restart the service, which destroys live conversation
state to fix a problem that does not exist.

The leak guard flagged Core's own documentation. A deployment that names an
agent "Main Agent" turned every "escalates to a main agent" in ARCHITECTURE.md
into a finding, and self-deploy.sh gates on a green suite, so that install
could not deploy at all until it renamed its agent or edited correct published
docs. The docstring's own reasoning applies to itself: a check that cries wolf
is one people stop reading.
"""

import importlib.util

import pytest

from src.core.agent_manager import AgentManager
from src.core.base import PROJECT_ROOT

SPEC = importlib.util.spec_from_file_location(
    "leaks", PROJECT_ROOT / "scripts" / "check_install_leaks.py")
leaks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(leaks)

DISCORD = (PROJECT_ROOT / "src" / "connectors" / "discord.py").read_text()


# --- /status ---

class _Session:
    def __init__(self, agent_id, messages=0):
        self.agent_id = agent_id
        self.message_count = messages


def _manager(sessions=(), inflight=(), running=()):
    """A manager with only what get_agent_status touches, so the real one runs."""
    mgr = AgentManager.__new__(AgentManager)
    mgr.agent_configs = {"bot": {"display_name": "Bot", "tools": []}}
    mgr.sessions = {f"s{i}": s for i, s in enumerate(sessions)}
    mgr._inflight_turns = {i: t for i, t in enumerate(inflight)}
    mgr._running_procs = {a: object() for a in running}
    return mgr


async def test_an_idle_agent_with_retained_contexts_reports_idle():
    """The whole report. Two contexts, no work, and the old code called that
    "Active sessions: 2".
    """
    mgr = _manager(sessions=[_Session("bot"), _Session("bot")])
    status = await mgr.get_agent_status("bot")
    assert status["running"] is False
    assert status["inflight_turns"] == 0
    assert status["active_sessions"] == 2, "the context count is still reported"


async def test_a_turn_awaiting_approval_reports_as_running():
    """A turn blocked on HITL sits in _inflight_turns with NO subprocess yet.
    Checking _running_procs alone would report a genuinely blocked agent as
    idle, which is the same lie in the other direction.
    """
    mgr = _manager(sessions=[_Session("bot")],
                   inflight=[{"agent_id": "bot", "channel_id": "123"}])
    status = await mgr.get_agent_status("bot")
    assert status["running"] is True
    assert status["inflight_turns"] == 1
    assert status["inflight_channels"] == ["123"]


async def test_a_running_subprocess_reports_as_running():
    mgr = _manager(sessions=[_Session("bot")], running=["bot"])
    assert (await mgr.get_agent_status("bot"))["running"] is True


async def test_another_agents_work_does_not_leak_into_this_status():
    mgr = _manager(sessions=[_Session("bot")],
                   inflight=[{"agent_id": "other", "channel_id": "999"}],
                   running=["other"])
    status = await mgr.get_agent_status("bot")
    assert status["running"] is False
    assert status["inflight_channels"] == []


async def test_the_context_count_survives_for_callers_that_read_it():
    mgr = _manager(sessions=[_Session("bot", messages=4)])
    status = await mgr.get_agent_status("bot")
    assert status["active_sessions"] == 1 and status["total_messages"] == 4


def test_the_command_no_longer_labels_contexts_as_active_sessions():
    """The count was never wrong; the word above it was. "Active sessions"
    means work to an operator, and this number is not work.
    """
    # The line the user sees, not the comment explaining why it changed.
    block = DISCORD.split("Messages handled:")[0][-1200:]
    sent = [ln for ln in block.splitlines() if "Active sessions" in ln
            and not ln.strip().startswith("#")]
    assert sent == [], sent
    assert "Conversation contexts" in block and "retained" in block


def test_the_command_leads_with_whether_anything_is_running():
    block = DISCORD.split("Messages handled:")[0][-1200:]
    assert "Working on:" in block
    assert block.index("Working on:") < block.index("Conversation contexts")


# --- the leak guard's false positives ---

CORE_DOCS = ["README.md", "ARCHITECTURE.md"]


@pytest.mark.parametrize("doc", CORE_DOCS)
def test_a_generic_role_name_does_not_flag_core_documentation(doc):
    """"main agent" is Core's own vocabulary, used throughout by design. A
    deployment naming an agent that turned six lines of correct documentation
    into findings and blocked its own deploys.
    """
    text = (PROJECT_ROOT / doc).read_text()
    assert leaks.roster_hits(text, doc, {"Main Agent"}) == []


def test_an_unscannable_name_is_reported_rather_than_dropped_in_silence():
    """Skipping is the right call and must not read as "this name is clean"."""
    assert leaks.skipped_generic_names({"Main Agent", "Zeta-Bot"}) == ["Main Agent"]
    assert leaks.skipped_generic_names({"Zeta-Bot"}) == []


def test_a_distinctive_two_word_name_is_still_caught():
    assert leaks.roster_hits("Ada Lindqvist saw it", "msg", {"Ada Lindqvist"})


def test_a_two_word_name_no_longer_matches_lowercase_prose():
    """The trade this makes. A real name is written in its own casing; the
    collision is with ordinary English, which is lowercase. In commit-message
    mode the component words are scanned separately and case-insensitively,
    so the lowercase form is still caught there.
    """
    assert leaks.roster_hits("ada lindqvist", "msg", {"Ada Lindqvist"}) == []
    assert leaks.roster_hits("ada saw it", "msg", {"Ada"}), "single words stay case-insensitive"


@pytest.mark.parametrize("name,text", [
    ("Zeta-Bot", "ping zeta-bot about it"),
    ("Data.Bot", "discord-token-data.bot"),
    ("some-bot", "SOME-BOT is down"),
])
def test_names_carrying_punctuation_stay_case_insensitive(name, text):
    """They cannot be mistaken for prose, so the looser match costs nothing and
    catches the lowercased mention that actually leaks in practice.
    """
    assert leaks.roster_hits(text, "msg", {name})
