"""Goal lifecycle end to end, and the one place a goal turn is counted.

The store had 53 unit tests and nothing that drove propose → nominate →
approve → advance → task → block → resume → retire in sequence. Each step
here uses the real tool functions against the real store with only Discord
stubbed, so a regression in the hand-offs between steps is caught before a
human hits it.
"""

from types import SimpleNamespace

import pytest

from src.core import goals as store
from src.core.base import IncomingMessage, ToolContext


@pytest.fixture(autouse=True)
def _isolated_goals_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "goals.db"))
    monkeypatch.setattr(store, "_db", None)
    store._cache.clear()
    yield
    if store._db is not None:
        store._db.close()
    store._db = None
    store._cache.clear()


@pytest.fixture
def world(monkeypatch):
    """The goals tool module with Discord stubbed. Returns a namespace with
    the module, the posts made, the reactions added, and the archive calls."""
    from src.tools import goals as tools

    posts: list[tuple[str, str]] = []
    reactions: list[tuple[str, tuple]] = []
    archived: list[str] = []

    async def _post(ctx, channel_id, content):
        posts.append((channel_id, content))
        return f"msg-{len(posts)}"

    async def _react(ctx, channel_id, message_id, emojis):
        reactions.append((message_id, emojis))

    async def _chan(ctx, title, cfg):
        return "chan-1", ""

    async def _card(ctx, goal):
        return None

    async def _archive(ctx, goal):
        archived.append(goal["id"])
        return "channel archived read-only"

    monkeypatch.setattr(tools, "_post_to_channel", _post)
    monkeypatch.setattr(tools, "_add_reactions", _react)
    monkeypatch.setattr(tools, "_create_goal_channel", _chan)
    monkeypatch.setattr(tools, "_update_card", _card)
    monkeypatch.setattr(tools, "_archive_channel", _archive)
    monkeypatch.setattr(tools, "_agent_tier",
                        lambda a: "privileged" if a == "atlas" else "assistant")
    monkeypatch.setattr(tools, "_known_agents", lambda: {"atlas", "beacon", "quill"})
    monkeypatch.setattr(tools, "_cfg", lambda: {
        "enabled": True, "create_tiers": ["privileged"], "default_turn_budget": 30,
        "max_participants": 4, "_discord": {"guild_id": "g1"}, "_alert_channel": "",
        "alert_on_block": True, "escalation_user": "u1", "objection_window_hours": 0})
    return SimpleNamespace(tools=tools, posts=posts, reactions=reactions,
                           archived=archived)


def _ctx(agent="atlas", channel="home", user="user1"):
    return ToolContext(agent_id=agent, channel_id=channel, user_id=user, vault=object())


