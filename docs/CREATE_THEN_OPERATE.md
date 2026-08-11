# Create-Then-Operate: Big Models Build the Tools, Small Models Run Them

The most expensive thing in an agent system is frontier-model intelligence. The
biggest waste in an agent system is spending that intelligence on the same task
twice.

**Create-then-operate** is kbots' answer: a large model (Claude) does the
*engineering* — once — and a small local model (or no model at all) does the
*operating* — forever. Ask your agent to build a capability in chat; after one
human approval it becomes a tool + a scoped skill that runs on your own hardware
at zero marginal cost, on a schedule, on a webhook, or on demand.

```
                      ONCE (frontier intelligence)          FOREVER (free)
                ┌──────────────────────────────────┐   ┌─────────────────────┐
 "build me a    │  Claude designs & writes the     │   │  local model drives │
  tool that…"──▶│  TOOL (code, HITL-reviewed)      │──▶│  the tool on demand │
                │  + the SKILL (operating manual,  │   │  / cron / webhook — │
                │  pinned to the local model)      │   │  or zero-LLM direct │
                └──────────────────────────────────┘   └─────────────────────┘
```

## Why this matters

**1. The economics invert.** A repetitive task ("check X every morning",
"switch Y when the sensor fires", "compile the weekly report") normally costs
frontier-model tokens *every single run* — the same reasoning, re-purchased
daily. With create-then-operate, the reasoning is spent once at design time.
The runs cost nothing: they execute on your Mac/VPS via Ollama, or skip the
model entirely.

**2. Reliability comes from narrowing, not from bigger models.** A 9B local
model is unreliable when handed 40 tools and an open-ended request — and
*very* reliable when handed 2-3 purpose-built tools with enum-constrained
parameters and a 5-line operating prompt. The large model's real job is
building that narrow world: a tool with self-correcting errors, a skill that
scopes the turn to exactly the tools it needs (`restrict_tools`), a round cap
so nothing thrashes. Design intelligence is *encoded into the artifact* so
execution doesn't need it.

**3. Capability compounds instead of evaporating.** A clever Claude answer
helps you once and is gone. A tool + skill is an asset: it appears as a slash
command, agents can schedule it, webhooks can fire it, other agents can be
granted it (`promote_tool`). Your platform gets permanently more capable with
each request — this is how the system *grows* rather than just responds.

**4. Independence and resilience.** Operated tasks keep working during Claude
usage caps, API incidents, or subscription policy changes. And if the *local*
runtime is down, pinned skills fall back to Claude automatically — the task
never silently dies from either side.

**5. It feeds the training flywheel.** Every local-operated turn (prompt →
tool calls → outcome → your 👍/👎) is captured by the training collector. That
is exactly the dataset you'd use to fine-tune your own operator model
(docs/TRAINING.md) — the system generates the curriculum for its own
replacement parts.

## The loop, end to end

A real conversation with your main agent in Discord:

> **You:** Build me a tool that checks the water level API at my summer house
> pump, and a local skill that runs it every morning at 7 and warns me if it's
> above 80cm.

**Step 1 — Claude writes the tool** (`create_tool`). You get a HITL approval
card in Discord *showing the full source code*. The tool follows the
design-for-local-operation contract (below): one `check_water_level()` call,
terse output, config-driven endpoint. You react ✅.

**Step 2 — Claude writes the operating manual** (`create_skill` with
`run_on_local=True`): a skill whose prompt is a 4-line instruction, whose
toolset is *only* `check_water_level`, capped at 3 tool rounds, pinned to your
local model. It's now `/check-water` in Discord.

**Step 3 — Claude schedules it** (`schedule_task(cron='0 7 * * *',
skill='check_water')`). Done. From tomorrow, qwen3.5-9B on your own machine
runs the check and posts one line to your channel. Claude is out of the loop —
your subscription is untouched.

