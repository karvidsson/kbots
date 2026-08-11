"""Reflector consolidation + the session-start <lessons> injection."""

import pytest

from src.core.base import LLMResponse
from src.core.reflector import Reflector
from src.core.startup_context import _build_lessons, build_startup_context


class FakeMemory:
    def __init__(self, lessons):
        self._lessons = lessons

    async def list_by_category(self, agent_id, category, limit=100):
        return [m for m in self._lessons if m.get("category") == category][:limit]


class FakeLLM:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, tools=None, **kw):
        self.calls.append(kw)
        return LLMResponse(content="## Preferred\n- alpha works")


class FakeMgr:
    def __init__(self, mem, project_dir, llm):
        self.agent_configs = {"a": {}}
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


async def test_reflect_skips_below_min(overlay, tmp_path):
    lessons = [{"category": "lesson", "content": "only one", "confidence": 0.7}]
    mgr = FakeMgr(FakeMemory(lessons), tmp_path, FakeLLM())
    r = Reflector(mgr, {"min_lessons": 3})
    assert await r._reflect("a") is False
    assert not (tmp_path / "LESSONS.md").exists()


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
