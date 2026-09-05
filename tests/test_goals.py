"""Goal workstreams — store lifecycle, decisions, dynamic routing, turn budget."""

import time
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
    # Non-participant account: nothing routed
    conn._agent_configs["rio"] = {"routing": {"discord": {"account": "rio-bot"}}}
    del conn._agent_configs["rio"]
    # Explicit channel match still wins over goal routing
    assert conn.get_agent_for_channel("777", "maya-bot") == "maya"


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
