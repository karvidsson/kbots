"""Agent scaffolding — shared by the setup wizard and the create_agent tool.

Pure, non-interactive functions that write everything a new agent needs:
an agents.yaml entry, the agent directory with AGENTS.md (+ per-CLI stubs),
.mcp.json and .claude/settings.json. New agents come online after a process
restart (agents.yaml is read at startup).
"""

import json
import re
from pathlib import Path

import yaml

from src.core.base import PROJECT_ROOT

TIERS = ("privileged", "coordinator", "assistant")

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# The agent's identity/instructions live in ONE canonical file, named for the
# cross-CLI convention most agent CLIs read natively (Codex, opencode, Amp…).
# CLIs with their own filename get a thin stub that imports it, so the content
# exists exactly once and per-CLI notes have a home.
IDENTITY_FILE = "AGENTS.md"

CLAUDE_STUB = """@AGENTS.md

<!-- Claude Code entry point. This agent's identity and instructions live in
     AGENTS.md (imported above) — edit THAT file, not this one. Only Claude
     Code-specific notes belong below this line. -->
"""


def write_identity(agent_dir: Path, content: str, *, force: bool = False) -> list[Path]:
    """Write the canonical AGENTS.md plus per-CLI stub files.

    Returns the paths written. Existing files are left untouched unless
    force=True (used by dev harnesses that override the prompt).
    """
    agent_dir = Path(agent_dir)
    written: list[Path] = []
    identity_path = agent_dir / IDENTITY_FILE
    if force or not identity_path.exists():
        identity_path.write_text(content)
        written.append(identity_path)
    stub_path = agent_dir / "CLAUDE.md"
    # A legacy full CLAUDE.md is only replaced under force — otherwise the
    # migration script handles the rename explicitly.
    if force or not stub_path.exists():
        stub_path.write_text(CLAUDE_STUB)
        written.append(stub_path)
    return written


def read_identity(project_dir: Path | str) -> str:
    """The agent's identity text: AGENTS.md, or legacy CLAUDE.md, or empty."""
    for name in (IDENTITY_FILE, "CLAUDE.md"):
        path = Path(project_dir) / name
        try:
            if path.exists():
                return path.read_text()
        except OSError:
            continue
    return ""


def agent_entries(overlay: Path) -> dict[str, dict]:
    """All agent entries across agents*.yaml files in the overlay config."""
    entries: dict[str, dict] = {}
    config_dir = Path(overlay) / "config"
    if not config_dir.is_dir():
        return entries
    for agents_file in sorted(config_dir.glob("agents*.yaml")):
        try:
            data = yaml.safe_load(agents_file.read_text()) or {}
        except yaml.YAMLError:
            continue
        for agent_id, agent_cfg in (data.get("agents") or {}).items():
            entries.setdefault(agent_id, agent_cfg or {})
    return entries


def agent_is_privileged_or_coordinator(overlay: Path, agent_id: str) -> bool:
    """True if the agent is the coordinator (main) or a privileged agent."""
    entry = agent_entries(overlay).get(agent_id, {})
    return bool(entry.get("privileged")) or entry.get("tier") == "coordinator"


def cc_allow_for_tier(tier: str) -> list[str]:
    """Derive Claude Code settings.json allow list from agent tier.

    Isolation model:
      privileged  — full filesystem + shell (rescue/ops agents)
      coordinator — the main/orchestrator agent: may READ everywhere,
                    including other agents' work folders
      assistant   — confined to its own project directory (Claude Code runs
                    with the agent's project_dir as cwd, so ./** is its own
                    folder); cannot read other agents' folders
    """
    if tier == "privileged":
        # Full access: shell + the file-write builtins must be ALLOW-listed too,
        # else headless Claude Code stalls asking to approve each Write/Edit.
        # NB: "mcp__kbots-tools" (server-level, no __* suffix) — Claude Code does
        # not support wildcards in MCP permission rules; the suffixed form is
        # silently treated as an unknown tool name and grants nothing.
        return ["Bash(*)", "Read(*)", "Write(*)", "Edit(*)", "MultiEdit(*)",
                "Glob(*)", "Grep(*)", "WebSearch(*)", "WebFetch(*)",
                "mcp__kbots-tools"]
    if tier == "coordinator":
        # Reads everywhere; full file ops in its OWN folder; restricted shell.
        return ["Read(*)", "Glob(*)", "Grep(*)",
                "Write(./**)", "Edit(./**)", "MultiEdit(./**)",
                "mcp__kbots-tools", "Bash(uv run python:*)"]
    # assistant — confined to its own project_dir, but FULL file ops there so it
    # can actually work (save data, build reports). No shell (see disallow).
    return ["Read(./**)", "Write(./**)", "Edit(./**)", "MultiEdit(./**)",
            "Glob(./**)", "Grep(./**)", "mcp__kbots-tools"]


