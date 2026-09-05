"""Approvers get told, by DM, that something is waiting on them.

The behaviour under test is a promise about what a human sees, so the tests are
written against the DM text and the timing, not against internal calls.
"""

import asyncio

import pytest

from src.core.hitl_notify import HitlNotifier, _fmt_wait


class FakeNotifier(HitlNotifier):
    """Records DMs instead of sending them. `_dm` is the only network seam."""

    def __init__(self, *a, **kw):
        self.fail = kw.pop("fail", False)
        super().__init__(*a, **kw)
        self.sent: list[tuple[str, str]] = []

    async def _dm(self, user_id: str, content: str) -> bool:
        if self.fail:
            raise RuntimeError("discord is down")
        self.sent.append((user_id, content))
        return True


def _n(**kw) -> FakeNotifier:
    kw.setdefault("timeout", 1800)
    return FakeNotifier("tok", ["1000000000000000001"], **kw)


# --- the announcement -------------------------------------------------------

@pytest.mark.asyncio
async def test_announce_says_the_agent_is_waiting_not_broken():
    """The whole point: silence in the agent's own channel reads as failure.
    The DM has to say the agent is blocked and that the human is the blocker."""
    n = _n()
    await n.announced(agent_id="atlas", tool_name="send_email", hitl_id="ab12",
                      description="Send email to a@example.com")
    (_, text), = n.sent
    assert "atlas" in text
    assert "send_email" in text
    assert "ab12" in text
    assert "waiting" in text.lower()
    assert "blocked" in text.lower()


@pytest.mark.asyncio
async def test_announce_states_the_deadline():
    """A human who knows it expires in 30 minutes behaves differently from one
    who thinks it will wait forever."""
    n = _n(timeout=1800)
    await n.announced(agent_id="atlas", tool_name="install_mcp", hitl_id="ab12")
    assert "30 min" in n.sent[0][1]


@pytest.mark.asyncio
async def test_every_approver_is_told():
    n = FakeNotifier("tok", ["1000000000000000001", "1000000000000000002"])
    sent = await n.announced(agent_id="atlas", tool_name="team_add", hitl_id="ab12")
    assert sent == 2
    assert {u for u, _ in n.sent} == {"1000000000000000001", "1000000000000000002"}


@pytest.mark.asyncio
async def test_jump_link_included_when_the_guild_is_known():
    n = _n(guild_id="1000000000000000009")
    await n.announced(agent_id="atlas", tool_name="send_email", hitl_id="ab12",
                      channel_id="1000000000000000008",
                      message_id="1000000000000000007")
    assert ("https://discord.com/channels/1000000000000000009/"
            "1000000000000000008/1000000000000000007") in n.sent[0][1]


@pytest.mark.asyncio
async def test_no_guild_means_no_link_rather_than_a_broken_one():
    """A link that 404s trains people to ignore the DM."""
    n = _n()
    await n.announced(agent_id="atlas", tool_name="send_email", hitl_id="ab12",
                      channel_id="1000000000000000008", message_id="1000000000000000007")
    assert "discord.com/channels" not in n.sent[0][1]


@pytest.mark.asyncio
async def test_long_description_cannot_exceed_the_discord_limit():
    """create_tool puts a whole source file in the description. Over 2000
    characters Discord rejects the message and the notice is lost entirely."""
    n = _n()
    await n.announced(agent_id="atlas", tool_name="create_tool", hitl_id="ab12",
                      description="x" * 50_000)
    assert len(n.sent[0][1]) <= 2000


# --- who gets nothing -------------------------------------------------------

@pytest.mark.asyncio
async def test_no_approvers_means_no_dm():
    n = FakeNotifier("tok", [])
    assert not n.enabled
    assert await n.announced(agent_id="atlas", tool_name="x", hitl_id="ab12") == 0


@pytest.mark.asyncio
async def test_no_token_means_no_dm():
    n = FakeNotifier("", ["1000000000000000001"])
    assert not n.enabled
    assert await n.announced(agent_id="atlas", tool_name="x", hitl_id="ab12") == 0


