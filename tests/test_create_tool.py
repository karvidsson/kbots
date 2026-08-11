"""Tests for create_tool — agents authoring Python tools (HITL-gated, AST-validated)."""

from src.core.base import ToolContext
from src.core.tools import get_all_tools
from src.tools.ingest import _validate_tool_source, create_tool

VALID_SOURCE = '''
from src.core.base import ToolContext
from src.core.tools import tool


@tool(name="zz_dyn_echo", description="Echo test tool")
async def zz_dyn_echo(ctx: ToolContext, text: str) -> str:
    return f"dyn-echo: {text}"
'''


def _ctx():
    return ToolContext(agent_id="helper")


# --- static validation ---

def test_validator_accepts_valid_tool():
    assert _validate_tool_source(VALID_SOURCE) is None


def test_validator_rejects_syntax_error():
    assert "Syntax error" in _validate_tool_source("def broken(:\n  pass")


def test_validator_rejects_missing_decorator():
    src = "async def naked(ctx, x: str) -> str:\n    return x\n"
    assert "@tool" in _validate_tool_source(src)


def test_validator_rejects_forbidden_imports_and_calls():
    assert "Forbidden import: subprocess" in _validate_tool_source(
        "import subprocess\n" + VALID_SOURCE
    )
    assert "Forbidden call: eval()" in _validate_tool_source(
        VALID_SOURCE + "\nx = eval('1+1')\n"
    )
    assert "Forbidden call: os.system()" in _validate_tool_source(
        "import os\n" + VALID_SOURCE + "\nos.system('ls')\n"
    )
    assert "Forbidden call: shutil.rmtree()" in _validate_tool_source(
        "import shutil\n" + VALID_SOURCE + "\nshutil.rmtree('/x')\n"
    )


# --- end-to-end tool creation ---

async def test_create_tool_hot_loads(overlay, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))

    result = await create_tool(
        _ctx(), "zz_dyn_echo", "Echo test tool", VALID_SOURCE,
    )

    assert not result.startswith("ERROR"), result
    tool_file = overlay / "tools" / "zz_dyn_echo.py"
    assert tool_file.exists()
    assert "Created by agent helper" in tool_file.read_text()

    # Registered and callable, live
    registered = get_all_tools()
    assert "zz_dyn_echo" in registered
    out = await registered["zz_dyn_echo"].func(_ctx(), text="hi")
    assert out == "dyn-echo: hi"


async def test_create_tool_rejects_bad_code(overlay, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    result = await create_tool(
        _ctx(), "evil", "Bad", "import subprocess\n" + VALID_SOURCE,
    )
    assert result.startswith("ERROR")
    assert not (overlay / "tools" / "evil.py").exists()


async def test_create_tool_rejects_duplicate_name(overlay, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    result = await create_tool(
        _ctx(), "web_search", "Clash", VALID_SOURCE,
    )
    assert result.startswith("ERROR") and "already exists" in result


async def test_create_tool_removes_file_when_nothing_registers(overlay, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    # Parses and has a @tool decorator, but raises at import time
    broken = (
        "from src.core.tools import tool\n"
        "raise RuntimeError('boom at import')\n\n"
        "@tool(name='zz_never', description='x')\n"
        "async def zz_never(ctx):\n    return 'x'\n"
    )
    result = await create_tool(_ctx(), "zz_never", "Broken", broken)
    assert result.startswith("ERROR")
    assert not (overlay / "tools" / "zz_never.py").exists()


async def test_create_tool_requires_overlay(monkeypatch):
    monkeypatch.delenv("KBOTS_OVERLAY", raising=False)
    result = await create_tool(_ctx(), "x", "X", VALID_SOURCE)
    assert result.startswith("ERROR")
