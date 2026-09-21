"""Goal janitor — one reminder, then expiry, for proposals nobody approved."""

import pytest

from src.core import goals as store
from src.core.goal_janitor import GoalJanitor

H = 3600.0
T0 = 1_000_000.0


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


class _Connector:
    def __init__(self, fail=False):
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    async def send(self, channel_id, content, **kw):
        if self.fail:
            raise RuntimeError("discord down")
        self.sent.append((channel_id, content))


def _proposal(title="Slow goal", channel="c1", created=T0, nominees=("redline",)):
    goal = store.create_goal(title, "d", "maya", channel, "u")
    store._get_db().execute("UPDATE goals SET created_at=? WHERE id=?", (created, goal["id"]))
    store._get_db().commit()
    for a in nominees:
        store.add_nomination(goal["id"], a, "needed")
    return store.get_goal(goal["id"])


def _janitor(conn, **cfg):
    base = {"proposal_timeout_hours": 72, "escalation_user": ""}
    base.update(cfg)
    return GoalJanitor(base, {"discord": conn}, mention=lambda: "<@owner>")


@pytest.mark.asyncio
async def test_reminds_once_at_half_life_then_expires_at_timeout():
    conn = _Connector()
    goal = _proposal()
    j = _janitor(conn)

    assert await j.tick(now=T0 + 10 * H) == {"expired": [], "reminded": []}
    assert conn.sent == []

    out = await j.tick(now=T0 + 37 * H)
    assert out == {"expired": [], "reminded": [goal["id"]]}
    assert len(conn.sent) == 1
    chan, text = conn.sent[0]
    assert chan == "c1" and goal["id"] in text and "1 nomination(s) pending" in text
    assert "expires in 35h" in text and "<@owner>" in text

    # Second pass in the same window: no second nudge.
    assert await j.tick(now=T0 + 50 * H) == {"expired": [], "reminded": []}
    assert len(conn.sent) == 1

    out = await j.tick(now=T0 + 73 * H)
    assert out == {"expired": [goal["id"]], "reminded": []}
    assert store.get_goal(goal["id"])["status"] == "abandoned"
    assert store.get_nomination(goal["id"], "redline")["status"] == "expired"
    assert "expired after 73h" in conn.sent[-1][1] and "goal_create" in conn.sent[-1][1]

    # Gone for good: nothing more to do, nothing more posted.
    assert await j.tick(now=T0 + 200 * H) == {"expired": [], "reminded": []}
    assert len(conn.sent) == 2


@pytest.mark.asyncio
async def test_a_started_goal_is_never_touched():
    conn = _Connector()
    goal = _proposal()
    store.update_goal(goal["id"], "maya", status="brainstorm")
    assert await _janitor(conn).tick(now=T0 + 500 * H) == {"expired": [], "reminded": []}
    assert store.get_goal(goal["id"])["status"] == "brainstorm"
    assert conn.sent == []


@pytest.mark.asyncio
async def test_timeout_zero_disables_everything():
    conn = _Connector()
    _proposal()
    j = _janitor(conn, proposal_timeout_hours=0)
    assert not j.enabled
    assert await j.tick(now=T0 + 500 * H) == {"expired": [], "reminded": []}
    assert conn.sent == []


@pytest.mark.asyncio
async def test_reminder_hours_are_configurable_and_bounded():
    conn = _Connector()
    goal = _proposal()
    j = _janitor(conn, proposal_timeout_hours=24, proposal_remind_hours=2)
    assert (await j.tick(now=T0 + 3 * H))["reminded"] == [goal["id"]]

    # A reminder at or past the timeout is no reminder at all.
    j2 = _janitor(conn, proposal_timeout_hours=24, proposal_remind_hours=24)
    assert not j2.reminds


@pytest.mark.asyncio
async def test_a_failed_post_does_not_stop_the_expiry():
    """The store is the truth; the channel line is a courtesy."""
    conn = _Connector(fail=True)
    goal = _proposal()
    out = await _janitor(conn).tick(now=T0 + 100 * H)
    assert out["expired"] == [goal["id"]]
    assert store.get_goal(goal["id"])["status"] == "abandoned"


@pytest.mark.asyncio
async def test_expiry_wins_over_a_late_reminder():
    """A proposal found already past its timeout on the first tick (say after
    a long outage) is expired, not reminded about."""
    conn = _Connector()
    goal = _proposal()
    out = await _janitor(conn).tick(now=T0 + 100 * H)
    assert out == {"expired": [goal["id"]], "reminded": []}
    assert len(conn.sent) == 1 and "expired" in conn.sent[0][1]


# --- with the vault: the same close path as any other retirement ------------

def _vault_janitor(conn, alert="", **cfg):
    base = {"proposal_timeout_hours": 72, "escalation_user": ""}
    base.update(cfg)
    return GoalJanitor(base, {"discord": conn}, mention=lambda: "<@owner>",
                       vault=object(), alert_channel=lambda: alert)


@pytest.mark.asyncio
async def test_an_anchored_expiry_goes_through_the_close_path(monkeypatch):
    """A borrowed home channel gets the closing notice and is never deleted."""
    closed: list[tuple[str, str]] = []

    async def _close(ctx, goal, reason=""):
        closed.append((goal["id"], reason))
        assert ctx.agent_id == "system" and ctx.vault is not None
        return goal, "closing notice posted"

    deleted: list[str] = []

    async def _delete(vault, endpoint, bot=""):
        deleted.append(endpoint)
        return {"success": True}

    monkeypatch.setattr("src.tools.goals._close_goal", _close)
    monkeypatch.setattr("src.tools.discord_tools._discord_delete", _delete)
    conn = _Connector()
    goal = store.create_goal("Anchored", "d", "maya", "home-1", "u", anchored=True)
    store._get_db().execute("UPDATE goals SET created_at=? WHERE id=?", (T0, goal["id"]))
    store._get_db().commit()

    out = await _vault_janitor(conn).tick(now=T0 + 100 * H)
    assert out["expired"] == [goal["id"]]
    assert store.get_goal(goal["id"])["status"] == "abandoned"
    assert closed == [(goal["id"], "expired after 100h with no decision")]
    assert deleted == [] and conn.sent == []