@pytest.mark.asyncio
async def test_a_dm_failure_is_swallowed():
    """If a closed DM could raise, an unreachable approver would turn into a
    denial. That is worse than the problem this feature solves."""
    n = FakeNotifier("tok", ["1000000000000000001"], fail=True)
    assert await n.announced(agent_id="atlas", tool_name="x", hitl_id="ab12") == 0


# --- the reminder -----------------------------------------------------------

@pytest.mark.asyncio
async def test_reminder_fires_when_still_unanswered():
    n = _n(timeout=1.0, remind_after=0.05)
    task = n.start_reminder(agent_id="atlas", tool_name="send_email", hitl_id="ab12",
                            still_pending=lambda: asyncio.sleep(0, result=True))
    await task
    assert len(n.sent) == 1
    assert "Still waiting" in n.sent[0][1]


@pytest.mark.asyncio
async def test_reminder_is_silent_once_answered():
    """The predicate is re-read at fire time, because the answer can arrive
    from another process while the timer sleeps."""
    n = _n(timeout=1.0, remind_after=0.05)
    task = n.start_reminder(agent_id="atlas", tool_name="send_email", hitl_id="ab12",
                            still_pending=lambda: asyncio.sleep(0, result=False))
    await task
    assert n.sent == []


@pytest.mark.asyncio
async def test_reminder_reads_the_predicate_late_not_early():
    """Arming with 'pending' and answering during the sleep must produce no DM.
    A captured boolean would fail this."""
    state = {"pending": True}

    async def still_pending() -> bool:
        return state["pending"]

    n = _n(timeout=1.0, remind_after=0.1)
    task = n.start_reminder(agent_id="atlas", tool_name="send_email",
                            hitl_id="ab12", still_pending=still_pending)
    await asyncio.sleep(0.02)
    state["pending"] = False       # approver reacts while the timer sleeps
    await task
    assert n.sent == []


@pytest.mark.asyncio
async def test_reminder_can_be_cancelled():
    n = _n(timeout=10.0, remind_after=5.0)
    task = n.start_reminder(agent_id="atlas", tool_name="send_email", hitl_id="ab12",
                            still_pending=lambda: asyncio.sleep(0, result=True))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert n.sent == []


@pytest.mark.asyncio
async def test_reminder_defaults_to_halfway():
    """Late enough that a present approver never sees it, early enough to leave
    time to act."""
    assert _n(timeout=1800).remind_after == 900


@pytest.mark.asyncio
async def test_reminder_can_be_switched_off():
    n = _n(timeout=1800, remind_after=0)
    assert n.start_reminder(agent_id="atlas", tool_name="x", hitl_id="ab12",
                            still_pending=lambda: asyncio.sleep(0, result=True)) is None


@pytest.mark.asyncio
async def test_reminder_after_the_deadline_is_not_armed():
    """It could only ever announce a failure, and the timeout notice already
    does that."""
    n = _n(timeout=60, remind_after=600)
    assert n.start_reminder(agent_id="atlas", tool_name="x", hitl_id="ab12",
                            still_pending=lambda: asyncio.sleep(0, result=True)) is None


# --- closing the loop -------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_is_reported():
    """A silent timeout is how an approval nobody saw becomes a denial nobody
    remembers refusing."""
    n = _n()
    await n.resolved(agent_id="atlas", tool_name="send_email", hitl_id="ab12",
                     status="timeout")
    text = n.sent[0][1]
    assert "did NOT run" in text
    assert "ab12" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["approved", "denied"])
async def test_a_human_who_acted_is_not_told_what_they_did(status):
    """Confirming a click the approver just made is noise, and noise is what
    makes the next DM ignorable."""
    n = _n()
    assert await n.resolved(agent_id="atlas", tool_name="send_email",
                            hitl_id="ab12", status=status) == 0
    assert n.sent == []