def default_claude_md(display_name: str, description: str, agent_dir: Path,
                      personality: str = "") -> str:
    """Starter identity file (AGENTS.md) for a scaffolded agent."""
    personality_line = f"- Be {personality}\n" if personality else ""
    return f"""# {display_name}

## Identity

You are **{display_name}**. {description}.

## Guidelines

{personality_line}- Search memory before asking the user for context you might already have
- Remember important things from conversations by storing them to memory

## How to work

The same loop on every request:

1. **Classify the ask.** A question wants findings, not actions. A task wants
   the thing done.
2. **Define done** in one line before acting. Ambiguous ask → one clarifying
   question, not a guess.
3. **Evidence over recall.** Use your tools for anything factual — never
   invent names, IDs, values, or file contents.
4. **Act small.** The smallest action that completes the ask; don't expand
   scope uninvited.
5. **Verify by observation.** Check the tool result before saying done — a
   call sent is not an outcome confirmed.
6. **Report outcome first**, then essentials. Name caveats and unknowns.
7. **Failed? Say so plainly** — what you tried, what happened, what's next.
   Never claim unobserved success.

## File System

You run inside a sandboxed kbots instance. Your project directory is in
the **overlay**, separate from the engine code.

- **Your directory:** `{agent_dir}/` — your AGENTS.md, config, and data live here
- **Generated files:** use `$KBOTS_TMP` (media, docs, scratch) for any files you create
- **NEVER** `git add`, `git commit`, or `git push` in the kbots core engine
  directory — it is the framework, not your workspace. Agent configs, custom
  files, and deployment data belong in the overlay.

## Memory System

Use your MCP tools for memory — do NOT use curl or HTTP calls.

- `memory_search` — keyword/FTS5 search
- `memory_semantic_search` — embedding-based conceptual search
- `memory_store` — save important information
- `memory_forget` — remove a memory by ID

## Working Memory (get smarter over time)

You have a reward-based lessons system. A `<lessons>` block may be injected at
the start of a session — **read it first**: prefer approaches under "Preferred",
and do NOT retry anything under "Avoid / Dead ends".

- When you learn something durable — a working approach, a dead end, or a
  generalisable insight — call `remember_lesson(content=...)`. Save the *lesson*,
  not the one-off answer.
- Give feedback so good lessons rise and bad ones fade: `record_outcome(lesson,
  outcome)` where outcome is `useful`, `dead_end`, or `corrected`.
- **When the user corrects you, that's high-signal** — call
  `record_outcome(lesson, "corrected", correction="&lt;the right answer&gt;")`.

Useful lessons resurface automatically in future sessions; dead ends fade away.

## Codify Repetitive Work

When work turns out to be repeatable, turn it into a capability instead of
re-deriving it from memory each time. Triggers:

- The user confirms a final format or result ("I'm happy with this — keep
  doing it this way").
- You complete substantially the same multi-step task a second time.
- Your `<lessons>` block contains a "## Codify" section.

When a trigger fires, **propose, don't just install**: tell the user what
you'd codify and how, then on approval use `create_skill` (a prompt pipeline)
or `create_tool` (real code — requires human approval). Write the skill so a
fresh session with zero conversation context can reproduce the result:
concrete steps, exact file paths, known gotchas, and what done looks like.

**Prefer code over prompt.** Robustness is moving decisions out of the
prompt: put everything deterministic — parsing, computation, chart
rendering, template filling, file naming — into ONE pipeline tool that
fails loudly, and keep the skill a thin wrapper for what is irreducibly
judgment (narrative, delivery). A model should never re-derive what code
can produce.

**Pin the assets first.** Copy any template, stylesheet, or reference file
the skill depends on into your own project directory and reference that
path — never `$KBOTS_TMP`, which gets cleaned.

If the user declines, save that with `remember_lesson` so you don't
re-propose the same thing.

## Team

The team roster is at `config/team.json`. User context is injected before
each message so you know who you're talking to.
"""


