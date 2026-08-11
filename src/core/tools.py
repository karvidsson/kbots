"""Tool decorator and registry. Drop a @tool function in src/tools/ and it's available."""

import inspect
from typing import Any, Callable, get_type_hints

from src.core.base import ToolDef, ToolParam

# Global tool registry — populated by @tool decorator at import time
_tool_registry: dict[str, ToolDef] = {}


def tool(
    name: str | None = None,
    description: str | None = None,
    category: str = "general",
    hitl: bool = False,
):
    """Decorator to register a function as an agent tool.

    Schema is auto-generated from type hints and docstring.

    Usage:
        @tool(name="web_search", description="Search the web")
        async def web_search(ctx: ToolContext, query: str, max_results: int = 5) -> str:
            ...
    """
    def decorator(func: Callable) -> Callable:
        tool_name = name or func.__name__
        tool_desc = description or func.__doc__ or f"Tool: {tool_name}"

        params = _extract_params(func)
        tool_def = ToolDef(
            name=tool_name,
            description=tool_desc.strip(),
            parameters=params,
            func=func,
            category=category,
            hitl=hitl,
        )
        _tool_registry[tool_name] = tool_def
        # Attach metadata to the function for introspection
        func._tool_def = tool_def
        return func

    return decorator


def _extract_params(func: Callable) -> list[ToolParam]:
    """Extract tool parameters from function signature and type hints."""
    sig = inspect.signature(func)
    # include_extras keeps Annotated[...] metadata (used for enum choices below)
    hints = get_type_hints(func, include_extras=True)
    params = []

    for param_name, param in sig.parameters.items():
        # Skip 'ctx' (ToolContext) and 'self'
        if param_name in ("ctx", "self"):
            continue

        hint = hints.get(param_name, str)
        # Unwrap Annotated[T, ...] to T for the type mapping (metadata kept on hint)
        base_hint = hint.__origin__ if hasattr(hint, "__metadata__") else hint
        param_type = _python_type_to_str(base_hint)
        has_default = param.default is not inspect.Parameter.empty
        default = param.default if has_default else None

        # Check for list/choices in annotation metadata (future: Annotated types)
        choices = None
        if hasattr(hint, "__metadata__"):
            for meta in hint.__metadata__:
                if isinstance(meta, dict) and "choices" in meta:
                    choices = meta["choices"]

        params.append(ToolParam(
            name=param_name,
            type=param_type,
            description="",  # Could extract from docstring parsing later
            required=not has_default,
            default=default,
            choices=choices,
        ))

    return params


def _python_type_to_str(hint: Any) -> str:
    """Convert Python type hint to simple type string."""
    origin = getattr(hint, "__origin__", None)

    if hint is str:
        return "string"
    elif hint is int:
        return "integer"
    elif hint is float:
        return "number"
    elif hint is bool:
        return "boolean"
    elif origin is list:
        return "array"
    elif origin is dict:
        return "object"
    else:
        return "string"


def get_tool(name: str) -> ToolDef | None:
    """Get a registered tool by name."""
    return _tool_registry.get(name)


def get_all_tools() -> dict[str, ToolDef]:
    """Get all registered tools."""
    return dict(_tool_registry)


def get_tools_for_agent(tool_names: list[str]) -> list[ToolDef]:
    """Get tool definitions for a list of tool names (agent's allowed tools)."""
    tools = []
    for name in tool_names:
        td = _tool_registry.get(name)
        if td:
            tools.append(td)
    return tools
