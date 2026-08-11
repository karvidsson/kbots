# End-to-End Examples: From "Build Me a Tool" to Your Own Weights Running It

This doc walks the full kbots loop, several times, with concrete use cases:
an agent **builds a tool** in chat → you **collect training data** by using it →
you **train a model** on that data → the trained model **operates the tool**
through the harness's routing. Each example is a narrative that stitches
together the three concept docs — [CREATE_THEN_OPERATE.md](CREATE_THEN_OPERATE.md)
(build), [TRAINING.md](TRAINING.md) (collect/export/train), and
[LOCAL_MODELS.md](LOCAL_MODELS.md) (serve/route) — into one runnable story.

Prerequisites for all of them:

- Training collection enabled ([TRAINING.md §1](TRAINING.md#1-turn-on-collection)) —
  and then **weeks of real usage**. The dataset accumulates; there is no shortcut.
- A local runtime — Ollama or LM Studio ([LOCAL_MODELS.md](LOCAL_MODELS.md)).
- `pip install mlx-lm` for the LoRA stage (Apple Silicon; no GPU needed).

## The pipeline every example follows

| Stage | What happens | Where it's documented in depth |
|---|---|---|
| 1. Build | You describe a capability in chat; Claude writes the `@tool` (+ skill), you approve the source in a HITL card | [CREATE_THEN_OPERATE.md](CREATE_THEN_OPERATE.md) |
| 2. Collect | A single-tool *specialist skill* scopes every turn to that tool, tagging a clean per-tool corpus | `skills/tool_specialist.yaml.example`, [TRAINING.md §4](TRAINING.md#4-specialized-models--one-model-per-tool-or-per-skill) |
| 3. Train | Export a labeled slice, fine-tune | [TRAINING.md §2–§3](TRAINING.md#2-export-a-dataset) |
| 4. Operate | Route the trained model back in: skill pin, agent pin, or tier router | [TRAINING.md §5](TRAINING.md#5-what-you--and-the-harness--get-from-a-local-model), [LOCAL_MODELS.md](LOCAL_MODELS.md) |

One framing note, stated once so the examples don't repeat it. Training happens in
**two stages** with different jobs:

- **The from-scratch stage (optional, for understanding).** nanoGPT or
  [llm-from-scratch](https://github.com/angelos-p/llm-from-scratch) trained on your
  exported corpus produces a model that *mimics* your agents — same tags, same
  tool-call shapes — but does not reliably decide-and-call tools
  ([TRAINING.md — when it works](TRAINING.md#when-it-works--and-when-it-doesnt)).
  Do it to demystify the LoRA stage: once you've written the attention block
  yourself, fine-tuned weights stop being magic.
- **The LoRA stage (the operator).** MLX LoRA on a small instruct base
  (Qwen2.5-1.5B/3B) over the same slice produces the model that actually runs
  the tool ([TRAINING.md §3b](TRAINING.md#3b-lora-a-tool-using-model-on-your-mac-mlx-lm--the-real-goal)).

(The [hard mode](#hard-mode-the-from-scratch-model-operates-the-tool) section at the
end deliberately breaks this rule, and measures what happens.)

## Example 1: "Goodnight" — smart-home scenes on your own weights

The highest-payoff shape: a command you issue **many times a day**, standing at the
door or half-asleep, where a 5–15 s frontier round-trip feels absurd and a ~1 s
local answer feels like a light switch. It also keeps working when the internet —
or the LLM vendor — is down, because Shelly control is LAN RPC
(`extras/shelly/shelly.py`).

This is the interactive sibling of the scheduled water-pump monitor in
[CREATE_THEN_OPERATE.md](CREATE_THEN_OPERATE.md#the-loop-end-to-end): that one
runs on cron; this one answers you.

### The request

> **You:** We always say "goodnight", "movie time", "morning", or "we're leaving" —
> build me a scene tool on top of my Shelly devices, and a skill so a local model
> can run it.

### Stage 1 — Claude builds the tool

`create_tool` produces something like this (you see the full source in the HITL
approval card and react ✅):

```python
# agent-authored via create_tool — sketch of what lands in the approval card
@tool(description="Activate a smart-home scene across configured Shelly devices")
async def set_scene(
    ctx: ToolContext,
    scene: Annotated[str, {"choices": ["goodnight", "movie", "morning", "away"]}],
) -> str:
    # reads a scene → device-state map the agent adds to config.yaml, e.g.
    #   shelly:
    #     scenes:
    #       goodnight: {office_light: off, hall_light: off, heater: off, blinds: close}
    # then drives each device exactly like extras/shelly/shelly.py does.
    ...
    return "goodnight → 4 devices off, blinds down"
```

Note the `shelly.scenes` block is **part of the tool the agent wrote**, layered on
the existing `shelly.devices` registry — not a harness feature. The tool follows
the [operable-tool contract](CREATE_THEN_OPERATE.md#what-makes-an-authored-tool-operable)
to the letter: one enum parameter (the model *cannot* send an invalid scene), one
terse result line, devices resolved from config — the model picks names, never hosts.

### Stage 2 — scope collection with a skill

Copy `skills/tool_specialist.yaml.example` (or let Claude do it via `create_skill`):

```yaml
# skills/scene_specialist.yaml
name: scene_specialist
description: Activate smart-home scenes (runs on the local model)
parameters:
  request:
    type: string
    description: What scene to set, in plain language
    required: true
prompt: |
  You activate smart-home scenes. Request: {request}
  Rules:
  - Call `set_scene` with the right scene. Do not use any other tool.
  - Confirm what you did in one short sentence. Do nothing else.
tools: [set_scene]
restrict_tools: true
max_rounds: 3
llm:
  provider: local
  model: scene-specialist   # doesn't exist yet — see below
  fallback: true
```

The trick: **before** you've trained anything, `fallback: true` means every
`/scene-specialist` turn falls back to Claude — so what the collector records is
*Claude correctly using your tool*, tagged `skill: scene_specialist`. You're
farming the training set just by living with the thing.

### What the collected data looks like

Use it for a few weeks, react 👍/👎, and `<data_dir>/training/turns.jsonl` fills
with turns whose `nanogpt` export looks like:

```
<|user|>
movie time
<|tool_call|> set_scene {"scene": "movie"}
<|tool_result|> movie → 3 lights dimmed, blinds down
<|assistant|>
Movie scene is on.
<|endofturn|>
```

The `mlx`/`sft` exports carry the same turns as structured chat messages *plus the
tool schemas offered that turn* — train/inference parity
([TRAINING.md — what gets recorded](TRAINING.md#what-gets-recorded-per-turn)).

### Stage 3 — export and train

Wait until `--stats` shows a few hundred kept calls, then:

```bash
uv run python scripts/export_training_data.py --dir <data_dir>/training \
    --skill scene_specialist --successful-only --format mlx nanogpt --stats

mlx_lm.lora --model Qwen/Qwen2.5-1.5B-Instruct --train \
    --data <data_dir>/training --batch-size 1 --num-layers 8 --iters 400
mlx_lm.fuse --model Qwen/Qwen2.5-1.5B-Instruct --adapter-path adapters
mlx_lm.server --model fused_model        # or: ollama create scene-specialist -f Modelfile
```

Commands, flags, and hardware notes are TRAINING.md
[§2](TRAINING.md#2-export-a-dataset) and [§3b](TRAINING.md#3b-lora-a-tool-using-model-on-your-mac-mlx-lm--the-real-goal)
verbatim — nothing new here, this is just the moment in the story where you run them.

### Detour: watch a model you wrote mimic this skill

The same export produced `corpus.txt`. Before (or instead of) the LoRA, this is
the best from-scratch dataset you'll ever have — small, formulaic, and *yours*:

- **llm-from-scratch** — do the workshop (you write `model.py` / `train.py` /
  `generate.py` yourself; char-level; the ~10M-param medium config trains in
  ~45 min on a laptop), then swap Shakespeare for your agents:
  `cp <data_dir>/training/corpus.txt data/input.txt`, retrain, and prompt
  `generate.py` with `<|user|>\n`.
- **nanoGPT** — `scripts/nanogpt/prepare_kbots.py --char` +
  `scripts/nanogpt/config_finetune_kbots.py`, per
  [TRAINING.md §3a](TRAINING.md#3a-train-a-from-scratch-toy-style-mimic--nanogpt-or-llm-from-scratch).

Sampling produces an uncanny echo — `<|user|>`-tagged requests, plausible-looking
`<|tool_call|> set_scene {...}` lines — from ten million parameters you built by
hand. That's the point of the detour: it's the same objective, the same data
shape, and (after it) the LoRA stage stops being magic. It is **not** the
operator; the LoRA'd model above is.

### Stage 4 — wire it in

One line. Point the skill at the model you just served:

```yaml
# skills/scene_specialist.yaml — after training
llm:
  provider: local
  model: scene-specialist
  fallback: true            # local runtime down → the turn retries on Claude
```

That's the whole routing change. From now on, *only* scene turns run on your
weights — the skill pin takes precedence over everything else for those turns —
while every other conversation stays on Claude. `/admin usage` shows the
local-vs-Claude split moving.

### What you gain (rough numbers)

- **Latency:** ~1 s on Apple Silicon vs a 5–15 s frontier round-trip — for a
  thing you say while standing at the door.
- **Usage:** at ~5 scene commands a day, roughly 150 turns/month move off your
  subscription (~300k tokens/month at ~2k tokens per turn including the tool menu).
- **Resilience:** scenes keep working with the WAN down, during provider
  incidents, and after usage caps — LAN RPC plus your own weights need nobody.

## Example 2: The expense logger — private data, high volume

A different pressure: **privacy and volume**. "coffee 45", "taxi to airport 320,
work trip" — 10–30 of these a day, each a trivially formulaic extraction into one
structured call, each containing financial data that has no business leaving your
machine once a local model can handle it. This shape (free text → one call with an
enum) is the single best fit for a 1.5B specialist, and its frequency means it
accumulates trainable data faster than any other tool you own.

### The request

> **You:** Build me an expense logger — I'll message things like "lunch 180" or
> "taxi 320, work" and it should append to a CSV in your workspace with a category.

### Stage 1 — Claude builds the tool

```python
# agent-authored via create_tool — sketch
@tool(description="Log a personal expense to the ledger")
async def log_expense(
    ctx: ToolContext,
    amount: float,
    category: Annotated[str, {"choices": ["food", "transport", "household", "fun", "other"]}],
    note: str = "",
) -> str:
    # appends a timestamped row to ledger.csv in the agent workspace
    ...
    return "logged 320.00 transport — month total 4,120"
```

Three parameters (the contract's ceiling), one enum, no paths or secrets as
arguments. The running total in the result line is deliberate: it gives you an
instant sanity check, and gives the model a terse result to confirm.

### Stage 2 — scope collection with a skill

Same template as Example 1 — `expense_specialist.yaml` with `tools: [log_expense]`,
`restrict_tools: true`, `max_rounds: 2` (parse → call → confirm; nothing to
explore), `llm: {provider: local, model: expense-specialist, fallback: true}`.
Weeks of Claude-via-fallback doing the categorizing = your labeled dataset. React
👎 when it picks a category you disagree with — that label is training signal.

### Stage 3 — export and train

This example slices by **the call itself** rather than the skill — the exporter's
other axis, useful because expenses might also arrive through normal chat with
your main agent, not just the skill:

```bash
uv run python scripts/export_training_data.py --dir <data_dir>/training \
    --tool log_expense --successful-only --format mlx --stats
mlx_lm.lora --model Qwen/Qwen2.5-1.5B-Instruct --train \
    --data <data_dir>/training --iters 400
```

Then fuse and serve as in Example 1. (Optional: a KTO pass on your 👍/👎 teaches
it your category *taste*, not just the mapping — [TRAINING.md §3d](TRAINING.md).)

### Stage 4 — wire it in

Set the skill's `llm.model: expense-specialist`. Done.

### What you gain (rough numbers)

- **Privacy:** amounts, merchants, and spending habits never leave the box again.
- **Usage:** ~20 messages/day ≈ 600 frontier turns/month (~1.2M tokens) saved —
  the highest-volume win in this doc.
- **Latency:** sub-second logging; reacting faster than you can put the receipt away.

## Example 3: Composing specialists — a fully-local home agent

Once Examples 1 and 2 exist, compose them. Export the union slice and train one
model on both tools:

```bash
uv run python scripts/export_training_data.py --dir <data_dir>/training \
    --tool set_scene log_expense --successful-only --format mlx
```

LoRA as before, serve it as `home-specialist`, then use the third routing
mechanism — the **agent pin** ([LOCAL_MODELS.md](LOCAL_MODELS.md#agent-on-a-local-model)):

```yaml
# agents.yaml — a small dedicated agent that runs entirely on your weights
home:
  description: Household helper (scenes + expenses), fully local
  tools: [set_scene, log_expense]
  llm: {provider: local, model: home-specialist}
```

A whole agent whose (two-tool) world runs on weights you trained, with the
engine still enforcing HITL, rate limits, and allowlists around it — the model
was never the security boundary. Alternatively, keep your main agent and set the
model as the tier router's local workhorse (`defaults.llm.router.local_model`) so
confidently-simple turns land on it automatically —
[LOCAL_MODELS.md](LOCAL_MODELS.md#tier-routing-save-claude-usage-keep-claude-quality).

## Hard mode: the from-scratch model operates the tool

Everything above uses the from-scratch model as a learning detour, because
[TRAINING.md](TRAINING.md#when-it-works--and-when-it-doesnt) is right: a char-level
GPT will not reliably decide-and-call tools. This section doesn't dispute that —
it **shrinks the problem** until a toy can sometimes clear the bar, and measures
how often. One tool, one four-value enum, short formulaic inputs, and a validator
that makes every miss safe. An experiment for understanding, not a recommendation;
Example 1's LoRA path is the real thing.

### What you already have

- **The DSL is free.** The `nanogpt` export of your `scene_specialist` slice *is*
  the training corpus — `<|user|>` request, `<|tool_call|> set_scene {json}`,
  `<|endofturn|>`. No preprocessing. Train exactly as in the
  [detour](#detour-watch-a-model-you-wrote-mimic-this-skill).
- **The harness side is free.** The `local` provider (`src/llm/openai_compat.py`)
  speaks `/v1/chat/completions` to any `base_url` and parses `tool_calls` out of
  the response, dispatching them through the normal engine path — allowlists,
  HITL, audit, everything. And a pinned skill whose provider *errors* retries the
  turn on Claude (`fallback: true`).

### What you have to write: a ~50-line shim

The one missing piece — and it's yours to write, it does not ship with kbots —
is an OpenAI-compatible HTTP wrapper around your trained checkpoint (FastAPI or
Flask, one endpoint). Per request:

1. Take the last `user` message from the incoming `messages`.
2. Prompt your model with `<|user|>\n{message}\n`, sample to `<|endofturn|>` at
   low temperature.
3. Regex-parse the first `<|tool_call|> set_scene {…}` line; **validate** the JSON
   and that `scene` is one of the four enum values.
4. **Valid** → respond with the standard shape the provider expects:
   ```json
   {"choices": [{"message": {"content": "",
       "tool_calls": [{"function": {"name": "set_scene",
                                    "arguments": "{\"scene\": \"movie\"}"}}]},
     "finish_reason": "tool_calls"}]}
   ```
5. **Invalid** (no parse, bad JSON, unknown scene) → return HTTP 500. That
   surfaces as a provider error, which triggers the skill's *real* fallback path —
   Claude quietly handles the turn.
6. Second round (the engine sends the tool result back): don't let the toy
   freestyle — echo the tool's terse result line as the assistant message.

### Wiring

A copy of the skill pinned to the shim, and the provider pointed at it:

```yaml
# skills/scene_toy.yaml — as scene_specialist, but:
llm: {provider: local, model: scene-toy, fallback: true}
max_rounds: 2
```

```yaml
# config.yaml — point the local provider at the shim while you experiment
defaults:
  llm:
    local:
      base_url: http://localhost:8123/v1
```

Note `base_url` is the *provider's* endpoint — while it points at the shim, every
local-pinned turn goes there, so run this experiment when nothing else is pinned
local (or give the shim a pass-through to your normal runtime). The important
part: **nothing was added to the harness**. As far as kbots is concerned, your
hand-written GPT behind a hand-written shim is just another OpenAI-compatible
runtime.

### Honest expectations

With a few hundred training turns it will nail phrasings near the training
distribution ("movie time", "goodnight") and miss novel ones ("we're off, kill
everything"). Measure it — `scripts/eval_skill.py` automates exactly this: hold
out ~20 phrasings as fixtures (including *trap* fixtures that must NOT trigger
the tool — an idea adapted from
[fable-method](https://github.com/Sahir619/fable-method), MIT) and score the
proposed calls without ever executing them:

```bash
uv run python scripts/eval_skill.py --skill scene_toy --fixtures holdout.jsonl
# holdout.jsonl:
#   {"input": "we're heading up", "expect_tool": "set_scene", "expect_args": {"scene": "goodnight"}}
#   {"input": "what scenes are there?", "expect_no_tool": true}
```

The same command scores the LoRA'd specialist (Example 1) — run it on both. The
design makes misses cheap — enum validation plus fallback means a failure costs
one Claude turn and can never actuate a wrong scene. And yes, Example 1's 1.5B
LoRA makes this model obsolete as an operator the moment it exists. The payoff is
different: a model whose every line you wrote, calling a tool your agent wrote,
through a harness that can't tell the difference.

## Picking your own use case

The examples generalize. A task is a good candidate when most of these hold:

- **Pressure that pays**: high frequency, latency-sensitive, privacy-sensitive,
  must survive offline/outages, or eating your usage cap.
- **One tool, 1–3 parameters, enums wherever possible** — the
  [operable-tool contract](CREATE_THEN_OPERATE.md#what-makes-an-authored-tool-operable).
  If the tool isn't operable by a small model, no fine-tune will save it.
- **Short, formulaic requests** — a 1.5–3B model learns a mapping, not judgment.
  "taxi 320" trains; "should I recategorize last month's travel?" doesn't.
- **A few hundred positively-labeled turns within reach** — run the export with
  `--stats` and look at *tool calls (kept)*; that number tells you when you're ready.

Then it's always the same four stages: build (once, with Claude), collect (weeks,
for free, via `fallback: true`), train (an afternoon), route (one line of YAML).

## See also

- [CREATE_THEN_OPERATE.md](CREATE_THEN_OPERATE.md) — the concept these examples instantiate, the operable-tool contract, the three execution tiers, the safety model
- [TRAINING.md](TRAINING.md) — collection, export formats and filters, every training path in depth
- [LOCAL_MODELS.md](LOCAL_MODELS.md) — runtimes, model picks per RAM, the tier router
- [skills/README.md](../skills/README.md) — skill format: `llm:`, `restrict_tools`, `max_rounds`
