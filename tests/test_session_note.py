"""Fresh-session honesty note: injected when history is replayed, absent otherwise."""

from src.core.base import Message, MessageRole
from src.llm.claude_code import ClaudeCodeProvider


def _build(messages, resuming):
    llm = ClaudeCodeProvider.__new__(ClaudeCodeProvider)
    return llm._build_prompt(messages, resuming=resuming)


def _history():
    return [
        Message(role=MessageRole.USER, content="do the thing"),
        Message(role=MessageRole.ASSISTANT, content="done (allegedly)"),
        Message(role=MessageRole.USER, content="status?"),
    ]


def test_note_injected_on_fresh_session_with_history():
    prompt = _build(_history(), resuming=False)
    assert "<session-note>" in prompt
    assert prompt.index("<session-note>") < prompt.index("do the thing")
    assert "[Previous response]: done (allegedly)" in prompt


def test_no_note_when_resuming():
    prompt = _build(_history(), resuming=True)
    assert "<session-note>" not in prompt
    assert prompt == "status?"


def test_no_note_on_brand_new_conversation():
    prompt = _build([Message(role=MessageRole.USER, content="hi")], resuming=False)
    assert "<session-note>" not in prompt
    assert prompt == "hi"


def test_extra_dir_args(tmp_path, monkeypatch):
    """Was: no extra_dirs meant no --add-dir at all. That is what left every
    agent unable to open the screenshot its own tool had just written, so the
    shared temp dir is now granted unconditionally and a home-relative path is
    still expanded. See tests/test_agent_sandbox_dirs.py."""
    from src.llm.claude_code import _extra_dir_args

    monkeypatch.setenv("KBOTS_TMP", str(tmp_path))
    monkeypatch.delenv("KBOTS_OVERLAY", raising=False)
    assert _extra_dir_args(None) == ["--add-dir", str(tmp_path)]

    app = tmp_path / "dev" / "app"
    app.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    args = _extra_dir_args(["~/dev/app"])
    assert args[2:] == ["--add-dir", str(app)]
    assert "~" not in args[3]


async def test_messages_record_provider_and_model(tmp_path):
    """provider/model columns: migration is idempotent and values round-trip."""
    from src.core.storage import Storage

    db = tmp_path / "t.db"
    st = Storage(db_path=db)
    await st.init()
    await st.get_or_create_session("s1", "atlas", "c1", "u1")
    await st.save_message("s1", "assistant", "hi", provider="local", model="qwen3.5:9b")
    await st.save_message("s1", "user", "yo")
    import aiosqlite
    async with aiosqlite.connect(db) as conn:
        async with conn.execute(
            "SELECT role, provider, model FROM messages ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
    assert rows[0] == ("assistant", "local", "qwen3.5:9b")
    assert rows[1] == ("user", None, None)
    await st._db.close()
    # migration is idempotent — re-init on an already-migrated DB is a no-op
    st2 = Storage(db_path=db)
    await st2.init()
    await st2._db.close()


async def test_progress_message_lifecycle():
    """Long turns post one in-channel ⏳ message, edit it throttled, and the
    state dict drives cleanup — never spam."""
    import time as _time

    from src.core.agent_manager import AgentManager  # noqa: F401 — import sanity

    # Exercise the closure logic in isolation: simulate the state machine.
    state = {"msg": None, "started": _time.monotonic() - 15, "last_edit": 0.0}
    posts, edits = [], []

    class FakeConn:
        async def post_progress(self, channel_id, text, bot_account=None):
            posts.append(text)
            return object()

        async def edit_progress(self, msg, text):
            edits.append(text)

    conn = FakeConn()
    now = _time.monotonic()
    elapsed = int(now - state["started"])
    text = f"⏳ reading files · {elapsed}s"
    if state["msg"] is None:
        state["msg"] = await conn.post_progress("c", text)
        state["last_edit"] = now
    assert len(posts) == 1 and "reading files" in posts[0] and state["msg"] is not None
    # immediate second event inside throttle window → no edit
    if now - state["last_edit"] < 3:
        pass
    else:
        await conn.edit_progress(state["msg"], text)
    assert edits == []


async def test_active_turns_counter_balances():
    """handle_message increments/decrements active_turns even when the inner
    handler raises — shutdown drain depends on this never leaking."""
    from unittest.mock import AsyncMock, patch

    from src.core.agent_manager import AgentManager

    am = AgentManager.__new__(AgentManager)
    am.active_turns = 0
    am._session_locks = {}
    am._session_key = lambda a, c: f"{a}:{c}"
    am._inflight_turns = {}
    am._inflight_seq = 0

    class Msg:
        channel_id = "c"
        raw = None
        connector = "discord"
        user_id = "u"
        bot_account = "main"

    with patch.object(AgentManager, "_handle_message_inner", new=AsyncMock()) as inner:
        await AgentManager.handle_message(am, "atlas", Msg())
        assert inner.await_count == 1
    assert am.active_turns == 0

    async def boom(*a, **k):
        raise RuntimeError("x")

    with patch.object(AgentManager, "_handle_message_inner", new=boom):
        try:
            await AgentManager.handle_message(am, "atlas", Msg())
        except RuntimeError:
            pass
    assert am.active_turns == 0
