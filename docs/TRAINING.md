# Training a local model on the agents' work

kbots can collect its own agent turns — the prompt, the full tool-call trace, the
final response, and outcome signals (👍/👎, HITL, tool errors) — into a dataset you can
later use to train a local model. This doc covers collection → export → training →
serving the result back to the harness (§5). For complete worked examples of the
whole pipeline — from `create_tool` to a trained specialist operating the tool —
see [docs/E2E_EXAMPLES.md](E2E_EXAMPLES.md).

## Why train your own?

The engine *is* Claude. Every turn you collect is **Claude correctly using your tools**.
Fine-tuning a small open model on that data is **distillation**: you capture Claude's
competence with *your* toolbox and keep it — as weights you own. Concretely:

- **Cost & latency.** Routine tool calls (turn a light off, look up a record, send a
  templated message) run locally — free, instant, no API round-trip. Reserve Claude for
  the hard turns.
- **Privacy & offline.** A local model never leaves the box; the agent keeps working
  with the network down.
- **It compounds.** The more you use the agents (and react 👍/👎), the better the
  dataset gets — the system trains itself on its own good work.

### When it works — and when it doesn't

- **You need data.** A fresh install has almost none. The collector *accumulates*
  labeled turns over weeks/months. Exporting today produces a tiny file that trains
  nothing useful. Collect first.
- **From-scratch models (nanoGPT, llm-from-scratch) are toys.** They learn to
  *continue text* — they will mimic the agents' *style* and echo the shape of tool
  calls, but they will **not** reliably decide-and-call tools. Great for learning what
  a GPT is, not a worker (§3a).
- **A tool-using model needs a real base.** The `sft`/`mlx`/`openai` exports preserve
  tool-call structure *and* the available-tool menu, so with enough *positively-labeled*
  turns you can LoRA-fine-tune a small modern instruct model (Qwen2.5-1.5B/3B/7B,
  Llama-3.x) into something that actually calls your tools (§3b). Because kbots is
  LLM-agnostic and *is* the tool harness, that model drops straight in via the `local`
  provider and inherits every tool.
