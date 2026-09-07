"""Goal workstreams — store lifecycle, decisions, dynamic routing, turn budget."""

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core import goals as store


@pytest.fixture(autouse=True)
def _isolated_goals_db(tmp_path, monkeypatch):
    """Each test gets a fresh goals.db — never the real data/goals.db."""
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "goals.db"))
    monkeypatch.setattr(store, "_db", None)
    store._cache.clear()
    yield
    if store._db is not None:
        store._db.close()
    store._db = None
    store._cache.clear()


def _mk(status="proposed", channel="123", owner="maya", budget=30):
    goal = store.create_goal("Get 1000 streams", "for the new single", owner,
                             channel, "user1", turn_budget=budget)
    path = {"proposed": [], "brainstorm": ["brainstorm"],
            "strategy": ["brainstorm", "strategy"],
            "executing": ["brainstorm", "strategy", "executing"]}
    for step in path.get(status, [status]):
        goal = store.update_goal(goal["id"], owner, status=step)
    return goal


# --- lifecycle ---

def test_create_defaults_and_owner_participant():
    goal = _mk()
    assert goal["status"] == "proposed"
    assert goal["turn_budget"] == 30
    parts = store.list_participants(goal["id"])
    assert [(p["agent_id"], p["role"]) for p in parts] == [("maya", "owner")]


def test_slug_ids_do_not_collide():
    a = store.create_goal("Same Title", "", "maya", "1", "u")
    b = store.create_goal("Same Title", "", "maya", "2", "u")
    assert a["id"] != b["id"]
    assert b["id"].startswith(a["id"])


def test_legal_transition_chain():
    goal = _mk("executing")
    goal = store.update_goal(goal["id"], "maya", status="paused")
    goal = store.update_goal(goal["id"], "maya", status="executing")
    goal = store.update_goal(goal["id"], "maya", status="done")
    assert goal["status"] == "done"


def test_illegal_transition_rejected():
    goal = _mk()
    with pytest.raises(ValueError, match="illegal transition"):
        store.update_goal(goal["id"], "maya", status="executing")


def test_pause_can_resume_to_earlier_phase():
    goal = _mk("brainstorm")
    goal = store.update_goal(goal["id"], "maya", status="paused")
    goal = store.update_goal(goal["id"], "maya", status="brainstorm")
    assert goal["status"] == "brainstorm"


def test_unknown_field_rejected():
    goal = _mk()
    with pytest.raises(ValueError, match="cannot set"):
        store.update_goal(goal["id"], "maya", nonsense="x")


def test_writes_bump_activity_and_log_events():
    goal = _mk("brainstorm")
    before = store.get_goal(goal["id"])["last_activity_at"]
    time.sleep(0.01)
    store.log_event(goal["id"], "kai", "update", "progress")
    after = store.get_goal(goal["id"])["last_activity_at"]
    assert after > before


# --- tasks ---

def test_task_flow():
    goal = _mk("executing")
    t = store.add_task(goal["id"], "pitch playlists", "", "rio", "maya")
    assert store.update_task(t["id"], "rio", status="doing")["status"] == "doing"
    assert store.update_task(t["id"], "rio", status="done")["status"] == "done"
    assert store.list_tasks(goal["id"]) == []  # open/doing only by default


def test_task_bad_status_rejected():
    goal = _mk("executing")
    t = store.add_task(goal["id"], "x", "", "", "maya")
    with pytest.raises(ValueError):
        store.update_task(t["id"], "maya", status="finished")


# --- decisions & votes ---

def test_decision_vote_and_decide():
    goal = _mk("executing")
    dec = store.create_decision(goal["id"], "pause", "kai",
                                "wait for playlist reply", "", time.time() + 60)
    store.vote(dec["id"], "rio", "object", "we can keep outreach going")
    store.vote(dec["id"], "rio", "support", "changed my mind")  # upsert
    votes = store.list_votes(dec["id"])
    assert len(votes) == 1 and votes[0]["stance"] == "support"
    closed = store.decide(dec["id"], "maya", "adopted")
    assert closed["status"] == "adopted"
    with pytest.raises(ValueError, match="already"):
        store.vote(dec["id"], "kai", "support", "late")
    with pytest.raises(ValueError, match="already"):
        store.decide(dec["id"], "maya", "rejected")


def test_decision_kind_validated():
    goal = _mk("executing")
    with pytest.raises(ValueError):
        store.create_decision(goal["id"], "veto", "kai", "r", "", time.time())


# --- hot-path helpers ---

