"""Reflector consolidation + the session-start <lessons> injection."""

import pytest

from src.core.base import LLMResponse
from src.core.reflector import Reflector
from src.core.startup_context import _build_lessons, build_startup_context


class FakeMemory:
    def __init__(self, lessons):
        self._lessons = lessons
        self.calls = []

    async def list_by_category(self, agent_id, category, limit=100, own_only=False):
        self.calls.append({"agent_id": agent_id, "own_only": own_only})
        return [m for m in self._lessons if m.get("category") == category][:limit]


class FakeLLM:
    def __init__(self, name="claude_code", response=None):
        self.name = name
        self.calls = []
        self._response = response

    async def complete(self, messages, tools=None, **kw):
        self.calls.append(kw)
        return self._response or LLMResponse(content="## Preferred\n- alpha works")


class FakeMgr:
    def __init__(self, mem, project_dir, llm, agent_cfg=None):
        self.agent_configs = {"a": agent_cfg or {}}
        self._mem, self._pd, self._llm = mem, project_dir, llm

    def _get_agent_memory(self, aid):
        return self._mem

    def _get_agent_llm(self, aid):
        return self._llm

    def _get_project_dir(self, aid):
        return str(self._pd)


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    return tmp_path


async def test_reflect_writes_lessons_md(overlay, tmp_path):
    lessons = [{"category": "lesson", "content": f"lesson {i}", "confidence": 0.7} for i in range(4)]
    llm = FakeLLM()
    mgr = FakeMgr(FakeMemory(lessons), tmp_path, llm)
    r = Reflector(mgr, {"model": "haiku", "min_lessons": 3})
    assert await r._reflect("a") is True
    out = (tmp_path / "LESSONS.md").read_text()
    assert "# LESSONS" in out and "alpha works" in out
    # cheap: single call on the configured cheap model, no tools
    assert llm.calls[0]["model"] == "haiku"


def _lessons(n=4):
    return [{"category": "lesson", "content": f"lesson {i}", "confidence": 0.7}
            for i in range(n)]


CODEX_CFG = {"llm": {"provider": "codex_cli", "model": "gpt-5.6-sol"}}


async def test_reflect_falls_back_to_the_agents_own_model(overlay, tmp_path):
    """'haiku' is a Claude alias and codex 400s on it. Falling through to the
    provider is not a fix either: providers are shared singletons built from
    defaults.llm, so a codex agent inherited the fleet's 'opus' and 400'd on
    that instead. The agent's own model is the one name known to work."""
    llm = FakeLLM(name="codex_cli")
    r = Reflector(FakeMgr(FakeMemory(_lessons()), tmp_path, llm, CODEX_CFG),
                  {"model": "haiku", "min_lessons": 3})
    assert await r._reflect("a") is True
    assert llm.calls[0]["model"] == "gpt-5.6-sol"


async def test_reflect_prefers_a_configured_cheap_model(overlay, tmp_path):
    llm = FakeLLM(name="codex_cli")
    r = Reflector(FakeMgr(FakeMemory(_lessons()), tmp_path, llm, CODEX_CFG),
                  {"models": {"codex_cli": "gpt-5-mini"}, "min_lessons": 3})
    assert await r._reflect("a") is True
    assert llm.calls[0]["model"] == "gpt-5-mini"


async def test_reflect_uses_provider_default_when_agent_has_no_model(overlay, tmp_path):
    """No model anywhere for this agent, so the provider's own default is
    correct — it is the same fleet default the agent runs on."""
    llm = FakeLLM(name="codex_cli")
    r = Reflector(FakeMgr(FakeMemory(_lessons()), tmp_path, llm, {}),
                  {"min_lessons": 3})
    assert await r._reflect("a") is True
    assert llm.calls[0]["model"] is None


@pytest.mark.parametrize("stop_reason", ["error", "usage_limit"])
async def test_failed_response_does_not_overwrite_lessons(overlay, tmp_path, stop_reason):
    """A provider that could not answer returns its apology as content. Writing
    it replaced whole lessons files with an error string."""
    (tmp_path / "LESSONS.md").write_text("# LESSONS\n\n- the real ones")
    llm = FakeLLM(response=LLMResponse(
        content="Sorry, something went wrong on my end.", stop_reason=stop_reason))
    r = Reflector(FakeMgr(FakeMemory(_lessons()), tmp_path, llm), {"min_lessons": 3})
    assert await r._reflect("a") is False
    assert (tmp_path / "LESSONS.md").read_text() == "# LESSONS\n\n- the real ones"


async def test_reflect_skips_below_min(overlay, tmp_path):
    lessons = [{"category": "lesson", "content": "only one", "confidence": 0.7}]
    mgr = FakeMgr(FakeMemory(lessons), tmp_path, FakeLLM())
    r = Reflector(mgr, {"min_lessons": 3})
    assert await r._reflect("a") is False
    # no digest, but not nothing either: a stub says why (see the own-lessons
    # tests below), so the agent never boots on a stale file
    from src.core.reflector import _HEADER, _STUB
    assert (tmp_path / "LESSONS.md").read_text() == _HEADER + _STUB


