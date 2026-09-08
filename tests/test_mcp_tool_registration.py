"""MCP schema building must survive tool modules that defer their annotations.

Regression (2026-08-19): `process_map.py` uses `from __future__ import
annotations` and annotates a parameter as
`Annotated[str, {"choices": LENS_CHOICES}]`. The annotation therefore reaches
_register_tool as the *string* 'Annotated[str, {"choices": LENS_CHOICES}]',
and pydantic evaluated it in the handler's globals — src.mcp_server — where
LENS_CHOICES does not exist. That raised PydanticUserError during schema
build, which propagated out of build_server and killed the MCP server on
startup: every agent lost all ~167 tools because of one tool's signature.

Two independent guarantees are pinned here:
  1. such a tool registers, with its annotation resolved in its own module;
  2. a tool that cannot register is skipped, not fatal to the rest.
"""

import sys
import types

import pytest

from src.core.base import ToolDef


def _tool_module(source: str, name: str) -> types.ModuleType:
    """Build a real module so annotations resolve against real globals."""
    mod = types.ModuleType(name)
    mod.__dict__["__name__"] = name
    exec(compile(source, f"<{name}>", "exec"), mod.__dict__)
    sys.modules[name] = mod
    return mod


DEFERRED_TOOL = '''
from __future__ import annotations
from typing import Annotated

LENS_CHOICES = ["", "sipoc", "raci"]

async def gaps(ctx, name: str,
               lens: Annotated[str, {"choices": LENS_CHOICES}] = "",
               limit: int = 5) -> str:
    return "ok"
'''


def _register(monkeypatch, func, tool_name):
    from mcp.server.fastmcp import FastMCP

    import src.mcp_server as ms

    mcp = FastMCP("test")
    tool_def = ToolDef(name=tool_name, description="d", parameters=[],
                       func=func, category="test")
    monkeypatch.setattr(ms, "_make_middleware_handler",
                        lambda *a, **k: (lambda **kw: "ok"))
    ms._register_tool(mcp, tool_def, None, None, None, None)
    return mcp


def test_deferred_annotation_with_module_constant_registers(monkeypatch):
    """The exact shape that took the server down must now build a schema."""
    mod = _tool_module(DEFERRED_TOOL, "_kbots_test_deferred")
    try:
        mcp = _register(monkeypatch, mod.gaps, "gaps")
    finally:
        sys.modules.pop("_kbots_test_deferred", None)

    tool = mcp._tool_manager._tools["gaps"]
    props = tool.parameters["properties"]
    # ctx is the injected context, never part of the agent-facing schema.
    assert "ctx" not in props
    assert props["lens"]["type"] == "string"
    assert props["name"]["type"] == "string"
    assert props["limit"]["type"] == "integer"


def test_one_unregisterable_tool_does_not_kill_the_others(monkeypatch, tmp_path):
    """build_server degrades to a missing tool, never to a dead server.

    This drives the real build_server. A tool whose schema cannot be built is
    the failure that actually happened, and the only thing that matters is
    whether the OTHER tools survive it.
    """
    import src.mcp_server as ms

    good = _tool_module(
        "async def fine(ctx, a: str) -> str:\n    return 'ok'\n",
        "_kbots_test_good")

    tools = {
        "broken": ToolDef(name="broken", description="d", parameters=[],
                          func=good.fine, category="t"),
        "fine": ToolDef(name="fine", description="d", parameters=[],
                        func=good.fine, category="t"),
    }

    real_register = ms._register_tool

    def flaky_register(mcp, tool_def, *a, **k):
        if tool_def.name == "broken":
            raise RuntimeError("schema explosion")
        return real_register(mcp, tool_def, *a, **k)

    monkeypatch.setattr(ms, "_register_tool", flaky_register)
    monkeypatch.setattr(ms, "get_all_tools", lambda: tools)
    monkeypatch.setattr(ms.Registry, "discover", lambda self: None)

    config = {
        "defaults": {"memory": {"path": str(tmp_path / "mem.db")}},
        "kbots": {"data_dir": str(tmp_path)},
    }
    try:
        mcp = ms.build_server(vault=None, config=config)
    finally:
        sys.modules.pop("_kbots_test_good", None)

    registered = mcp._tool_manager._tools
    assert "fine" in registered, "registration must continue past the failure"
    assert "broken" not in registered, "the broken tool must be absent, not faked"


@pytest.mark.parametrize("name", ["process_model_gaps", "process_render"])
def test_real_process_tools_register(monkeypatch, name):
    """The two tools that actually broke, against the real registry."""
    from src.core.registry import Registry
    from src.core.tools import get_all_tools

    Registry().discover()
    tools = get_all_tools()
    if name not in tools:
        pytest.skip(f"{name} not present in this build")
    mcp = _register(monkeypatch, tools[name].func, name)
    assert name in mcp._tool_manager._tools


# --- per-turn tool denial (KBOTS_MCP_DENY) ----------------------------------
#
# Claude Code enforces the engine's disallowed_tools list itself, via
# --disallowedTools. Codex has no per-tool switch, so for codex agents the
# whole access-control layer -- agent tool lists, per-sender tier blocks,
# other agents' private tools -- was computed and then dropped. Enforcement
# lands here instead, at registration, so a denied tool is not merely refused
# on call but absent from the surface the model is shown.

def _build_with_tools(monkeypatch, tmp_path, names):
    import src.mcp_server as ms

    mod = _tool_module(
        "async def fine(ctx, a: str) -> str:\n    return 'ok'\n",
        "_kbots_test_deny")
    tools = {n: ToolDef(name=n, description="d", parameters=[],
                        func=mod.fine, category="t") for n in names}
    monkeypatch.setattr(ms, "get_all_tools", lambda: tools)
    monkeypatch.setattr(ms.Registry, "discover", lambda self: None)
    config = {
        "defaults": {"memory": {"path": str(tmp_path / "mem.db")}},
        "kbots": {"data_dir": str(tmp_path)},
    }
    try:
        return ms.build_server(vault=None, config=config)._tool_manager._tools
    finally:
        sys.modules.pop("_kbots_test_deny", None)


def test_denied_tools_are_absent_from_the_surface(monkeypatch, tmp_path):
    monkeypatch.setenv("KBOTS_MCP_DENY", "run_command,create_tool")
    registered = _build_with_tools(
        monkeypatch, tmp_path, ["run_command", "create_tool", "weather"])
    assert "weather" in registered
    assert "run_command" not in registered
    assert "create_tool" not in registered


def test_unset_deny_list_withholds_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("KBOTS_MCP_DENY", raising=False)
    registered = _build_with_tools(
        monkeypatch, tmp_path, ["run_command", "weather"])
    assert {"run_command", "weather"} <= set(registered)


@pytest.mark.parametrize("value", ["", "   ", ",,", " , "])
def test_blank_deny_list_is_not_a_tool_named_empty(monkeypatch, tmp_path, value):
    """A stray separator must not become a tool name, and an empty var must
    not read as 'deny everything'."""
    monkeypatch.setenv("KBOTS_MCP_DENY", value)
    registered = _build_with_tools(monkeypatch, tmp_path, ["weather"])
    assert "weather" in registered


def test_deny_list_tolerates_spacing(monkeypatch, tmp_path):
    monkeypatch.setenv("KBOTS_MCP_DENY", " run_command , weather ")
    registered = _build_with_tools(
        monkeypatch, tmp_path, ["run_command", "weather", "web_search"])
    assert set(registered) == {"web_search"}