**Step 4 (when even a small model is too much) — go zero-LLM.** If the task
needs no language at all ("when the pump webhook fires, run the tool with
these exact args"):

```
create_trigger(event='pump_alert', tool='check_water_level',
               tool_args='{"verbose": true}', narrate=true)
```

The tool runs deterministically — sub-second, zero tokens, works with every
model offline. `narrate=true` optionally lets the local model phrase the
result nicely.

## The three execution tiers

Always use the cheapest tier that can do the job:

| Tier | What runs | Cost / latency | Use for |
|---|---|---|---|
| **Tool-direct** | No model. Schedule/trigger executes ONE tool with fixed args | ~0s, 0 tokens, works offline | Fixed automations: sensor→switch, nightly job, exact pipelines |
| **Skill on local model** | Small local model + the skill's 2-3 tools only | seconds, $0 | NL flexibility over a narrow capability: "/check-water", "turn off the office lights" |
| **Claude** | Frontier model, full toolset | subscription usage | Everything open-ended — including *designing the two tiers above* |

The quality-first tier router (docs/LOCAL_MODELS.md) already keeps interactive
chat on the right tier; create-then-operate is how you deliberately *move* a
recurring task down the table.

## What makes an authored tool "operable"

The contract `create_tool` instructs large models to follow — the difference
between a tool a 9B can drive and one it fumbles:

- **1-3 parameters, max.** Every extra parameter is a hallucination surface.
- **Enums over free text**: `Annotated[str, {"choices": ["on", "off"]}]`
  becomes a JSON-schema enum — the model *cannot* send an invalid value.
- **One terse result line** ("office_light → ON (was off), 8.2W"), not a JSON
  dump — long outputs swamp small contexts.
- **Self-correcting errors**: an unknown input returns the *valid options*
  ("Unknown device 'offce_light'. Configured: office_light, heater") so the
  model fixes itself in one round.
- **No raw hosts/URLs/secrets as parameters** — endpoints live in config,
  credentials in the vault, read inside the tool. The model chooses *names*,
  never addresses.

`extras/shelly/shelly.py` and `compile_report` are the reference implementations.

## What makes an authored prompt "operable"

The sibling contract `create_skill` instructs large models to follow — a skill
prompt is an *operating manual* for a small model, not an essay:

- **Numbered steps, one tool call per step** — no branching prose.
- **The last step states what "done" looks like.**
- **A tool call for every factual claim** — the operator never answers from recall.
- **Verify by observation**: check the tool *result* before confirming.
- **Confirm in ONE short sentence**; keep the whole prompt **≤ ~10 lines** —
  small contexts drown in long prompts (`create_skill` warns past 12).
- **For each `{param}`, name its valid values in the prompt text** (skill
  params are plain strings — the prompt is where the constraint lives).

New agents get the same discipline baked into their scaffolded CLAUDE.md (the
"How to work" loop). Existing agents' CLAUDE.md files live in the overlay and
are never rewritten by core — copy the section in by hand if you want it.

*The method loop and prompt contract are adapted from
[fable-method](https://github.com/Sahir619/fable-method) (MIT).*

## The safety model (unchanged, everywhere)

Nothing in this loop bypasses the platform's controls:

- **Creation is gated**: `create_tool` requires human approval with the source
  in the approval card; tools are AST-validated (no exec/subprocess/etc.) and
  private to the creating agent until explicitly promoted.
- **Operation is gated**: local-model and zero-LLM executions go through the
  same dispatch path as Claude — agent allowlists, rate limits, HITL on gated
  tools, audit logging. A skill can only *narrow* an agent's toolset.
- **Bounded**: per-skill `max_rounds` caps runaway loops; killswitches
  (`/admin triggers off`, schedules toggle) stop all automation instantly.
- **Fallback**: a pinned skill whose local runtime errors retries once on the
  agent's default provider (`fallback: false` to disable).

## Watching it pay off

`/admin usage` shows the running score:

```
Local vs Claude (7 days)
  stayed local: 41 · to Claude: 57 · local ok/err: 39/2
  zero-LLM actions: 12 · fallbacks to Claude: 3
```

Every "stayed local" and every "zero-LLM action" is a task that used to need —
or would have needed — frontier tokens.

## See also

- [docs/E2E_EXAMPLES.md](E2E_EXAMPLES.md) — worked end-to-end examples of this loop, through to a trained specialist operating the tool
- [docs/LOCAL_MODELS.md](LOCAL_MODELS.md) — runtimes, model picks per RAM, the tier router
- [docs/TRAINING.md](TRAINING.md) — turning operated turns into fine-tuning data
- [skills/README.md](../skills/README.md) — skill format incl. `llm:`/`restrict_tools`/`max_rounds`
