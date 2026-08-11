#!/usr/bin/env python3
"""E2E tool tester — runs every tool with real API calls and reports results.

Usage:
    uv run python scripts/test-tools.py
    uv run python scripts/test-tools.py --category google
    uv run python scripts/test-tools.py --tool team_list
"""

import asyncio
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.base import PROJECT_ROOT, ToolContext, resolve_vault_key_file
from src.core.registry import Registry
from src.core.tools import get_all_tools
from src.vault.fernet import FernetVault


def get_vault():
    vault = FernetVault(str(PROJECT_ROOT / "config/secrets.enc"))
    key_file = resolve_vault_key_file()
    vault.unlock(key_file.read_text().strip())
    return vault


# Test definitions: tool_name -> (kwargs, description)
# Only read-only tools — no HITL-gated mutations
TESTS = {
    # --- Memory ---
    "memory_search": ({"query": "test", "limit": 3}, "Search memory"),
    "memory_semantic_search": ({"query": "test query", "limit": 3}, "Semantic search"),
    "team_list": ({}, "List team members"),
    "team_get": ({"name": "admin"}, "Get admin's profile"),

    # --- Discord ---
    "read_channel_history": ({"channel_id": "YOUR_TEST_CHANNEL_ID", "limit": 3}, "Read test channel"),
    "search_channel_history": (
        {"channel_id": "YOUR_TEST_CHANNEL_ID", "query": "test", "limit": 5},
        "Search test channel",
    ),

    # --- Web ---
    "web_search": ({"query": "example search", "max_results": 3}, "Web search"),

    # --- Google ---
    "gmail_search": ({"query": "invoice", "max_results": 2}, "Gmail search"),
    "calendar_list": ({"max_results": 3}, "Calendar events"),
    "drive_search": ({"query": "test", "max_results": 3}, "Drive search"),

    # --- Trello ---
    "trello_boards": ({}, "Trello boards"),

    # --- System ---
    "list_capabilities": ({}, "List all capabilities"),
}


async def run_test(tool_func, kwargs, description, tool_name):
    """Run a single tool test and return result."""
    start = time.time()
    try:
        result = await tool_func(**kwargs)
        duration = int((time.time() - start) * 1000)
        # Check for error indicators
        result_str = str(result)
        # Only flag as error if the tool itself returned an error, not if the content mentions errors
        first_line = result_str.split("\n")[0].lower()
        is_error = any(x in first_line for x in ["error:", "error ", "failed", "http 4", "http 5", "not configured"])
        status = "FAIL" if is_error else "PASS"
        preview = result_str[:120].replace("\n", " ")
        return status, duration, preview
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        return "ERROR", duration, str(e)[:120]


async def main():
    # Parse args
    filter_category = None
    filter_tool = None
    for arg in sys.argv[1:]:
        if arg.startswith("--category="):
            filter_category = arg.split("=", 1)[1]
        elif arg.startswith("--tool="):
            filter_tool = arg.split("=", 1)[1]
        elif arg == "--category" and sys.argv.index(arg) + 1 < len(sys.argv):
            filter_category = sys.argv[sys.argv.index(arg) + 1]
        elif arg == "--tool" and sys.argv.index(arg) + 1 < len(sys.argv):
            filter_tool = sys.argv[sys.argv.index(arg) + 1]

    vault = get_vault()
    # Set active discord token for test bot
    test_token = vault.get("secrets/test-bot-token")
    if test_token:
        vault._secrets["active-discord-token"] = test_token

    ctx = ToolContext(agent_id="test", vault=vault)

    # Discover tools
    registry = Registry()
    registry.discover()
    all_tools = get_all_tools()

    print(f"\n{'='*70}")
    print(f"  kbots Tool Tester — {len(TESTS)} tests defined")
    print(f"{'='*70}\n")

    passed = 0
    failed = 0
    errors = 0
    skipped = 0
    results = []

    for test_key, (kwargs, description) in TESTS.items():
        # Handle tool_name|variant format
        tool_name = test_key.split("|")[0]

        # Filter
        if filter_tool and tool_name != filter_tool:
            continue
        if (filter_category and filter_category.lower() not in description.lower()
                and filter_category.lower() not in tool_name):
            continue

        tool_def = all_tools.get(tool_name)
        if not tool_def:
            print(f"  SKIP  {test_key:40s} — tool not found")
            skipped += 1
            continue

        # Build the call — inject ctx as first arg
        async def call_tool(**kw):
            return await tool_def.func(ctx, **kw)

        status, duration, preview = await run_test(call_tool, kwargs, description, tool_name)

        icon = {"PASS": " OK ", "FAIL": "FAIL", "ERROR": " ERR"}[status]
        color = {"PASS": "\033[32m", "FAIL": "\033[31m", "ERROR": "\033[31m"}[status]
        reset = "\033[0m"

        print(f"  {color}[{icon}]{reset} {test_key:40s} {duration:>5d}ms  {description}")
        if status != "PASS":
            print(f"         {preview}")

        results.append((test_key, status, duration, description, preview))
        if status == "PASS":
            passed += 1
        elif status == "FAIL":
            failed += 1
        else:
            errors += 1

    # Summary
    total = passed + failed + errors
    print(f"\n{'='*70}")
    print(f"  Results: {passed} passed, {failed} failed, {errors} errors, {skipped} skipped ({total} total)")
    print(f"{'='*70}\n")

    # Report failures
    failures = [(k, p) for k, s, d, desc, p in results if s != "PASS"]
    if failures:
        print("  FAILURES:")
        for key, preview in failures:
            print(f"    {key}: {preview}")
        print()

    return 1 if (failed + errors) > 0 else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