@pytest.mark.asyncio
async def test_full_lifecycle(world):
    t = world.tools

    # 1. Propose. Nothing routes, nobody is a participant but the owner.
    out = await t.goal_create(_ctx(), title="Ship the thing", description="d",
                              participants="beacon: owns the repo; quill: owns pricing",
                              plan="first this, then that", turn_budget=4)
    assert "awaiting approval" in out and "beacon, quill (2)" in out
    goal = store.list_goals()[0]
    assert goal["status"] == "proposed" and goal["channel_id"] == "chan-1"
    assert goal["turn_budget"] == 4 and goal["anchored"] == 0
    assert [p["agent_id"] for p in store.list_participants(goal["id"])] == ["atlas"]
    assert store.routed_participants_for_channel("chan-1") == ["atlas"]
    assert store.active_goal_for_channel("chan-1") is None
    # kickoff card first, then one card per nominee, each with its reactions
    assert [m for m, _ in world.reactions] == ["msg-1", "msg-2", "msg-3"]

    # 2. The human reacts: ✅ beacon, ❌ quill (what _handle_goal_reaction does).
    nom = store.nomination_by_message("msg-2")
    assert nom["agent_id"] == "beacon"
    store.decide_nomination(goal["id"], "beacon", True, "user1")
    store.decide_nomination(goal["id"], "quill", False, "user1")
    assert store.nomination_by_message("msg-2") is None           # decided
    roles = {p["agent_id"]: p["role"] for p in store.list_participants(goal["id"])}
    assert roles == {"atlas": "owner", "beacon": "member"}
    assert store.get_nomination(goal["id"], "quill")["status"] == "declined"
    assert store.goal_by_kickoff_message("msg-1")["id"] == goal["id"]

    # 3. Advance through the phases. A non-owner assistant cannot.
    assert "only the owner" in await t.goal_set(_ctx("beacon"), goal["id"], "status", "brainstorm")
    assert (await t.goal_set(_ctx(), goal["id"], "status", "brainstorm")).startswith("✅")
    assert store.goal_by_kickoff_message("msg-1") is None         # no longer proposed
    assert (await t.goal_set(_ctx(), goal["id"], "strategy", "two PRs")).startswith("✅")
    assert (await t.goal_set(_ctx(), goal["id"], "status", "strategy")).startswith("✅")
    assert "illegal transition" in await t.goal_set(_ctx(), goal["id"], "status", "done")
    assert (await t.goal_set(_ctx(), goal["id"], "status", "executing")).startswith("✅")
    assert store.active_goal_for_channel("chan-1")["id"] == goal["id"]
    assert set(store.routed_participants_for_channel("chan-1")) == {"atlas", "beacon"}
    assert "You are: member" in store.build_goal_context("beacon", "chan-1")
    assert "not a participant" in store.build_goal_context("quill", "chan-1")

    # 4. Tasks.
    assert "#1 added" in await t.goal_task(_ctx(), goal["id"], "add", title="write it",
                                           detail="", assignee="beacon")
    assert "doing" in await t.goal_task(_ctx("beacon"), goal["id"], "doing", task_id=1)
    assert "done" in await t.goal_task(_ctx("beacon"), goal["id"], "done", task_id=1)
    assert store.list_tasks(goal["id"], statuses=("done",))[0]["title"] == "write it"

    # 5. The turn ledger: budget 4, human resets.
    for i in range(1, 5):
        assert store.record_turn("chan-1", "beacon", "bot")["exhausted"] is False
    over = store.record_turn("chan-1", "beacon", "bot")
    assert over["exhausted"] and over["announce"] and over["count"] == 5
    assert store.record_turn("chan-1", "atlas", "agent")["announce"] is False
    assert store.record_turn("chan-1", "atlas", "bot", human=True)["count"] == 0
    assert store.record_turn("chan-1", "beacon", "schedule")["count"] == 1
    kinds = [r[0] for r in store._get_db().execute(
        "SELECT kind FROM goal_events WHERE goal_id=? AND kind='budget_exhausted'",
        (goal["id"],))]
    assert kinds == ["budget_exhausted"]

    # 6. Block on the user, then resume.
    brief = await t.goal_block(_ctx(), goal["id"], need_to_know="ctx",
                               need_from_user="approve the copy")
    assert brief.startswith("🧱") and "<@u1>" in brief
    assert store.get_goal(goal["id"])["status"] == "blocked_on_user"
    assert store.active_goal_for_channel("chan-1") is None          # budget off
    assert store.record_turn("chan-1", "beacon", "bot") is None
    assert "atlas" in store.routed_participants_for_channel("chan-1")  # still hears the user
    assert "BLOCKED" in store.build_goal_context("beacon", "chan-1")
    assert (await t.goal_resume(_ctx(), goal["id"], note="answered")).startswith("▶️")
    assert store.get_goal(goal["id"])["status"] == "executing"
    assert store.get_goal(goal["id"])["blocked_brief"] == ""

    # 7. Retire. The room closes, routing ends, the ledger stops.
    out = await t.goal_set(_ctx(), goal["id"], "status", "done")
    assert out.startswith("✅") and "archived read-only" in out
    assert world.archived == [goal["id"]]
    assert store.get_goal(goal["id"])["status"] == "done"
    assert store.routed_participants_for_channel("chan-1") == []
    # #90 replaced is_retired_goal_channel with is_goal_channel: the room stays
    # a goal room forever (so it is never anyone's home), and retirement
    # shows as "no active goal, nobody routed" rather than a separate flag.
    assert store.is_goal_channel("chan-1")
    assert store.active_goal_for_channel("chan-1") is None
    assert store.routed_participants_for_channel("chan-1") == []
    assert store.record_turn("chan-1", "beacon", "bot") is None
    assert store.build_goal_context("beacon", "chan-1") is None
    assert "illegal transition" in await t.goal_set(_ctx(), goal["id"], "status", "executing")