def test_active_goal_only_in_active_statuses():
    goal = _mk("executing", channel="42")
    assert store.active_goal_for_channel("42")["id"] == goal["id"]
    store.update_goal(goal["id"], "maya", status="paused")
    assert store.active_goal_for_channel("42") is None  # cache invalidated on write
    assert store.routed_participants_for_channel("42") == ["maya"]  # still routed


def test_routed_participants_cover_members():
    goal = _mk("brainstorm", channel="42")
    store.add_participant(goal["id"], "kai")
    assert set(store.routed_participants_for_channel("42")) == {"maya", "kai"}
    store.update_goal(goal["id"], "maya", status="abandoned")
    assert store.routed_participants_for_channel("42") == []


# --- context block ---

def test_goal_context_phases():
    goal = _mk("executing", channel="42")
    store.add_task(goal["id"], "pitch playlists", "", "rio", "maya")
    ctx = store.build_goal_context("maya", "42")
    assert 'status="executing"' in ctx
    assert "You are: owner" in ctx
    assert "NO_REPLY" in ctx
    assert "Turn budget: 30" in ctx
    assert "pitch playlists" in ctx

    store.add_participant(goal["id"], "kai")
    store.create_decision(goal["id"], "pause", "kai", "wait for reply", "",
                          time.time() + 3600)
    store._cache.clear()
    ctx = store.build_goal_context("kai", "42")
    assert "PAUSE proposed by kai" in ctx
    assert "You are: member" in ctx


def test_goal_context_blocked_shows_asks():
    goal = _mk("executing", channel="42")
    store.update_goal(goal["id"], "maya", status="blocked_on_user",
                      blocked_brief='{"know": ["ads rejected"], "do": ["approve budget"]}')
    ctx = store.build_goal_context("maya", "42")
    assert "approve budget" in ctx
    assert "BLOCKED" in ctx


def test_no_context_without_goal():
    assert store.build_goal_context("maya", "999") is None


# --- discord integration ---

def _bot(agent_configs, channel_cfg=None):
    from src.connectors.discord import DiscordBot
    b = DiscordBot.__new__(DiscordBot)
    b.account_name = "main"
    b.client = SimpleNamespace(
        user=SimpleNamespace(id=999, bot=True, name="Atlas", display_name="Atlas"),
        get_channel=lambda cid: None,
    )
    b._seen_message_ids = set()
    b._seen_message_cap = 1000
    b._bot_chain = {}
    b._bot_loop_hits = {}
    b._bot_cooldown = {}
    b._bot_recent_content = {}
    b.connector = SimpleNamespace(config=channel_cfg or {},
                                  _agent_configs=agent_configs)
    return b


def test_goal_channel_routes_participant_without_config():
    from src.connectors.discord import DiscordConnector
    goal = _mk("brainstorm", channel="42", owner="maya")
    store.add_participant(goal["id"], "kai")
    conn = DiscordConnector.__new__(DiscordConnector)
    conn._agent_configs = {
        "maya": {"routing": {"discord": {"account": "maya-bot", "channels": ["777"]}}},
        "kai": {"routing": {"discord": {"account": "kai-bot", "channels": ["888"]}}},
    }
    assert conn.get_agent_for_channel("42", "maya-bot") == "maya"
    assert conn.get_agent_for_channel("42", "kai-bot") == "kai"
    # An ordinary channel is untouched by goal routing.
    assert conn.get_agent_for_channel("777", "maya-bot") == "maya"


def test_goal_channel_routes_nobody_but_participants():
    """The wildcard fallback used to run after the goal lookup, so a bot whose
    agent is not a participant still resolved — by category, by wildcard, or as
    the DM fallback. Every agent on a real fleet routes with an empty channels
    list, so that meant the whole fleet took a turn on every message in a goal
    room, under a goal's context, in front of its participants.
    """
    from src.connectors.discord import DiscordConnector
    goal = _mk("brainstorm", channel="42", owner="maya")
    store.add_participant(goal["id"], "kai")
    conn = DiscordConnector.__new__(DiscordConnector)
    conn._agent_configs = {
        "maya": {"routing": {"discord": {"account": "maya-bot"}}},
        "kai": {"routing": {"discord": {"account": "kai-bot"}}},
        "rio": {"routing": {"discord": {"account": "rio-bot"}}},   # wildcard
    }
    assert conn.get_agent_for_channel("42", "rio-bot") is None
    # ... and with a category, which reaches the category branch instead.
    assert conn.get_agent_for_channel("42", "rio-bot", category_id="c1") is None
    # Participants still route.
    assert conn.get_agent_for_channel("42", "kai-bot") == "kai"
    # A bot with no agent at all resolves to nothing rather than raising.
    assert conn.get_agent_for_channel("42", "ghost-bot") is None