def test_build_lessons_block(tmp_path):
    (tmp_path / "LESSONS.md").write_text("# LESSONS\n\n## Preferred\n- do X")
    block = _build_lessons(str(tmp_path))
    assert block.startswith("<lessons>") and block.endswith("</lessons>")
    assert "do X" in block


def test_build_lessons_absent(tmp_path):
    assert _build_lessons(str(tmp_path)) is None      # no LESSONS.md
    assert _build_lessons(None) is None


async def test_startup_context_includes_lessons(tmp_path):
    (tmp_path / "LESSONS.md").write_text("## Preferred\n- lesson content here")
    ctx = await build_startup_context("a", memory=None, project_dir=str(tmp_path))
    assert ctx is not None and "<lessons>" in ctx and "lesson content here" in ctx


def test_system_prompt_asks_for_codify_section():
    # The Codify section is what feeds /codify and the scaffold's
    # "Codify Repetitive Work" guidance — guard against it being dropped.
    from src.core.reflector import _SYSTEM
    assert "## Codify" in _SYSTEM


# --- Graph extraction (reflector → graph memory) ---

from src.core.reflector import _parse_edges  # noqa: E402

try:
    import ladybug  # noqa: F401
    _ladybug_missing = False
except ImportError:
    _ladybug_missing = True

needs_ladybug = pytest.mark.skipif(_ladybug_missing, reason="ladybug not installed")


def test_parse_edges_tolerates_fences_and_junk():
    good = '[{"a": "kbots", "rel": "uses", "b": "LadybugDB", "confidence": 0.9, "source": 1}]'
    assert _parse_edges(good)[0]["b"] == "LadybugDB"
    assert _parse_edges(f"```json\n{good}\n```")[0]["a"] == "kbots"
    assert _parse_edges("no json here") == []
    assert _parse_edges('{"a": "not a list"}') == []
    # malformed entries dropped, confidence clamped, self-loops skipped
    edges = _parse_edges(
        '[{"a": "", "rel": "r", "b": "B"},'
        ' {"a": "X", "rel": "r", "b": "X"},'
        ' {"a": "A", "rel": "r", "b": "B", "confidence": 7}]')
    assert edges == [{"a": "A", "rel": "r", "b": "B", "confidence": 1.0, "source": None}]


class FakeMemoryWithSince(FakeMemory):
    def __init__(self, lessons, since_batches):
        super().__init__(lessons)
        self._batches = list(since_batches)
        self.since_calls = []

    async def list_since(self, agent_id, since=None, limit=100):
        self.since_calls.append(since)
        return self._batches.pop(0) if self._batches else []


class ExtractLLM(FakeLLM):
    def __init__(self, payload):
        super().__init__()
        self._payload = payload

    async def complete(self, messages, tools=None, **kw):
        self.calls.append(kw)
        from src.core.base import LLMResponse
        return LLMResponse(content=self._payload)


@needs_ladybug
async def test_extract_graph_links_edges_and_advances_cursor(overlay, tmp_path, monkeypatch):
    from src.lib import graph_store
    from src.lib.graph_store import GraphMemory
    gm = GraphMemory({"enabled": True, "path": str(tmp_path / "g.lbdb")})
    monkeypatch.setattr(graph_store, "_graph", gm)
    try:
        mems = [
            {"id": "m1", "category": "general", "content": "Kristian founded kbots.",
             "scope": "global", "updated_at": "2026-08-14 10:00:00"},
            {"id": "m2", "category": "project", "content": "kbots uses LadybugDB.",
             "scope": "agent:jarvis", "updated_at": "2026-08-14 10:00:01"},
        ]
        payload = (
            '[{"a": "Kristian", "rel": "founded", "b": "kbots", "confidence": 0.95, "source": "m1"},'
            ' {"a": "kbots", "rel": "uses", "b": "LadybugDB", "confidence": 0.9, "source": "m2"}]')
        mem = FakeMemoryWithSince([], [mems])
        mgr = FakeMgr(mem, tmp_path, ExtractLLM(payload))
        r = Reflector(mgr, {}, graph_cfg={"enabled": True})
        assert r.extract_enabled
        assert await r._extract_graph("jarvis") == 2

        # edge scope inherited from the source memory
        data = await gm.export(agent_id="jarvis")
        by_pair = {(e["src"], e["dst"]): e for e in data["edges"]}
        assert by_pair[("Kristian", "kbots")]["scope"] == "global"
        assert by_pair[("kbots", "LadybugDB")]["scope"] == "agent:jarvis"

        # cursor persisted as (updated_at, id) of the last processed memory
        from src.core import runtime_state
        assert runtime_state.get_flag("graph_extract_cursor_jarvis") == \
            ["2026-08-14 10:00:01", "m2"]

        # next pass: nothing new → no LLM call beyond the first
        assert await r._extract_graph("jarvis") == 0
    finally:
        gm.close()