@pytest.mark.asyncio
async def test_abandon_by_decision_archives_the_room(world):
    t = world.tools
    await t.goal_create(_ctx(), title="Doomed", description="d", participants="beacon: why")
    goal = store.list_goals()[0]
    store.decide_nomination(goal["id"], "beacon", True, "user1")
    for status in ("brainstorm", "strategy", "executing"):
        await t.goal_set(_ctx(), goal["id"], "status", status)
    out = await t.goal_propose(_ctx("beacon"), goal["id"], kind="abandon",
                               reason="market moved")
    dec_id = int(out.split("#")[1].split()[0])
    out = await t.goal_decide(_ctx(), dec_id, "adopted")
    assert "abandoned" in out and "archived read-only" in out
    assert world.archived == [goal["id"]]
    # #90 replaced is_retired_goal_channel with is_goal_channel: the room stays
    # a goal room forever (so it is never anyone's home), and retirement
    # shows as "no active goal, nobody routed" rather than a separate flag.
    assert store.is_goal_channel("chan-1")
    assert store.active_goal_for_channel("chan-1") is None
    assert store.routed_participants_for_channel("chan-1") == []


@pytest.mark.asyncio
async def test_an_anchored_goal_never_locks_the_borrowed_channel(monkeypatch):
    """The proposer's home channel is not the goal's to close."""
    from src.tools import goals as tools
    calls = []

    async def _get(vault, endpoint, bot=""):
        calls.append(endpoint)
        return {"guild_id": "g1", "permission_overwrites": [], "topic": ""}

    monkeypatch.setattr("src.tools.discord_tools._discord_get", _get)
    goal = store.create_goal("Anchored", "", "atlas", "home-7", "u", anchored=True)
    assert await tools._archive_channel(_ctx(), goal) == ""
    assert calls == []


@pytest.mark.asyncio
async def test_archive_denies_posting_for_everyone_and_keeps_other_overwrites(monkeypatch):
    from src.tools import goals as tools
    patched: list[tuple[str, dict]] = []

    async def _get(vault, endpoint, bot=""):
        return {"guild_id": "g1", "topic": "Goal workstream: X",
                "permission_overwrites": [
                    {"id": "g1", "type": 0, "allow": "1024", "deny": "0"},
                    {"id": "bot-1", "type": 1, "allow": "2048", "deny": "0"}]}

    async def _patch(vault, endpoint, payload, bot=""):
        patched.append((endpoint, payload))
        return {"id": "chan-1", "name": "goal-x"}

    monkeypatch.setattr("src.tools.discord_tools._discord_get", _get)
    monkeypatch.setattr("src.tools.discord_tools._discord_patch", _patch)
    goal = store.create_goal("X", "", "atlas", "chan-1", "u")
    goal = store.update_goal(goal["id"], "atlas", status="abandoned")
    assert await tools._archive_channel(_ctx(), goal) == "channel archived read-only"
    endpoint, payload = patched[0]
    assert endpoint == "/channels/chan-1"
    by_id = {o["id"]: o for o in payload["permission_overwrites"]}
    assert by_id["bot-1"]["allow"] == "2048"                    # untouched
    assert int(by_id["g1"]["deny"]) & (1 << 11)                 # SEND_MESSAGES
    assert int(by_id["g1"]["deny"]) & (1 << 38)                 # ...IN_THREADS
    assert payload["topic"].startswith("[abandoned]")
    kinds = [r[0] for r in store._get_db().execute(
        "SELECT kind FROM goal_events WHERE goal_id=?", (goal["id"],))]
    assert "archived" in kinds


# --- the accounting point in AgentManager -----------------------------------