# --- formatting -------------------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (30, "under a minute"), (60, "1 min"), (900, "15 min"),
    (1800, "30 min"), (3600, "1h00"), (5400, "1h30"),
])
def test_wait_is_human_readable(seconds, expected):
    assert _fmt_wait(seconds) == expected


# --- the gate actually uses it ----------------------------------------------

@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    return tmp_path


class _Msg:
    id = "1000000000000000007"

    async def add_reaction(self, emoji):
        return None


class _Connector:
    async def send(self, channel_id, content, **kw):
        return _Msg()


class _Vault:
    def get(self, key):
        return "tok" if key == "discord-token" else None


async def _gate(tmp_path, **cfg):
    import aiosqlite

    from src.core.hitl import HITLGate
    db = await aiosqlite.connect(tmp_path / "t.db")
    base = {"channel": "1000000000000000008", "timeout": 0.3,
            "poll_interval": 0.05, "approvers": ["1000000000000000001"]}
    base.update(cfg)
    g = HITLGate(base, db, connector=_Connector(), vault=_Vault())
    await g.init_schema()
    return g, db


@pytest.mark.asyncio
async def test_gate_dms_the_approver_when_it_posts_a_card(tmp_path, overlay, monkeypatch):
    """The engine-side gate must announce, not just post to the channel."""
    g, db = await _gate(tmp_path)
    sent = []

    async def fake_dm(self, user_id, content):
        sent.append(content)
        return True

    monkeypatch.setattr(HitlNotifier, "_dm", fake_dm)
    monkeypatch.setattr("src.core.hitl_notify.resolve_guild_id",
                        lambda *a, **k: asyncio.sleep(0, result=""))
    try:
        result = await g.request_approval("atlas", "send_email", {"to": "a@b.c"},
                                          "Send email to a@b.c")
        assert result["status"] == "timeout"
        assert any("waiting for your approval" in s for s in sent)
        assert any("timed out" in s for s in sent), "no timeout notice"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_gate_without_a_vault_still_gates(tmp_path, overlay):
    """No token means no DM. It must not mean no approval gate."""
    import aiosqlite

    from src.core.hitl import HITLGate
    db = await aiosqlite.connect(tmp_path / "t.db")
    g = HITLGate({"channel": "1000000000000000008", "timeout": 0.2,
                  "poll_interval": 0.05, "approvers": ["1000000000000000001"]},
                 db, connector=_Connector())
    await g.init_schema()
    try:
        assert (await g.request_approval("atlas", "send_email", {}, "d"))["status"] == "timeout"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_approving_early_leaves_no_reminder_running(tmp_path, overlay, monkeypatch):
    """The orphan case: approved quickly, with the reminder still pending.

    Without a cancel that timer survives the request and later DMs about
    something answered minutes ago, which is exactly the noise that teaches
    people to ignore these.
    """
    # remind_after well beyond the approval, so it cannot have fired on its own.
    g, db = await _gate(tmp_path, timeout=5.0, remind_after=3.0)

    async def fake_dm(self, user_id, content):
        return True

    monkeypatch.setattr(HitlNotifier, "_dm", fake_dm)
    monkeypatch.setattr("src.core.hitl_notify.resolve_guild_id",
                        lambda *a, **k: asyncio.sleep(0, result=""))
    try:
        task = asyncio.create_task(
            g.request_approval("atlas", "send_email", {}, "d"))
        # Wait for the row, then approve it the way a reaction would.
        for _ in range(100):
            await asyncio.sleep(0.02)
            async with db.execute(
                    "SELECT hitl_id FROM hitl_pending WHERE status = 'pending'") as cur:
                row = await cur.fetchone()
            if row:
                break
        assert row, "gate never persisted a pending request"
        assert await g.approve(row[0], "1000000000000000001")

        result = await asyncio.wait_for(task, timeout=3.0)
        assert result["status"] == "approved"

        live = [t for t in asyncio.all_tasks()
                if t.get_name().startswith("hitl-remind-") and not t.done()]
        assert live == [], "reminder outlived the request it was about"
    finally:
        await db.close()