def _bot_msg(mid, content="card"):
    return {"id": mid, "content": content, "author": {"id": "bot-1", "bot": True}}


def _human_msg(mid, content="what would this cost?"):
    return {"id": mid, "content": content, "author": {"id": "user-1"}}


def _own_room(monkeypatch, messages, delete_result=None):
    """Stub the room fetch, the delete and the close path; return the logs."""
    log = {"deleted": [], "closed": []}

    async def _get(vault, endpoint, bot="", **kw):
        assert endpoint == "/channels/room-9/messages?limit=100"
        return messages

    async def _delete(vault, endpoint, bot=""):
        log["deleted"].append(endpoint)
        return delete_result or {"success": True}

    async def _close(ctx, goal, reason=""):
        log["closed"].append((goal["id"], reason))
        return goal, "closing notice posted; channel archived read-only"

    monkeypatch.setattr("src.tools.discord_tools._discord_get", _get)
    monkeypatch.setattr("src.tools.discord_tools._discord_delete", _delete)
    monkeypatch.setattr("src.tools.goals._close_goal", _close)
    return log


@pytest.mark.asyncio
async def test_an_expired_proposal_with_its_own_room_loses_the_room(monkeypatch):
    """Nothing in that room but the goal's own posts: delete it and say so
    in the alert channel, so the expiry is still visible somewhere."""
    log = _own_room(monkeypatch, [_bot_msg("1"), _bot_msg("2"), _bot_msg("3", "⏳ reminder")])
    conn = _Connector()
    goal = _proposal(channel="room-9")

    out = await _vault_janitor(conn, alert="alerts").tick(now=T0 + 100 * H)
    assert out["expired"] == [goal["id"]]
    assert log["deleted"] == ["/channels/room-9"] and log["closed"] == []
    assert conn.sent == [("alerts", conn.sent[0][1])]
    assert "expired after 100h" in conn.sent[0][1] and "room was removed" in conn.sent[0][1]
    kinds = [r[0] for r in store._get_db().execute(
        "SELECT kind FROM goal_events WHERE goal_id=? AND kind='channel_deleted'", (goal["id"],))]
    assert kinds == ["channel_deleted"]
    # the record keeps its channel id for history
    assert store.get_goal(goal["id"])["channel_id"] == "room-9"


@pytest.mark.asyncio
async def test_a_room_with_a_human_message_is_archived_not_deleted(monkeypatch):
    """A proposal room is routed: the owner answers questions in it. One
    human line in there and an unattended tick must not destroy it."""
    log = _own_room(monkeypatch, [_bot_msg("1"), _human_msg("2"), _bot_msg("3", "answer")])
    conn = _Connector()
    goal = _proposal(channel="room-9")
    out = await _vault_janitor(conn, alert="alerts").tick(now=T0 + 100 * H)
    assert out["expired"] == [goal["id"]]
    assert log["deleted"] == []
    assert log["closed"] == [(goal["id"], "expired after 100h with no decision")]
    assert conn.sent == []


@pytest.mark.asyncio
async def test_no_alert_channel_means_no_delete(monkeypatch):
    """Without somewhere to report it, deleting the room would make the
    expiry invisible. Keep the room and post the notice in it instead."""
    log = _own_room(monkeypatch, [_bot_msg("1")])
    conn = _Connector()
    goal = _proposal(channel="room-9")
    out = await _vault_janitor(conn, alert="").tick(now=T0 + 100 * H)
    assert out["expired"] == [goal["id"]]
    assert log["deleted"] == [] and len(log["closed"]) == 1


@pytest.mark.asyncio
async def test_an_unreadable_or_long_history_is_kept(monkeypatch):
    """Cannot prove the room is empty of human posts: keep it."""
    conn = _Connector()
    log = _own_room(monkeypatch, {"error": True, "status": 403, "detail": "Missing Access"})
    goal = _proposal(channel="room-9")
    await _vault_janitor(conn, alert="alerts").tick(now=T0 + 100 * H)
    assert log["deleted"] == [] and len(log["closed"]) == 1
    assert store.get_goal(goal["id"])["status"] == "abandoned"

    log = _own_room(monkeypatch, [_bot_msg(str(i)) for i in range(100)])
    goal = _proposal(title="Busy", channel="room-9")
    await _vault_janitor(conn, alert="alerts").tick(now=T0 + 100 * H)
    assert log["deleted"] == [] and len(log["closed"]) == 1


@pytest.mark.asyncio
async def test_a_failed_room_delete_falls_back_to_the_close_path(monkeypatch):
    log = _own_room(monkeypatch, [_bot_msg("1")],
                    delete_result={"error": True, "status": 403, "detail": "Missing Access"})
    conn = _Connector()
    goal = _proposal(channel="room-9")
    out = await _vault_janitor(conn, alert="alerts").tick(now=T0 + 100 * H)
    assert out["expired"] == [goal["id"]]
    assert store.get_goal(goal["id"])["status"] == "abandoned"
    assert log["deleted"] == ["/channels/room-9"]
    assert log["closed"] == [(goal["id"], "expired after 100h with no decision")]
    assert conn.sent == []