class _Stub:
    name = "stub"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send(self, channel_id, content, **kw):
        self.sent.append((channel_id, content))


def _mgr(tmp_path):
    from src.core.agent_manager import AgentManager
    (tmp_path / "agents" / "beacon").mkdir(parents=True)
    mgr = AgentManager(
        agent_configs={"beacon": {
            "display_name": "Beacon",
            "project_dir": str(tmp_path / "agents" / "beacon"),
            "llm": {"provider": "mock"}, "tools": [],
            "routing": {"stub": {"account": "beacon-bot", "channels": []}}}},
        connectors={"stub": _Stub()},
        llm_providers={"mock": object()},
        memory_backends={},
    )
    ran: list[str] = []

    async def _inner(agent_id, message):
        ran.append(message.source)

    mgr._handle_message_inner = _inner
    return mgr, mgr.connectors["stub"], ran


def _msg(channel="chan-1", source="bot", **kw):
    m = IncomingMessage(connector="stub", channel_id=channel, user_id="u",
                        user_name="x", content="hi", source=source)
    for k, v in kw.items():
        setattr(m, k, v)
    return m


@pytest.mark.asyncio
async def test_every_entry_path_is_charged_and_a_human_resets(tmp_path):
    goal = store.create_goal("G", "", "atlas", "chan-1", "u", turn_budget=3)
    for s in ("brainstorm", "strategy", "executing"):
        store.update_goal(goal["id"], "atlas", status=s)
    mgr, stub, ran = _mgr(tmp_path)

    await mgr.handle_message("beacon", _msg(source="bot"))
    await mgr.handle_message("beacon", _msg(source="schedule"))
    await mgr.handle_message("beacon", _msg(source="user", _inter_agent_sender="atlas"))
    assert ran == ["bot", "schedule", "user"]          # 3 turns, all charged
    await mgr.handle_message("beacon", _msg(source="job"))   # 4th: over budget
    assert ran == ["bot", "schedule", "user"]
    assert len(stub.sent) == 1 and "Turn budget reached" in stub.sent[0][1]
    await mgr.handle_message("beacon", _msg(source="bot"))   # still over, no second notice
    assert len(ran) == 3 and len(stub.sent) == 1

    await mgr.handle_message("beacon", _msg(source="user"))  # a human: reset and run
    assert ran[-1] == "user"
    await mgr.handle_message("beacon", _msg(source="bot"))
    assert len(ran) == 5
    assert store.get_goal(goal["id"])["turns_since_human"] == 1


@pytest.mark.asyncio
async def test_channels_without_a_goal_are_not_charged(tmp_path):
    mgr, stub, ran = _mgr(tmp_path)
    for _ in range(40):
        await mgr.handle_message("beacon", _msg(channel="plain", source="bot"))
    assert len(ran) == 40 and stub.sent == []


@pytest.mark.asyncio
async def test_legacy_constructors_are_recognised_by_their_marks(tmp_path):
    from src.core.agent_manager import AgentManager
    bot_raw = SimpleNamespace(author=SimpleNamespace(bot=True))
    assert AgentManager._turn_source(_msg(source="user", raw=bot_raw)) == "bot"
    m = IncomingMessage(connector="stub", channel_id="c", user_id="",
                        user_name="scheduler", content="")
    assert AgentManager._turn_source(m) == "schedule"
    m = IncomingMessage(connector="stub", channel_id="c", user_id="",
                        user_name="jobs", content="")
    assert AgentManager._turn_source(m) == "job"
    assert AgentManager._turn_source(_msg(source="user", _inter_agent_sender="a")) == "agent"
    assert AgentManager._turn_source(_msg(source="user")) == "user"


# --- retirement tells the room (#57) ------------------------------------------

async def _run_to_executing(world, title="Closing"):
    t = world.tools
    await t.goal_create(_ctx(), title=title, description="d", participants="beacon: why")
    goal = store.list_goals()[0]
    store.decide_nomination(goal["id"], "beacon", True, "user1")
    for status in ("brainstorm", "strategy", "executing"):
        await t.goal_set(_ctx(), goal["id"], "status", status)
    return goal