def test_a_mention_does_not_get_a_non_participant_into_a_goal_room():
    """A mention is not a way onto a goal.

    It used to be, on the reasoning that being pinged is somebody deciding to
    bring you in. The audit says otherwise: seven turns by non-members in one
    day, every one of them by mention from inside the room. goal_add_member
    exists so joining is a decision a human makes once and can see; a mention
    route makes it a decision any participant makes silently and repeatedly.

    Nothing is dropped in silence — the connector answers the mention with a
    marked notice instead (see test_goal_notices).
    """
    from src.connectors.discord import DiscordConnector
    goal = _mk("brainstorm", channel="42", owner="maya")
    store.add_participant(goal["id"], "kai")
    conn = DiscordConnector.__new__(DiscordConnector)
    conn._agent_configs = {
        "maya": {"routing": {"discord": {"account": "maya-bot"}}},
        "rio": {"routing": {"discord": {"account": "rio-bot"}}},
    }
    # both directions: the participant routes, the outsider does not, and a
    # mention changes neither answer
    assert conn.get_agent_for_channel("42", "maya-bot") == "maya"
    assert conn.get_agent_for_channel("42", "rio-bot") is None
    assert conn.is_goal_channel("42") is True
    assert conn.is_goal_channel("999") is False


def test_ordinary_channel_keeps_the_wildcard_fallback():
    """The goal rule must not cost non-goal channels their routing."""
    from src.connectors.discord import DiscordConnector
    conn = DiscordConnector.__new__(DiscordConnector)
    conn._agent_configs = {"rio": {"routing": {"discord": {"account": "rio-bot"}}}}
    assert conn.get_agent_for_channel("999", "rio-bot") == "rio"


def test_goal_routing_failure_leaves_ordinary_routing_intact(monkeypatch):
    """A goals store that cannot be read must not take every channel offline.
    Refusing everything is the safe-looking answer and the wrong one: it
    silences the whole fleet on a store error."""
    from src.connectors.discord import DiscordConnector

    def boom(_channel_id):
        raise RuntimeError("store down")

    monkeypatch.setattr(store, "routed_participants_for_channel", boom)
    conn = DiscordConnector.__new__(DiscordConnector)
    conn._agent_configs = {"rio": {"routing": {"discord": {"account": "rio-bot"}}}}
    assert conn._goal_participants("42") == []
    assert conn.get_agent_for_channel("42", "rio-bot") == "rio"


def test_goal_turn_budget_overrides_chain_limit():
    goal = _mk("executing", channel="42", budget=3)
    bot = _bot({"maya": {"routing": {"discord": {"account": "main"}}}})
    now = time.monotonic()
    results = [bot._bot_chain_check(42, from_bot=True, now=now + i)
               for i in range(5)]
    # budget 3: turns 1-3 pass, 4-5 suppressed (global default is 12)
    assert results == [False, False, False, True, True]
    events = [e for e in store._get_db().execute(
        "SELECT kind FROM goal_events WHERE goal_id=?", (goal["id"],))]
    assert ("budget_exhausted",) in [tuple(e) for e in events]


def test_non_goal_channel_keeps_global_limit():
    from src.connectors.discord import _BOT_CHAIN_LIMIT
    bot = _bot({})
    now = time.monotonic()
    results = [bot._bot_chain_check(99, from_bot=True, now=now + i)
               for i in range(_BOT_CHAIN_LIMIT + 2)]
    assert results[:_BOT_CHAIN_LIMIT] == [False] * _BOT_CHAIN_LIMIT
    assert results[-1] is True


# --- an anchored goal earns its own channel when it is advanced --------------

def test_a_goal_records_whether_it_borrowed_a_channel():
    """The proposer's tier is not a durable answer: it can be promoted, and a
    coordinator also anchors when no guild_id is configured. So the fact has to
    be stored at creation rather than re-derived later."""
    borrowed = store.create_goal("Ship the thing", "d", "maya", "111", "u",
                                 anchored=True)
    owned = store.create_goal("Ship other thing", "d", "maya", "222", "u")
    assert borrowed["anchored"] == 1
    assert owned["anchored"] == 0


def test_attaching_a_channel_moves_the_goal_and_clears_the_anchor():
    goal = store.create_goal("Ship the thing", "d", "maya", "111", "u",
                             anchored=True)
    moved = store.attach_channel(goal["id"], "maya", "999")
    assert moved["channel_id"] == "999"
    assert moved["anchored"] == 0


