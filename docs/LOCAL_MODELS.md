# Local Models

Run agents on local models — via **Ollama** or **LM Studio** — either as an agent's
main LLM or as a **tier router** that answers simple requests locally and sends
everything else to Claude Code, saving subscription usage.

Both runtimes expose the same OpenAI-compatible API, so kbots ships **one provider
(`local`)** that covers both: leave `base_url` unset and it auto-detects whichever is
running (Ollama `:11434` first, then LM Studio `:1234`).

## Runtimes per OS

| OS | Recommendation |
|---|---|
| macOS (Apple Silicon) | **Ollama** (official MLX acceleration, `-mlx` model tags) or **LM Studio** (MLX engine, GUI model manager). Either works — auto-detected. |
| Linux server / VPS | **Ollama** — battle-tested on CPU, systemd-friendly. (LM Studio 0.4+ has headless Linux too.) |

## Which models? (verified July 2026, all with Ollama `tools` support)

| Your machine | Router (tiny, always loaded) | Workhorse (simple requests) |
|---|---|---|
| 8GB VPS / CPU-only | `granite4.1:3b` | `qwen3.5:4b` |
| 16GB Mac / 16GB VPS | `qwen3.5:2b` | `qwen3.5:9b` |
| 32GB+ Mac | `qwen3.5:4b` | `qwen3.6:35b-a3b` (MoE — fast, ~22GB) |
| 64GB Mac (max quality) | `qwen3.5:4b` | `qwen3.6:35b-a3b` Q8 or `gemma4:31b` |

Notable alternatives: `gemma4:26b-moe` (light on RAM for its quality), `gpt-oss:20b`
(fits 16GB, older), `glm-4.7-flash` (coding slant — verify tool support with
`ollama show`). The setup wizard detects your RAM and offers this table as a one-key
choice, then pulls the models for you.

```bash
ollama pull qwen3.5:2b qwen3.5:9b     # or let the wizard do it
```

## Agent on a local model

```yaml
# agents.yaml — this agent runs entirely on the local model
myagent:
  llm: {provider: local, model: qwen3.5:9b}
```

The agent gets the full kbots toolset — tool calls are executed by the engine with
rate-limits, access control, and HITL enforced, same as Claude agents. History comes
from SQLite; the agent's CLAUDE.md is injected as the system prompt.

## Tier routing (save Claude usage, keep Claude quality)

```yaml
# config.yaml — applies to all agents (or set per-agent under llm.router)
defaults:
  llm:
    provider: claude_code
    model: opus
    local:
      model: qwen3.5:9b          # default local model
      # base_url: http://localhost:11434/v1   # unset = auto-detect
    router:
      enabled: true
      router_model: qwen3.5:2b   # tiny classifier
      local_model: qwen3.5:9b    # local workhorse
      confidence: 0.75           # min confidence to stay local
```

**Quality-first policy** — a request stays local only when the classifier is confident
it's simple (greetings, thanks, quick factual Q&A, rewording text already in the
message). Everything else goes to Claude: attachments, code blocks, long messages
(>600 chars, configurable via `max_chars`), scheduled tasks, low confidence,
classifier errors/timeouts, or the runtime being down (fail-open). Every decision is
logged: `[main] tier-router → local (simple @0.92)`.

Context is safe across switches: all turns are stored in SQLite; when a conversation
escalates back to Claude after local turns, Claude rebuilds from stored history.

## Scoped local tasks + zero-LLM automations

> **The concept behind this section — why big models should build the tools and
> small models should run them — is explained in
> [CREATE_THEN_OPERATE.md](CREATE_THEN_OPERATE.md). Read that first.**

Three tiers for repetitive work, cheapest first:

**1. Tool-direct (no AI at all)** — bind a schedule/trigger straight to a tool:
```
schedule_task(cron='0 22 * * *', tool='shelly_switch',
              tool_args='{"device": "office_light", "state": "off"}')
create_trigger(event='office_button', tool='shelly_switch', tool_args='...')
```
Sub-second, zero tokens, works with the runtime down; HITL/rate-limits/audit
still apply. `narrate=true` adds a one-line local-model summary.

**2. Skill on the local model (NL flexibility, scoped tools)** — a skill can pin
`llm: {provider: local, model: qwen3.5:9b}` with `restrict_tools: true` and
`max_rounds: N`. Small models are reliable exactly when the toolset is narrow
(2-3 tools with enum params). Ships as examples: `skills/shelly_control.yaml.example`
(`/shelly-control request:"office lights off"`) and `skills/compile_report.yaml.example`.
If the local runtime errors mid-task, the skill falls back to Claude once
(`fallback: false` to disable).

**3. Free-text on Claude** — everything open-ended, unchanged (quality-first).

**Shelly setup**: register devices under `shelly.devices` in config.yaml (names,
not IPs, are what agents use; LAN hosts only; optional vault password
`shelly_<name>`). `shelly_switch`/`shelly_status` run ungated; `shelly_cover`
requires approval — tighten with `security.hitl.gated_tools: ["shelly_*"]`.

**Reports**: author a spec once (interactively, with Claude), save it in the
agent workspace, then schedule `compile_report` — the pipeline (load → charts →
PDF) is deterministic; only the narration is generative.

## VPS tuning (CPU-only)

```bash
OLLAMA_MAX_LOADED_MODELS=1    # don't hold router + workhorse in RAM together
OLLAMA_CONTEXT_LENGTH=8192    # default is 4096; raise if RAM allows
# keep_alive: the router model is tiny — keep it warm; let the workhorse unload
```

## Troubleshooting

- Boot preflight prints a `local_models:` line — `✓` when the runtime is reachable and
  the configured models are downloaded, or a `⚠` with the exact fix
  (`ollama pull <model>` / start the runtime).
- Missing models **auto-download on first use** (Ollama, `auto_pull: true` default) —
  the first routed message just takes longer while the model pulls; set
  `auto_pull: false` to require explicit `ollama pull`.
- Runtime down mid-session? Routed agents fall back to Claude automatically; agents
  with `provider: local` return a friendly error until it's back.
- Models evolve fast — the table above is July 2026. Check `ollama.com/search?c=tools`
  for current tool-capable models before changing config.
