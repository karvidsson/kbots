#!/usr/bin/env python3
"""Codex PreToolUse hook: deny the builtins kbots blocked for this agent.

Codex has no --disallowedTools. Its hook system is the only place a builtin
can be refused without taking the whole sandbox down to read-only, and it
reports the shell tool under the same name kbots already uses ("Bash"), so
`disallow_builtins` maps across with no translation table.

Invoked by codex with the hook payload on stdin. The tools to refuse arrive
in KBOTS_DENIED_TOOLS rather than argv, because the hook command string is
recorded in codex's own trust bookkeeping and a per-turn argv would change
its identity on every call.

PreToolUse accepts exactly one decision, `deny`, and rejects it without a
non-empty reason (verified against codex-cli 0.153.4: "PreToolUse hook
returned unsupported permissionDecision:allow" / ":ask"). Anything not
denied therefore returns an empty object, which lets the call proceed.
"""

import json
import os
import sys


def decide(payload: dict, denied: set[str]) -> dict:
    tool = str(payload.get("tool_name") or "")
    if tool not in denied:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            # Addressed to the model: it sees this text and needs to know the
            # tool is off for it specifically, not broken or temporarily busy,
            # or it retries the same call for the rest of the turn.
            "permissionDecisionReason": (
                f"{tool} is not available to this agent. It is disabled in "
                f"the agent's kbots configuration (disallow_builtins). Do not "
                f"retry it; use another tool or explain what you cannot do."
            ),
        }
    }


def main() -> int:
    denied = {t.strip() for t in
              os.environ.get("KBOTS_DENIED_TOOLS", "").split(",") if t.strip()}
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        # A payload we cannot parse is a tool call we cannot vet. This hook
        # exists to refuse things, so an unreadable one is refused.
        payload = {"tool_name": "?"}
        if not denied:
            return 0
        denied.add("?")
    json.dump(decide(payload if isinstance(payload, dict) else {}, denied),
              sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