def test_the_old_card_is_forgotten_when_the_goal_moves():
    """card_message_id points into the channel being left. Keeping it makes
    every later status edit try to edit a message in the wrong channel."""
    goal = store.create_goal("Ship the thing", "d", "maya", "111", "u",
                             anchored=True)
    store.update_goal(goal["id"], "maya", card_message_id="555")
    moved = store.attach_channel(goal["id"], "maya", "999")
    assert moved["card_message_id"] == ""


def test_a_goal_that_owns_its_channel_cannot_be_moved():
    """Not a permission check, a data-integrity one: moving a live goal would
    strand every message and task already posted in the old channel."""
    goal = store.create_goal("Ship the thing", "d", "maya", "111", "u")
    with pytest.raises(ValueError, match="already has its own channel"):
        store.attach_channel(goal["id"], "maya", "999")


def test_attach_is_not_reachable_through_the_generic_setter():
    """goal_set exposes update_goal's field list to any owner. channel_id must
    not be in it, or a typo relocates a running workstream."""
    goal = store.create_goal("Ship the thing", "d", "maya", "111", "u",
                             anchored=True)
    for field in ("channel_id", "anchored"):
        with pytest.raises(ValueError, match="cannot set field"):
            store.update_goal(goal["id"], "maya", **{field: "999"})


def test_the_move_is_recorded_in_the_goals_history():
    """goal_events is the audit trail. Nothing reads it back yet, so the row is
    checked directly rather than through an accessor invented for the test."""
    goal = store.create_goal("Ship the thing", "d", "maya", "111", "u",
                             anchored=True)
    store.attach_channel(goal["id"], "maya", "999")
    rows = store._get_db().execute(
        "SELECT kind, payload FROM goal_events WHERE goal_id=? AND kind='channel'",
        (goal["id"],)).fetchall()
    assert len(rows) == 1
    assert "111" in rows[0]["payload"] and "999" in rows[0]["payload"]