async def test_extract_graph_noop_when_graph_unavailable(overlay, tmp_path, monkeypatch):
    from src.lib import graph_store
    monkeypatch.setattr(graph_store, "_graph", None)
    mem = FakeMemoryWithSince([], [[{"id": "m1", "content": "x", "scope": "global",
                                     "updated_at": "2026-08-14 10:00:00"}]])
    mgr = FakeMgr(mem, tmp_path, ExtractLLM("[]"))
    r = Reflector(mgr, {}, graph_cfg={"enabled": True})
    assert await r._extract_graph("a") == 0
    assert mem.since_calls == []      # bailed before touching memory


def test_extract_disabled_without_graph_config():
    r = Reflector(object(), {}, graph_cfg=None)
    assert r.extract_enabled is False
    r = Reflector(object(), {}, graph_cfg={"enabled": True, "extract": False})
    assert r.extract_enabled is False


# --- own lessons only, and what happens when there are too few -------------

async def test_reflect_asks_for_the_agents_own_lessons(overlay, tmp_path):
    """The store's default read is fleet-wide; the digest must not be."""
    mem = FakeMemory(_lessons())
    r = Reflector(FakeMgr(mem, tmp_path, FakeLLM()), {"min_lessons": 3})
    assert await r._reflect("a") is True
    assert mem.calls == [{"agent_id": "a", "own_only": True}]


async def test_too_few_lessons_replaces_a_reflector_digest_with_a_stub(overlay, tmp_path):
    """A v1 file digested the whole fleet. It must not survive as this
    agent's 'own experience' just because the agent has nothing to say."""
    from src.core.reflector import _HEADER, _STUB
    (tmp_path / "LESSONS.md").write_text(_HEADER + "## Preferred\n- someone else's\n")
    llm = FakeLLM()
    r = Reflector(FakeMgr(FakeMemory([]), tmp_path, llm), {"min_lessons": 3})
    assert await r._reflect("a") is False
    assert (tmp_path / "LESSONS.md").read_text() == _HEADER + _STUB
    assert llm.calls == []                       # no model call for a stub


async def test_too_few_lessons_replaces_a_provider_apology(overlay, tmp_path):
    from src.core.reflector import _HEADER, _STUB
    (tmp_path / "LESSONS.md").write_text(
        _HEADER + "That took too long and I had to stop. Try a shorter question.\n")
    r = Reflector(FakeMgr(FakeMemory([]), tmp_path, FakeLLM()), {"min_lessons": 3})
    await r._reflect("a")
    assert (tmp_path / "LESSONS.md").read_text() == _HEADER + _STUB


async def test_too_few_lessons_writes_a_stub_when_there_is_no_file(overlay, tmp_path):
    from src.core.reflector import _HEADER, _STUB
    r = Reflector(FakeMgr(FakeMemory([]), tmp_path, FakeLLM()), {"min_lessons": 3})
    await r._reflect("a")
    assert (tmp_path / "LESSONS.md").read_text() == _HEADER + _STUB


async def test_too_few_lessons_never_touches_a_hand_written_file(overlay, tmp_path):
    hand = "# LESSONS\n\nMy own notes, kept by hand.\n"
    (tmp_path / "LESSONS.md").write_text(hand)
    r = Reflector(FakeMgr(FakeMemory([]), tmp_path, FakeLLM()), {"min_lessons": 3})
    await r._reflect("a")
    assert (tmp_path / "LESSONS.md").read_text() == hand


async def test_stub_is_not_rewritten_every_tick(overlay, tmp_path):
    from src.core.reflector import _HEADER, _STUB
    r = Reflector(FakeMgr(FakeMemory([]), tmp_path, FakeLLM()), {"min_lessons": 3})
    await r._reflect("a")
    first = (tmp_path / "LESSONS.md").stat().st_mtime_ns
    await r._reflect("a")
    assert (tmp_path / "LESSONS.md").stat().st_mtime_ns == first
    assert (tmp_path / "LESSONS.md").read_text() == _HEADER + _STUB


def test_digest_version_bump_expires_every_timer_once(overlay, tmp_path):
    from src.core import runtime_state
    mgr = FakeMgr(FakeMemory([]), tmp_path, FakeLLM())
    mgr.agent_configs = {"a": {}, "b": {}}
    runtime_state.set_flag("reflector_last_a", 1_700_000_000.0)
    runtime_state.set_flag("reflector_last_b", 1_700_000_000.0)
    runtime_state.clear_flag("reflector_digest_version")
    r = Reflector(mgr, {})
    assert r._reset_timers_if_digest_changed() is True
    assert runtime_state.get_flag("reflector_last_a") == 0
    assert runtime_state.get_flag("reflector_last_b") == 0
    assert r._due("a", 1_700_000_001.0)
    # once: a later boot with the same version leaves the timers alone
    runtime_state.set_flag("reflector_last_a", 1_700_000_000.0)
    assert r._reset_timers_if_digest_changed() is False
    assert runtime_state.get_flag("reflector_last_a") == 1_700_000_000.0