def scaffold_agent(
    overlay: Path,
    name: str,
    display_name: str,
    description: str,
    *,
    model: str = "sonnet",
    tier: str = "assistant",
    personality: str = "",
    routing: dict | None = None,
    bot_account: str = "main",
    engine_root: Path | None = None,
    kbots_modules: str = "",
    profile: str = "",
    agents_file: str = "agents.yaml",
    claude_md: str | None = None,
    exist_ok: bool = False,
) -> list[Path]:
    """Create everything a new agent needs. Returns the list of paths written.

    Raises ValueError on invalid input or if the agent already exists
    (unless exist_ok, in which case the yaml entry is updated and existing
    files are left untouched).
    """
    overlay = Path(overlay).resolve()
    engine_root = Path(engine_root).resolve() if engine_root else PROJECT_ROOT

    if not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid agent name {name!r} — lowercase letters, digits, '-'/'_', max 32 chars"
        )
    if tier not in TIERS:
        raise ValueError(f"Invalid tier {tier!r} — one of {', '.join(TIERS)}")
    if not (overlay / "config").is_dir():
        raise ValueError(f"Overlay config directory not found: {overlay / 'config'}")

    agent_dir = overlay / "agents" / name
    written: list[Path] = []

    # --- agents yaml entry ---
    agents_path = overlay / "config" / agents_file
    agents_cfg: dict = {}
    if agents_path.exists():
        agents_cfg = yaml.safe_load(agents_path.read_text()) or {}
    agents = agents_cfg.setdefault("agents", {})
    if name in agents and not exist_ok:
        raise ValueError(f"Agent {name!r} already exists in {agents_path.name}")

    privileged = tier == "privileged"
    entry: dict = {
        "display_name": display_name,
        "description": description,
        "project_dir": str(agent_dir),
        # Persisted so runtime policy (e.g. who may call create_agent) can
        # check the caller's tier.
        "tier": tier,
        "llm": {"provider": "claude_code", "model": model},
        "tools": "all",
        "skills": "all",
        "routing": routing or {
            "discord": {"account": bot_account, "channels": [], "mentions": True}
        },
    }
    if privileged:
        entry["privileged"] = True
    else:
        # Block only shell; Write/Edit/MultiEdit stay available but are
        # allow-list-scoped to the agent's own folder (./** in settings.json),
        # so every agent has full permissions in its working directory.
        entry["disallow_builtins"] = ["Bash"]

    agents[name] = entry
    agents_path.write_text(yaml.dump(agents_cfg, default_flow_style=False, sort_keys=False))
    written.append(agents_path)

    # --- agent directory ---
    agent_dir.mkdir(parents=True, exist_ok=True)

    content = claude_md or default_claude_md(display_name, description, agent_dir, personality)
    written.extend(write_identity(agent_dir, content))

    # --- .mcp.json ---
    mcp_json_path = agent_dir / ".mcp.json"
    if not mcp_json_path.exists():
        # KBOTS_BOT_ACCOUNT/KBOTS_AGENT_ID pin the MCP server to this agent's
        # own Discord identity — without them it falls back to the first
        # configured account, so the agent's Discord tools would silently act
        # as another bot (wrong token, wrong DMs, wrong "own messages").
        account = ((routing or {}).get("discord") or {}).get("account") or bot_account
        mcp_env = {
            "KBOTS_PROFILE": profile or "${KBOTS_PROFILE:-}",
            "KBOTS_PROJECT_DIR": str(agent_dir),
            "KBOTS_OVERLAY": str(overlay),
            "KBOTS_BOT_ACCOUNT": account,
            "KBOTS_AGENT_ID": name,
        }
        if kbots_modules:
            mcp_env["KBOTS_MODULES"] = kbots_modules
        mcp_json = {
            "mcpServers": {
                "kbots-tools": {
                    "command": str(engine_root / ".venv" / "bin" / "python3"),
                    "args": ["-m", "src.mcp_server"],
                    "cwd": str(engine_root),
                    "env": mcp_env,
                }
            }
        }
        mcp_json_path.write_text(json.dumps(mcp_json, indent=2) + "\n")
        written.append(mcp_json_path)

    # --- .claude/settings.json ---
    claude_dir = agent_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"
    if not settings_path.exists():
        settings = {
            "permissions": {"allow": cc_allow_for_tier(tier), "deny": []},
            "env": {
                # Include Homebrew paths — node/npx live there on macOS and
                # stdio MCP servers (npx ...) silently fail to launch without it.
                "PATH": (
                    f"{engine_root}/.venv/bin:{Path.home()}/.local/bin"
                    ":/opt/homebrew/bin:/opt/homebrew/sbin"
                    ":/usr/local/bin:/usr/bin:/bin"
                ),
            },
        }
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        written.append(settings_path)

    return written