def test_existing_proposed_goals_are_treated_as_anchored(tmp_path, monkeypatch):
    """The migration case. Before this column existed nothing could give a goal
    a channel after creation, so a stored 'proposed' goal is anchored by
    definition and must become eligible rather than stay stranded."""
    import sqlite3
    db_path = tmp_path / "legacy.db"
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE goals (
            id TEXT PRIMARY KEY, title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'proposed',
            owner_agent TEXT NOT NULL, connector TEXT NOT NULL DEFAULT 'discord',
            channel_id TEXT NOT NULL, created_by TEXT NOT NULL,
            strategy TEXT NOT NULL DEFAULT '', turn_budget INTEGER NOT NULL DEFAULT 30,
            pause_reason TEXT NOT NULL DEFAULT '', wake_condition TEXT NOT NULL DEFAULT '',
            wake_ref TEXT NOT NULL DEFAULT '', blocked_brief TEXT NOT NULL DEFAULT '',
            card_message_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
            updated_at REAL NOT NULL, last_activity_at REAL NOT NULL);
    """)
    now = time.time()
    con.execute("INSERT INTO goals (id,title,owner_agent,channel_id,created_by,status,"
                "created_at,updated_at,last_activity_at) VALUES "
                "('g-old','Old','maya','111','u','proposed',?,?,?)", (now, now, now))
    con.execute("INSERT INTO goals (id,title,owner_agent,channel_id,created_by,status,"
                "created_at,updated_at,last_activity_at) VALUES "
                "('g-live','Live','maya','222','u','executing',?,?,?)", (now, now, now))
    con.commit()
    con.close()

    monkeypatch.setattr(store, "DB_PATH", str(db_path))
    monkeypatch.setattr(store, "_db", None)
    store._cache.clear()
    assert store.get_goal("g-old")["anchored"] == 1
    # A goal already running keeps whatever channel it is in. Marking it
    # anchored would move a live workstream on its next status change.
    assert store.get_goal("g-live")["anchored"] == 0


# --- goal_set is where an anchored goal gets its channel ---------------------

@pytest.fixture
def goal_tools(monkeypatch):
    """The goals tool module with Discord and config stubbed out.

    Returns (module, created) where `created` records channel-creation calls.
    """
    from src.tools import goals as tools

    created: list[str] = []

    async def fake_create_channel(ctx, title, cfg):
        created.append(title)
        return ("chan-new", "") if cfg.get("_ok", True) else ("", "no guild_id configured")

    async def fake_post(ctx, channel_id, content):
        return "card-1"

    async def fake_update_card(ctx, goal):
        return None

    monkeypatch.setattr(tools, "_create_goal_channel", fake_create_channel)
    monkeypatch.setattr(tools, "_post_to_channel", fake_post)
    monkeypatch.setattr(tools, "_update_card", fake_update_card)
    monkeypatch.setattr(tools, "_agent_tier", lambda a: "coordinator")
    monkeypatch.setattr(tools, "_cfg", lambda: {
        "enabled": True, "create_tiers": ["coordinator", "privileged"],
        "default_turn_budget": 30, "_discord": {"guild_id": "g1"}, "_ok": True})
    return tools, created


def _ctx(agent_id="jarvis", channel_id="home-1"):
    return SimpleNamespace(agent_id=agent_id, channel_id=channel_id,
                           user_id="u1", vault=object())


@pytest.mark.asyncio
async def test_advancing_an_anchored_goal_gives_it_a_channel(goal_tools):
    """The bug: an assistant-tier proposal anchored to the proposer's home
    channel and nothing ever created one afterwards, so the participants were
    routed into a private channel instead of a shared workstream."""
    tools, created = goal_tools
    goal = store.create_goal("Launch the game", "d", "rain", "home-1", "u",
                             anchored=True)

    out = await tools.goal_set(_ctx(), goal["id"], "status", "brainstorm")

    assert created == ["Launch the game"]
    assert "chan-new" in out
    fresh = store.get_goal(goal["id"])
    assert fresh["channel_id"] == "chan-new"
    assert fresh["anchored"] == 0


@pytest.mark.asyncio
async def test_a_goal_that_already_owns_a_channel_is_left_alone(goal_tools):
    tools, created = goal_tools
    goal = store.create_goal("Launch the game", "d", "rain", "chan-own", "u")

    await tools.goal_set(_ctx(), goal["id"], "status", "brainstorm")

    assert created == []
    assert store.get_goal(goal["id"])["channel_id"] == "chan-own"


@pytest.mark.asyncio
async def test_later_status_changes_do_not_create_more_channels(goal_tools):
    """Only the move out of 'proposed' earns a channel. Firing on every
    advance would make a new one at strategy and again at executing."""
    tools, created = goal_tools
    goal = store.create_goal("Launch the game", "d", "rain", "home-1", "u",
                             anchored=True)
    ctx = _ctx()
    await tools.goal_set(ctx, goal["id"], "status", "brainstorm")
    await tools.goal_set(ctx, goal["id"], "status", "strategy")
    await tools.goal_set(ctx, goal["id"], "status", "executing")
    assert created == ["Launch the game"]


@pytest.mark.asyncio
async def test_editing_a_non_status_field_creates_nothing(goal_tools):
    tools, created = goal_tools
    goal = store.create_goal("Launch the game", "d", "rain", "home-1", "u",
                             anchored=True)
    await tools.goal_set(_ctx(), goal["id"], "strategy", "ship it")
    assert created == []


@pytest.mark.asyncio
async def test_a_failed_channel_creation_does_not_undo_the_advance(goal_tools,
                                                                  monkeypatch):
    """The advance is what the caller asked for. A goal running in a borrowed
    channel is exactly what it was doing a second ago, so a Discord failure
    must report and continue, not roll the status back."""
    tools, _ = goal_tools
    monkeypatch.setattr(tools, "_cfg", lambda: {
        "enabled": True, "create_tiers": ["coordinator"], "default_turn_budget": 30,
        "_discord": {"guild_id": ""}, "_ok": False})
    goal = store.create_goal("Launch the game", "d", "rain", "home-1", "u",
                             anchored=True)

    out = await tools.goal_set(_ctx(), goal["id"], "status", "brainstorm")

    fresh = store.get_goal(goal["id"])
    assert fresh["status"] == "brainstorm", "the advance was rolled back"
    assert fresh["channel_id"] == "home-1"
    assert fresh["anchored"] == 1, "must stay eligible for a later retry"
    assert "no guild_id" in out


# --- staffing: nominate, then a human approves each seat ---

def test_nomination_is_not_participation_until_approved():
    """The gap is the feature: an agent nominated by another agent's judgement
    alone must not be routed or spending turns."""
    goal = _mk()
    store.add_nomination(goal["id"], "redline", "owns the game repo")
    assert [n["agent_id"] for n in store.list_nominations(goal["id"], "pending")] == ["redline"]
    assert "redline" not in [p["agent_id"] for p in store.list_participants(goal["id"])]

    store.decide_nomination(goal["id"], "redline", True, "user1")
    assert "redline" in [p["agent_id"] for p in store.list_participants(goal["id"])]
    assert store.get_nomination(goal["id"], "redline")["status"] == "approved"
    assert store.list_nominations(goal["id"], "pending") == []


def test_declined_nomination_is_remembered_and_adds_nobody():
    goal = _mk()
    store.add_nomination(goal["id"], "ledger", "might have a view on cost")
    store.decide_nomination(goal["id"], "ledger", False, "user1")
    nom = store.get_nomination(goal["id"], "ledger")
    assert nom["status"] == "declined" and nom["decided_by"] == "user1"
    assert "ledger" not in [p["agent_id"] for p in store.list_participants(goal["id"])]


def test_second_reaction_on_a_decided_nomination_changes_nothing():
    """Two people reacting, or one reacting twice, must not re-run the add."""
    goal = _mk()
    store.add_nomination(goal["id"], "redline", "owns the repo")
    assert store.decide_nomination(goal["id"], "redline", True, "user1")
    assert store.decide_nomination(goal["id"], "redline", False, "user2") is None
    assert store.get_nomination(goal["id"], "redline")["status"] == "approved"


def test_nomination_and_kickoff_lookup_by_message_id():
    goal = _mk()
    store.add_nomination(goal["id"], "redline", "owns the repo")
    store.set_nomination_message(goal["id"], "redline", "msg-1")
    store.update_goal(goal["id"], "maya", kickoff_message_id="msg-2")

    assert store.nomination_by_message("msg-1")["agent_id"] == "redline"
    assert store.goal_by_kickoff_message("msg-2")["id"] == goal["id"]
    # An unrelated message must fall through so it can reach HITL.
    assert store.nomination_by_message("msg-9") is None
    assert store.goal_by_kickoff_message("msg-9") is None


def test_kickoff_lookup_only_matches_a_goal_still_awaiting_approval():
    goal = _mk()
    store.update_goal(goal["id"], "maya", kickoff_message_id="msg-2")
    store.update_goal(goal["id"], "maya", status="brainstorm")
    assert store.goal_by_kickoff_message("msg-2") is None


def test_renominating_reopens_the_decision():
    goal = _mk()
    store.add_nomination(goal["id"], "redline", "first reason")
    store.decide_nomination(goal["id"], "redline", False, "user1")
    store.add_nomination(goal["id"], "redline", "better reason")
    nom = store.get_nomination(goal["id"], "redline")
    assert nom["status"] == "pending" and nom["reason"] == "better reason"


# --- participant spec parsing: a reason is mandatory ---

def test_parse_nominations_requires_a_reason_per_agent():
    from src.tools.goals import parse_nominations

    pairs, problems = parse_nominations(
        "redline: owns the game repo; rainmaker: owns pricing")
    assert pairs == [("redline", "owns the game repo"), ("rainmaker", "owns pricing")]
    assert problems == []

    pairs, problems = parse_nominations("redline; rainmaker: owns pricing")
    assert pairs == [("rainmaker", "owns pricing")]
    assert len(problems) == 1 and "redline" in problems[0]


def test_parse_nominations_tolerates_list_formatting_and_catches_duplicates():
    from src.tools.goals import parse_nominations

    pairs, problems = parse_nominations(
        "- @redline: owns the repo\n- redline: owns it again\n")
    assert pairs == [("redline", "owns the repo")]
    assert len(problems) == 1 and "twice" in problems[0]
    assert parse_nominations("") == ([], [])


# --- goal_create: the guards that make "less is more" structural ---

@pytest.fixture
def create_tool(monkeypatch, tmp_path):
    """goal_create with Discord stubbed out. Returns (call, posts)."""
    from src.core.base import ToolContext
    from src.tools import goals as tools

    posts: list[tuple[str, str]] = []
    reactions: list[tuple[str, tuple]] = []

    async def _post(ctx, channel_id, content):
        posts.append((channel_id, content))
        return f"msg-{len(posts)}"

    async def _react(ctx, channel_id, message_id, emojis):
        reactions.append((message_id, emojis))

    async def _chan(ctx, title, cfg):
        return "chan-1", ""

    monkeypatch.setattr(tools, "_post_to_channel", _post)
    monkeypatch.setattr(tools, "_add_reactions", _react)
    monkeypatch.setattr(tools, "_create_goal_channel", _chan)
    monkeypatch.setattr(tools, "_agent_tier", lambda a: "privileged")
    monkeypatch.setattr(tools, "_known_agents",
                        lambda: {"maya", "redline", "rainmaker", "ledger", "nestor", "caio"})
    monkeypatch.setattr(tools, "_cfg", lambda: {
        "enabled": True, "create_tiers": ["privileged"], "default_turn_budget": 30,
        "max_participants": 4, "_discord": {}, "_alert_channel": ""})

    async def call(**kw):
        ctx = ToolContext(agent_id="maya", channel_id="home", user_id="user1")
        return await tools.goal_create(ctx, **kw)

    return call, posts, reactions


async def test_goal_create_refuses_a_participant_with_no_reason(create_tool):
    call, posts, _ = create_tool
    out = await call(title="Launch", description="d", participants="redline")
    assert out.startswith("ERROR") and "needs a reason" in out
    assert posts == []          # nothing posted, nothing created
    assert store.list_goals() == []


async def test_goal_create_refuses_more_than_the_cap(create_tool):
    call, posts, _ = create_tool
    out = await call(title="Launch", description="d", participants="; ".join(
        f"{a}: because" for a in
        ("redline", "rainmaker", "ledger", "nestor", "caio")))
    assert out.startswith("ERROR") and "limit is 4" in out
    assert store.list_goals() == []


async def test_goal_create_refuses_an_agent_not_on_the_roster(create_tool):
    call, _, _ = create_tool
    out = await call(title="Launch", description="d",
                     participants="ghostbot: sounds useful")
    assert out.startswith("ERROR") and "not on the roster" in out


async def test_goal_create_stays_proposed_and_nominates_rather_than_adding(create_tool):
    """The whole point: created, staffed on paper, nobody routed, nothing started."""
    call, posts, reactions = create_tool
    out = await call(title="Launch", description="ship it",
                     plan="cut the trailer, then post it",
                     participants="redline: owns the game repo; rainmaker: owns pricing")

    goal = store.list_goals()[0]
    assert goal["status"] == "proposed"
    assert goal["plan"] == "cut the trailer, then post it"
    # owner only — the nominees are not participants yet
    assert [p["agent_id"] for p in store.list_participants(goal["id"])] == ["maya"]
    assert {n["agent_id"] for n in store.list_nominations(goal["id"], "pending")} == {
        "redline", "rainmaker"}

    # one kickoff card carrying ✅, then one card per nominee carrying ✅/❌
    assert len(posts) == 3
    assert "cut the trailer" in posts[0][1] and "awaiting your approval" in posts[0][1]
    assert reactions[0] == ("msg-1", ("✅",))
    assert reactions[1] == ("msg-2", ("✅", "❌"))
    assert goal["kickoff_message_id"] == "msg-1"
    assert "Do not start work" in out
    # named, not counted: a count matches whatever the caller sent, so a
    # malformed participants string reads as success while agents go missing
    assert "redline" in out and "rainmaker" in out


async def test_goal_create_drops_a_self_nomination(create_tool):
    call, posts, _ = create_tool
    await call(title="Launch", description="d",
               participants="maya: I am the owner; redline: owns the repo")
    goal = store.list_goals()[0]
    assert [n["agent_id"] for n in store.list_nominations(goal["id"])] == ["redline"]


# --- mid-goal additions: HITL-gated, reason required ---

@pytest.fixture
def add_tool(monkeypatch):
    """goal_add_member with Discord stubbed. The HITL gate lives in the MCP
    server, so reaching the body here is what 'already approved' looks like."""
    from src.core.base import ToolContext
    from src.tools import goals as tools

    posts: list[tuple[str, str]] = []

    async def _post(ctx, channel_id, content):
        posts.append((channel_id, content))
        return "msg-x"

    monkeypatch.setattr(tools, "_post_to_channel", _post)
    monkeypatch.setattr(tools, "_update_card", lambda ctx, goal: _noop())
    monkeypatch.setattr(tools, "_known_agents",
                        lambda: {"maya", "redline", "rainmaker"})

    async def _noop():
        return None

    async def call(**kw):
        ctx = ToolContext(agent_id="redline", channel_id="c", user_id="user1")
        return await tools.goal_add_member(ctx, **kw)

    return call, posts


def test_goal_add_member_is_hitl_gated():
    """The gate is the feature. If this flag is dropped, any agent can grow a
    goal by itself again and the reason becomes decoration."""
    from src.core.tools import get_all_tools
    assert get_all_tools()["goal_add_member"].hitl is True


async def test_goal_add_member_requires_a_reason(add_tool):
    call, posts = add_tool
    goal = _mk()
    out = await call(goal_id=goal["id"], reason="  ", agent_id="rainmaker")
    assert out.startswith("ERROR") and "reason is required" in out
    assert "rainmaker" not in [p["agent_id"] for p in store.list_participants(goal["id"])]
    assert posts == []


async def test_goal_add_member_records_who_asked_and_why(add_tool):
    call, posts = add_tool
    goal = _mk()
    out = await call(goal_id=goal["id"], reason="owns pricing", agent_id="rainmaker")

    assert "rainmaker" in [p["agent_id"] for p in store.list_participants(goal["id"])]
    nom = store.get_nomination(goal["id"], "rainmaker")
    assert nom["status"] == "approved" and nom["reason"] == "owns pricing"
    assert "owns pricing" in posts[0][1] and "redline" in posts[0][1]
    assert out.startswith("✅")


async def test_goal_add_member_defaults_to_the_caller(add_tool):
    call, _ = add_tool
    goal = _mk()
    await call(goal_id=goal["id"], reason="I built the capture rig")
    assert "redline" in [p["agent_id"] for p in store.list_participants(goal["id"])]


async def test_goal_add_member_rejects_unknown_and_duplicate(add_tool):
    call, _ = add_tool
    goal = _mk()
    assert "not on the roster" in await call(
        goal_id=goal["id"], reason="r", agent_id="ghostbot")
    assert "already on" in await call(
        goal_id=goal["id"], reason="r", agent_id="maya")   # the owner


async def test_goal_create_names_agents_whose_card_failed(create_tool, monkeypatch):
    """A silent drop is worse than a refusal: fourteen tasks once sat assigned
    to three agents who were never added. Name who did not make it."""
    from src.tools import goals as tools

    call, posts, _ = create_tool

    async def _post_first_only(ctx, channel_id, content):
        posts.append((channel_id, content))
        return f"msg-{len(posts)}" if len(posts) <= 2 else ""

    monkeypatch.setattr(tools, "_post_to_channel", _post_first_only)
    out = await call(title="Launch", description="d",
                     participants="redline: owns the repo; rainmaker: owns pricing")
    assert "Nominated, awaiting your ✅: redline (1)" in out
    assert "NOT nominated" in out and "rainmaker" in out
# --- store location: goals belong in the deployment's data dir ---

def test_db_path_follows_the_pinned_data_dir(tmp_path, monkeypatch):
    """Until 2026-09-06 this resolved against PROJECT_ROOT, so every goal lived
    in the engine checkout — the half a re-clone throws away."""
    monkeypatch.setattr(store, "DB_PATH", "goals.db")
    store.set_data_dir(tmp_path / "overlay-data")
    try:
        assert store.db_path() == tmp_path / "overlay-data" / "goals.db"
        goal = store.create_goal("t", "d", "maya", "c", "u")
        assert (tmp_path / "overlay-data" / "goals.db").is_file()
        assert store.get_goal(goal["id"])["title"] == "t"
    finally:
        store.set_data_dir(None)


def test_repinning_the_data_dir_drops_the_open_handle(tmp_path, monkeypatch):
    """A pin arriving after first use must not keep writing to the old file."""
    monkeypatch.setattr(store, "DB_PATH", "goals.db")
    store.set_data_dir(tmp_path / "a")
    try:
        store.create_goal("first", "d", "maya", "c", "u")
        store.set_data_dir(tmp_path / "b")
        assert store.list_goals() == []                     # fresh file
        assert (tmp_path / "b" / "goals.db").is_file()
        store.set_data_dir(tmp_path / "a")
        assert [g["title"] for g in store.list_goals()] == ["first"]
    finally:
        store.set_data_dir(None)


def test_split_store_warning_names_stores_and_ignores_scratch(tmp_path, monkeypatch):
    """Two failure modes, one test. The old warning listed two filenames by
    hand and goals.db slipped past it for three weeks. Matching every file
    instead named eight on this deployment, six of them runtime scratch, and a
    warning that fires eight times a boot is one nobody reads on the ninth."""
    from src.core import base

    monkeypatch.setattr(base, "PROJECT_ROOT", tmp_path)
    legacy = tmp_path / "data"
    (legacy / "graph").mkdir(parents=True)
    for name in ("goals.db", "memory.db", "brand-new-store.db", "old.sqlite"):
        (legacy / name).write_bytes(b"x")
    (legacy / "graph" / "memory.lbdb").mkdir()          # LadybugDB is a directory
    (legacy / "graph" / "memory.lbdb" / "0.seg").write_bytes(b"x")
    for scratch in ("kbots.lock", "heartbeat", "email_watch.json",
                    "interrupted_turns.json", "audit.jsonl", "kbots.log",
                    "goals.db-wal", "goals.db-shm"):
        (legacy / scratch).write_bytes(b"x")
    (legacy / "empty.db").write_bytes(b"")
    (legacy / "empty.lbdb").mkdir()

    stale = base.warn_on_split_store({"kbots": {"data_dir": str(tmp_path / "overlay")}})
    names = {Path(p).name for p in stale}
    # every store, including one in no list anywhere and one that is a folder
    assert names == {"goals.db", "memory.db", "brand-new-store.db",
                     "old.sqlite", "memory.lbdb"}

    # Same dir on both sides is not a split at all.
    assert base.warn_on_split_store({"kbots": {"data_dir": str(legacy)}}) == []
