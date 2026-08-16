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
