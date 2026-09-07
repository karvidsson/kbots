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