@pytest.mark.asyncio
async def test_done_posts_a_closing_notice_before_archiving(world):
    """The transcript used to end on 'approved' with the outcome only in an
    edited card at the top. The last message must now say what happened."""
    t = world.tools
    goal = await _run_to_executing(world)
    await t.goal_set(_ctx(), goal["id"], "strategy", "Defer submission: 28-48h exceeds the ceiling.")
    await t.goal_task(_ctx(), goal["id"], "add", title="audit")
    await t.goal_task(_ctx(), goal["id"], "done", task_id=1)
    n_before = len(world.posts)

    out = await t.goal_set(_ctx(), goal["id"], "status", "done")

    assert "closing notice posted" in out and "archived read-only" in out
    chan, text = world.posts[-1]
    assert chan == "chan-1"
    assert text.startswith("✅ **COMPLETED: Closing**")
    assert "Defer submission" in text
    assert "1 task(s) done" in text
    assert "Nothing is waiting on you" in text
    assert len(world.posts) == n_before + 1
    assert store.get_goal(goal["id"])["closing_message_id"] == f"msg-{len(world.posts)}"
    # posted before the room closed
    assert world.archived == [goal["id"]]


@pytest.mark.asyncio
async def test_closing_notice_names_open_tasks(world):
    t = world.tools
    goal = await _run_to_executing(world)
    await t.goal_task(_ctx(), goal["id"], "add", title="never started")
    await t.goal_set(_ctx(), goal["id"], "status", "done")
    text = world.posts[-1][1]
    assert "1 left open: #1 never started" in text
    assert "Left open, see above" in text


@pytest.mark.asyncio
async def test_abandon_by_decision_posts_the_reason(world):
    t = world.tools
    goal = await _run_to_executing(world, title="Doomed")
    out = await t.goal_propose(_ctx("beacon"), goal["id"], kind="abandon", reason="market moved")
    dec_id = int(out.split("#")[1].split()[0])
    out = await t.goal_decide(_ctx(), dec_id, "adopted")
    assert "closing notice posted" in out
    text = world.posts[-1][1]
    assert text.startswith("🪦 **ABANDONED: Doomed**")
    assert "Stopped because:** market moved" in text
    assert store.get_goal(goal["id"])["closing_message_id"]


@pytest.mark.asyncio
async def test_closing_notice_is_idempotent(world):
    """Retiring an already retired goal is the retry; it must not post twice."""
    t = world.tools
    goal = await _run_to_executing(world)
    await t.goal_set(_ctx(), goal["id"], "status", "done")
    n = len(world.posts)
    out = await t.goal_set(_ctx(), goal["id"], "status", "done")
    assert "already done" in out and "closing notice already posted" in out
    assert len(world.posts) == n


@pytest.mark.asyncio
async def test_closing_notice_failure_is_visible_and_retryable(world, monkeypatch):
    t = world.tools
    goal = await _run_to_executing(world)
    calls = {"n": 0}

    async def _flaky(ctx, channel_id, content):
        calls["n"] += 1
        if calls["n"] == 1:
            return ""                       # Discord said no
        world.posts.append((channel_id, content))
        return "msg-retry"

    monkeypatch.setattr(t, "_post_to_channel", _flaky)
    out = await t.goal_set(_ctx(), goal["id"], "status", "done")
    assert "closing notice NOT posted" in out and "retry with goal_set status=done" in out
    assert store.get_goal(goal["id"])["status"] == "done"          # retirement stands
    assert store.get_goal(goal["id"])["closing_message_id"] == ""

    out = await t.goal_set(_ctx(), goal["id"], "status", "done")   # the retry
    assert "closing notice posted" in out
    assert store.get_goal(goal["id"])["closing_message_id"] == "msg-retry"
    assert world.posts[-1][1].startswith("✅ **COMPLETED")


def test_closing_notice_is_a_system_notice():
    """It goes through _post_to_channel, which marks it, so it costs no turns."""
    from src.core.goal_notice import is_system_notice, mark
    assert is_system_notice(mark("✅ **COMPLETED: x** (`g-x`)"))
