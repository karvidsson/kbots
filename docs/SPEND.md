# Agent spend ledger

`/spend` reports the past seven days. `/spend days:30` changes the rolling
window (1 to 365 days). The response is private. Configured Discord admins,
including the owner, see all agents. Other users see only turns they initiated,
across agents. Shared-channel history is not attributed to its first visitor.
Historical rows without a verified per-turn requester are visible only to admins.

The report is a padded table inside a code block. Large reports arrive as one
private text attachment containing the complete report. It shows agent, turns,
recorded tokens, list-price equivalent in USD, and busiest model by recorded
tokens. Priced agents sort by known cost; wholly unpriced agents sort by tokens.
Names longer than the display column end in `~`.

**USD is a list-price equivalent, not a subscription bill.** This applies in
particular to Claude Code on a subscription. The ledger does not apportion
subscription fees, invoice anyone, or infer an actual charge from token counts.

A cost of `unknown` means no usable provider cost or configured price was
available. `$1.2500 + ?` means a known subtotal plus unpriced work, not a complete
total. Unknown-cost turns are counted separately; one turn with several unpriced
calls counts once. Missing or partial token totals also display `+ ?`. Claude
main-loop tokens without verified model-counter deltas exclude subagent usage,
so those rows and the total show `+ ?` even when all four main-loop classes exist.
Older v1 records lack that coverage marker; their main-loop-only totals can also
omit subagents. Very small positive costs display `<$0.0001`, never a rounded zero. Source counts distinguish provider
costs, configured prices and unknown records. Totals use decimal arithmetic.

## Configuration

The shipped example has an empty table. Configure rates under
`defaults.spend.prices`, indexed first by the **provider registry name** from
configuration and then by the exact reported model. All four rates are USD per
million tokens. This shape is illustrative; replace every placeholder with a
verified numeric rate before enabling that entry:

```yaml
defaults:
  spend:
    prices: {}
    # prices:
    #   provider_name:
    #     exact_reported_model:
    #       input: <USD per million uncached input tokens>
    #       output: <USD per million output tokens>
    #       cache_read: <USD per million cache-hit tokens>
    #       cache_write: <USD per million cache-write tokens>
```

Prices must be finite, nonnegative numbers. All four classes must be measured
and all four prices provided, even when one class is zero. Missing, malformed,
negative or nonfinite values leave cost unknown. There are no aliases, wildcard
rates, hardcoded vendor prices or automatic price downloads. If the provider
reports zero cost, it is a known zero and takes precedence over the table.

Order of precedence: provider-reported USD, configured token rates, unknown.
The selected rates and computed cost are captured at turn time. Changing the
configuration affects subsequent turns, not history. Activate configuration
changes through the deployment's normal restart procedure.

## Measurement and scope

The ledger covers assistant turns handled by AgentManager, including inter-agent
requests, scheduled messages routed through that manager, abstained `NO_REPLY`
turns, and each outer tool round or provider fallback. It does not cover direct
background calls such as reflection, summarization, training judges, alert
analysis or the autofix worker, nor unrelated uses of the provider account.
A provider retry which exposes no usage in its returned result cannot be priced
here. Provider-internal retry attempts are not independent ledger records.
This is an assistant-turn ledger, not an account billing export.

Tokens are stored as four disjoint classes: uncached input, output, cache read
and cache write. A provider total remains available when the breakdown is
incomplete. Missing measurements are nullable, never inferred as zero. APIs
without a separate cache-write category use zero for that category.

- Claude Code: preserve returned `usage` and `total_cost_usd`. Its documented
  cost and `modelUsage` counters are cumulative across a resumed session; the
  ledger stores raw reported cost and derives a delta from a durable baseline.
  Fresh sessions use the reported total. An unknown baseline or decreased cost
  counter yields unknown cost. A decreased cost stores a null baseline, rather
  than the lower value, and clears model baselines so configured pricing cannot
  reuse a reset counter. The next valid cumulative observation is unknown and
  establishes a new baseline; subsequent deltas can be priced again.
  Missing cost observations and known capture gaps break the chain. An unparsed
  reply or raised call invalidates cumulative baselines only for its exact
  engine session, agent and provider, even without a returned CLI session id.
  Per-model counter deltas include subagents when available and support mixed
  models. Without them, the returned main-loop tokens are retained, but no table
  cost is invented for the unobserved agent tree. Busiest model uses per-model
  deltas when available. [Claude cost tracking](https://code.claude.com/docs/en/agent-sdk/cost-tracking)
- Codex CLI: use `turn.completed.usage`. Cached input is deducted from inclusive
  input; output already includes reasoning. Its published completion event does
  not expose USD cost. The configured model is retained because the event does
  not identify a served model; an unspecified model remains `codex-default`.
  [Codex noninteractive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- OpenAI-compatible Chat Completions: use `prompt_tokens`, `completion_tokens`
  and `prompt_tokens_details.cached_tokens`. Cached input is deducted once;
  reasoning tokens are already part of output. A compatible endpoint's explicit
  `cache_write_tokens` detail is retained. Standard Chat Completions does not
  report USD cost; arbitrary vendor fields are not assumed to be dollars.
  [Chat Completions response](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- Ollama native: use `prompt_eval_count`, `eval_count` and, when present,
  `prompt_eval_cached_count`. Older responses without the cached count keep the
  total but cannot be priced from four known classes. No electricity or hardware
  cost is assumed. [Ollama chat response](https://docs.ollama.com/api/chat)

Durations are elapsed wall-clock milliseconds around each returned provider
call, plus elapsed time since the turn's first call. The last call's turn
duration includes tool dispatch between rounds, but not final Discord delivery.
Provider/model, actual incoming requester and session are retained. No prompt,
reply, tool content, credential or provider response blob is stored in the ledger.

## Storage and failure behavior

Migration adds `messages.usage_turn_id`, `messages.usage_call_count`, `messages.usage_requester_id`,
`turn_usage`, and `usage_baselines` in `kbots.db`. Existing rows are untouched.
`messages.tokens_used` continues to contain a total, now including every outer
provider round and Claude cache creation, rather than only the last round.
The old `get_token_usage` query remains available.

A turn can contain multiple provider/model calls. Its message references the
same turn id, preventing duplicate counting. Calls are committed as they finish,
even if no final reply is posted. Per-call duration, token classes, returned
usage classes, cumulative raw cost, attributed cost, source, model deltas and
any selected price snapshot are retained. Capturing the same call twice is
idempotent. Baselines advance atomically with the corresponding call record.

Capture uses a separate SQLite connection with serialized writes and a short
busy timeout. Optional migration or write failures log an exception type, not
provider data, and never fail an assistant turn. An assistant message whose
capture is absent remains an unknown-cost row. Expected call counts identify
partially captured completed turns so they do not appear fully priced. If the
process dies before a usage observation is returned or persisted, that usage
cannot be recovered. The report says it covers recorded usage. If the ledger
cannot be read, `/spend` says it is unavailable instead of reporting zero.
