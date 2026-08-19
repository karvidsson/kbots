# Skills

A skill is a prompt template written in YAML that the engine exposes as a Discord slash command. Adding a `.yaml` file to this directory is all it takes — hot reload picks it up right away, and a restart registers it in any case.

## Format

```yaml
name: my_skill
description: Short description shown in Discord slash command list
parameters:
  - name: topic
    type: string
    required: true
    description: What to work on
  - name: depth
    type: string
    choices: [brief, detailed, comprehensive]
    required: false
    description: Level of detail
prompt: |
  Analyse {topic} at a {depth} level.
  Search memory for relevant context first.
tools:
  - web_search
  - memory_search
```

## Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Identifier for the skill — lowercase with underscores. Doubles as the slash command name. |
| `description` | Yes | Single-line summary Discord displays in its command picker. |
| `parameters` | No | Parameter list. Every entry carries `name`, `type`, and `description`; `required` (false unless set) and `choices` are optional. |
| `prompt` | Yes* | Template for the prompt; write `{param_name}` wherever a parameter value should be substituted. |
| `command` | No | A shell command executed directly, skipping the LLM entirely. Cannot be combined with `prompt`. |
| `tools` | No | MCP tools this skill relies on. Leaving it out gives the skill everything the agent already has. |
| `restrict_tools` | No | When `true`, the turn's toolset is `tools` ∩ the agent's allowlist (a narrow, scoped set — recommended for local-model skills). Default: skill tools are added to the agent's set. |
| `max_rounds` | No | Per-skill tool-round cap (default: engine limit of 10). |
| `llm` | No | Pin this skill's turns to a provider: `llm: {provider: local, model: qwen3.5:9b}`. `fallback: true` (default) retries once on the agent's default provider if the pinned one errors. See [docs/LOCAL_MODELS.md](../docs/LOCAL_MODELS.md). |

## Direct Command Execution

A skill doesn't have to involve the LLM at all — it can execute a shell command on its own. To do that, replace `prompt` with a `command` field:

```yaml
name: system_audit
description: Run a full system health audit
command: scripts/health-audit.sh --report
```

With `command` set:
- The engine launches it as a subprocess and puts `KBOTS_HOME` in its environment
- Whatever the command prints is posted to the Discord channel as a message
- There's no LLM session at all, which makes it both quicker and cheaper
- Parameters aren't available in this mode; a skill that needs them must use `prompt`

It's a good fit for operational scripts whose output is already formatted for humans.

## Parameter Types

- `string` — free-form text
- `string` plus a `choices` list — rendered by Discord as a dropdown

## How It Works

1. At startup, `src/core/skills.py` walks both `skills/*.yaml` and `agents/*/skills/*.yaml`
2. Every skill it finds becomes a Discord slash command, parameters included
3. A skill carrying a `command` field executes that command and returns its output — the LLM never runs
4. For prompt skills, user input is substituted into the `{param_name}` placeholders
5. The filled-in prompt then goes to the agent's LLM, with the listed tools enabled

## Agent-Specific Skills

A skill dropped into `agents/<name>/skills/` belongs to that agent alone. To keep names from colliding, such skills receive an automatic prefix (e.g., `kbots:my_skill`).

## Multi-Step Interviews

A skill can drive a conversation across many turns: the channel session persists, so a prompt that says "ask ONE question, then STOP FOR THE USER" continues naturally when the user answers. `map_process.yaml` / `wardley_map.yaml` use this with `process_model_save` patch-merging each answer into a saved model, and `process_questions.yaml` turns the same gap analysis into a coach for live workshops.

## Creating Skills via Chat

An agent can also build a skill mid-conversation through its `create_skill` tool:

```
User: "Create a skill that analyses competitor pricing"
Agent: [calls create_skill] → writes YAML to skills/ → available as /competitor_pricing
```

## Examples

This directory contains `example.yaml` and `code_review.yaml` as working references.