- **Specialize to win.** A tiny model rarely masters the *whole* toolbox — but it can
  master **one tool** extremely well. That's the highest-success path; see
  [Specialized models](#4-specialized-models--one-model-per-tool-or-per-skill).

## 1. Turn on collection

In `config.yaml` under `kbots:` (off by default — it stores full conversation content
locally; secrets are redacted):

```yaml
kbots:
  training_collection:
    enabled: true
    include_tool_trace: true
```

Restart (`/admin reboot` or `scripts/self-deploy.sh`). Every agent turn now appends to
`<data_dir>/training/turns.jsonl`; 👍/👎 reactions append to `rewards.jsonl`.

**Give feedback** — react 👍/👎 on agent replies in Discord. Those become the reward
labels that let you export "only the good turns," which is what makes fine-tuning work.

### Auto-labeling with the judge (optional)

Reactions are the best labels but arrive slowly. The **turn judge** (default off)
is a background pass that periodically re-reads collected turns and has a cheap
model verdict each one *adversarially* — it treats the reply's claims as
unverified and judges only from the tool trace: did the right tools run, did
they succeed, does the result satisfy the ask? Verdicts land in
`judgments.jsonl` as **machine labels** — deliberately weaker than your
reactions (a human 👍/👎 on the same turn always wins at export, and judge
labels carry half magnitude). Adapted from
[fable-method](https://github.com/Sahir619/fable-method)'s judge (MIT).

```yaml
# config.yaml, under kbots.training_collection
    judge:
      enabled: true
      provider: claude_code   # judge with a competent model even for local-agent turns
      model: haiku
      interval_hours: 6
      max_turns_per_run: 50
      min_age_minutes: 60     # give humans first shot at reacting
```

Use them at export time with `--labels judge` or `--labels any` (§2); the
default `--labels human` ignores them entirely.

## 2. Export a dataset

```bash
uv run python scripts/export_training_data.py --dir <data_dir>/training \
    --format nanogpt sft mlx openai dpo --stats
# filters (apply to every format, and compose):
#   --agent research           only one agent
#   --skill tool_specialist  only turns run under one skill (a tool-scoped subset)
#   --tool send_email ...     only turns that actually CALLED one of these tools
#   --positive-only          only 👍 / non-degraded turns
#   --successful-only        drop turns with tool errors or degraded stop reasons
#   --min-reward 0.5 --since 2026-08-01
#   --labels any             also use judge auto-labels (§1; default: human only)
```

`--tool`/`--skill` are what make **per-tool specialist** datasets a one-liner —
`--tool send_email --successful-only` yields clean positive examples of that tool in
use. `--stats` prints a per-tool call count of the kept slice so you can see which
tools have enough data to train on yet. See
[Specialized models](#4-specialized-models--one-model-per-tool-or-per-skill).

`--format` picks the output(s) (default `nanogpt sft`):
- **`nanogpt`** → `corpus.txt` — role-tagged text for nanoGPT (§3a).
- **`sft`** → `sft.jsonl` — generic `{messages, reward, agent}` (any trl/Unsloth/Axolotl flow).
- **`mlx`** → `train.jsonl` + `valid.jsonl` — MLX-LM chat schema, LoRA on your Mac (§3b).
- **`openai`** → `openai.jsonl` — OpenAI tool-call schema, upload to a hosted fine-tune (§3c).
- **`dpo`** → `preference.jsonl` (KTO binary from 👍/👎) + `dpo_pairs.jsonl` (chosen/rejected
  where a prompt got both) — preference tuning (§3d).

## 3a. Train a from-scratch toy (style mimic) — nanoGPT or llm-from-scratch

### nanoGPT (fine-tune GPT-2 on your corpus)

```bash
git clone https://github.com/karpathy/nanoGPT && cd nanoGPT
pip install torch numpy tiktoken            # torch is NOT a kbots dependency

# prepare our corpus into token bins (GPT-2 BPE; use --char for a char-level toy)
python ../kbots/scripts/nanogpt/prepare_kbots.py \
    --corpus ../kbots/<data_dir>/training/corpus.txt --out data/kbots
cp ../kbots/scripts/nanogpt/config_finetune_kbots.py config/

# fine-tune GPT-2 on it — Apple Silicon:
python train.py config/config_finetune_kbots.py --device=mps --compile=False
python sample.py --out_dir=out-kbots --device=mps --start="<|user|>\n"
```

On an M-series Mac this runs (slowly) via Metal (MPS). Tiny data → it will overfit fast;
keep `max_iters` low. Again: expect a style echo, not a working agent.

### …or build the GPT yourself first: llm-from-scratch

[angelos-p/llm-from-scratch](https://github.com/angelos-p/llm-from-scratch) is a
workshop repo: you write `model.py` / `train.py` / `generate.py` yourself and end up
with a ~10M-param **char-level** GPT that trains on a laptop in under an hour (~45 min
on an M3 Pro at the medium 6-layer config; picks MPS/CUDA/CPU automatically). It
expects a single plain-text file — exactly what the `nanogpt` export emits — so after
finishing the workshop on Shakespeare, retrain on your agents:

```bash
git clone https://github.com/angelos-p/llm-from-scratch && cd llm-from-scratch
# ...follow the workshop to write model.py / train.py / generate.py...
cp ../kbots/<data_dir>/training/corpus.txt data/input.txt  # swap Shakespeare for your agents
python train.py
python generate.py            # prompt with "<|user|>\n" to sample turn-shaped output
```

Char-level is the right call at this corpus size — the workshop's own reasoning (a 50k
BPE vocab starves on a megabyte of text) is why our nanoGPT prep script has a `--char`
mode too. Expectations as above: an uncanny style echo of your agents, not a
tool-caller. The payoff is understanding — once you've written the attention block and
training loop yourself, the LoRA weights you produce in §3b stop being magic.

## 3b. LoRA a tool-using model on your Mac (MLX-LM) — the real goal

No GPU needed; runs on Apple Silicon. The `mlx` export is drop-in.

```bash
pip install mlx-lm
uv run python scripts/export_training_data.py --dir <data_dir>/training \
    --format mlx --positive-only            # writes train.jsonl / valid.jsonl
mlx_lm.lora --model Qwen/Qwen2.5-3B-Instruct --train \
    --data <data_dir>/training --batch-size 1 --num-layers 8 --iters 600
mlx_lm.fuse   --model Qwen/Qwen2.5-3B-Instruct --adapter-path adapters   # merge LoRA
mlx_lm.generate --model fused_model --prompt "..."
```

The `messages` carry `user → assistant(tool_calls) → tool → assistant`, the shape MLX's
chat template trains on. Once it's good, wrap it as a new provider in `src/llm/` and point
an agent at it — kbots is LLM-agnostic, so it inherits every tool.

## 3c. Hosted fine-tune (no local hardware) — OpenAI / Together

```bash
uv run python scripts/export_training_data.py --dir <data_dir>/training \
    --format openai --positive-only          # writes openai.jsonl (tool_calls + tool_call_id)
# OpenAI: files.create(purpose=fine-tune) → fine_tuning.jobs.create(model="gpt-4o-mini", ...)
# Together/Fireworks: same JSONL, their upload+finetune CLI/API.
```

## 3d. Preference tuning from your 👍/👎 — KTO / DPO

Your reactions are *binary* (thumbs), so **KTO** is the right algorithm (trl `KTOTrainer`);
plain DPO needs same-prompt chosen/rejected pairs, which the exporter emits when they exist.

```bash
uv run python scripts/export_training_data.py --dir <data_dir>/training --format dpo
#   preference.jsonl : {"prompt","completion","label": true|false}   → trl KTOTrainer / ORPO
#   dpo_pairs.jsonl  : {"prompt","chosen","rejected"}                → trl DPOTrainer (when present)
```
Run either on top of the SFT model (SFT first, then preference-tune) for a model that reflects
your taste, not just imitation.

## 4. Specialized models — one model per tool (or per skill)

This is the highest-success path, and it's what the collector is quietly built for.

### Why the data makes this possible

kbots doesn't just log text — every turn is recorded **structured and labeled**, so
the corpus is *sliceable* along exactly the axes you'd want to specialize on:

- **`tools_available`** — the full tool menu offered that turn (OpenAI function schemas),
  the same one the model sees at inference.
- **`tools`** — the trace of which tools *actually fired*, with their args and whether
  they errored.
- **`skill` / `agent`** — which skill and agent drove the turn.
- **reward / outcome** — 👍/👎, degraded, tool-error signals.

So "give me every successful turn that used `send_email`" is a one-command slice
(`--tool send_email --successful-only`), and "every turn from my email skill" is another
(`--skill email_specialist`). You're not scraping a text blob — you're querying a
labeled dataset.

### Why specialists win

A 1–3B model rarely masters your *whole* toolbox, but it can learn **one tool's**
call pattern — argument shapes, when to ask for a missing field, how to read the
result — extremely reliably. Scoped data → a scoped, dependable model. Train several
small specialists instead of one mediocre generalist, and route each tool's turns to
its own model.

### The workflow

1. **Scope collection with a skill.** Copy `skills/tool_specialist.yaml.example` to
   `<tool>_specialist.yaml`, set `tools: [your_tool]` and `restrict_tools: true`. Every
   turn it runs is now scoped to one tool and tagged `skill: <name>`. Keep
   `llm: {provider: local, fallback: true}` so that *before* training, turns fall back
   to Claude — you're collecting Claude's correct usage of that one tool.
2. **Collect + label.** Use the skill; react 👍/👎. Watch `--stats` → `tool calls (kept)`
   until the tool has a few hundred good turns.
3. **Export just that tool.**
   ```bash
   uv run python scripts/export_training_data.py --dir <data_dir>/training \
       --skill <tool>_specialist --successful-only --format mlx
   # or slice by the call itself:  --tool your_tool --successful-only
   ```
4. **LoRA a small base** (§3b) — a 1.5B/3B model is plenty for one tool:
   ```bash
   mlx_lm.lora --model Qwen/Qwen2.5-1.5B-Instruct --train \
       --data <data_dir>/training --iters 400
   mlx_lm.fuse --model Qwen/Qwen2.5-1.5B-Instruct --adapter-path adapters
   mlx_lm.server --model fused_model      # serves /v1/chat/completions
   ```
5. **Point the skill at the specialist.** Set that skill's `llm.model` to your fine-tune
   and serve it via the `local` provider (`config.yaml` → `defaults.llm.local.base_url`).
   Now only that tool's turns run on that model — cheap, fast, local — while everything
   else stays on Claude. (Or wire it as the `model_router` local tier for automatic
   triage; see [docs/LOCAL_MODELS.md](LOCAL_MODELS.md).)

Repeat per tool. Each specialist is small, quick to train, and independently upgradeable.

## 5. What you — and the harness — get from a local model

Training produces weights; this is what the engine already does with them. Serve the
model through any OpenAI-compatible runtime — `mlx_lm.server`, an Ollama Modelfile
(`ollama create my-specialist -f Modelfile`), LM Studio — and the `local` provider
auto-detects it. Runtimes, model choices, and config live in
[docs/LOCAL_MODELS.md](LOCAL_MODELS.md); this section is the map of where a trained
model plugs in and what you get back.

**For you:**

- **Subscription headroom.** Every locally-served turn is one that doesn't burn Claude
  usage. The engine already downgrades models when you hit a usage cap — with a trained
  specialist in the local tier, "downgraded" stops meaning "bad at your tools."
- **Latency.** A 1.5–3B specialist answers a routine tool turn in about a second on
  Apple Silicon, no API round-trip. For high-frequency tools (lights, lookups,
  templated messages) that's the difference between an agent and a page-load.
- **Privacy & offline.** Turns routed local never leave the box. Agents pinned to the
  local provider keep working with the network — or the LLM vendor — down.
- **A flywheel you own.** The collector keeps recording, your 👍/👎 keep labeling, and a
  periodic re-export + LoRA run makes the specialist track how *your* tool usage
  evolves. The weights are yours; nobody deprecates them.

**Where the harness plugs it in:**

- **Tier-router local tier** (`llm.router` in config) — the tiny classifier keeps
  confidently-simple turns on your local model and escalates everything else to Claude.
  Deterministic guards run first: attachments, code blocks, long messages, and
  build/deploy/repo directives *never* route local (the local model has no CLI or repo
  access). `/admin usage` shows the local-vs-Claude split, so the savings are visible.
- **Skill pinning** (`llm: {provider: local, model: <your-finetune>}` in a skill) — the
  §4 specialist loop: only that skill's turns run on your model, everything else stays
  on Claude, and `fallback: true` retries the turn on Claude if the local runtime is
  down. A pinned task never silently dies.
- **Agent pinning** (`agents.yaml → llm.provider: local`) — an entire agent on your
  weights, inheriting every tool through the engine's provider-agnostic dispatch (with
  HITL, rate limits, and access control still enforced by the engine, not the model).
- **Ops safety nets** — startup preflight verifies the local runtime and models,
  missing Ollama models auto-pull on first use, and reasoning-tag output is sanitized
  before anything reaches a channel.
- **Train/inference parity** — the same `tools_available` schemas your exports trained
  on are what the local provider sends at run time, so the specialist meets exactly the
  tool menu it learned (see below).

## What gets recorded (per turn)

`turns.jsonl` line: `turn_id, ts, agent, session_id, cli_session_id, connector,
channel_id, user_id, skill, reply_message_id, input (the user's message), response
(content/model/tokens/stop_reason), tools_available (the tools offered to the model
this turn, as OpenAI function schemas), tools (full tool_use/tool_result/text trace
from the Claude Code transcript), outcome (stop_reason/degraded/tool_calls/tool_errors)`.
Secrets are redacted. `rewards.jsonl` line: `ts, reply_message_id, agent, signal, user_id`.

`tools_available` matters for tool-use fine-tuning: at inference the local provider
sends the model those same schemas (`_to_openai_tools`), so the `sft`/`mlx`/`openai`
exports attach them as each example's `tools` array — the model trains on the exact
available-tool menu it will see at run time (train/inference parity). Without it the
fine-tune never learns to condition on which tools exist, which is the whole point of
teaching it to call your *created* tools.
