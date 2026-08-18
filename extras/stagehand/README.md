# stagehand — AI-native browser automation (smart_browser)

Natural-language browser control via [Stagehand](https://github.com/browserbase/stagehand)
(MIT), parallel to the selector-based `browser` tool: agents say *what* they
want — `act("click the login button")`, `extract` structured data against a
schema, `observe` what's actionable — and Stagehand finds the elements, with
self-healing when pages change.

Runs a local Chromium; **nothing uses Browserbase cloud**. The AI inside
Stagehand runs on your **local model endpoint by default** (Ollama / LM
Studio — the same stack the tier router uses), wired through a custom LLM
callback, so browsing costs no API tokens and page content never leaves the
machine.

## Install

```bash
echo stagehand >> "$KBOTS_OVERLAY/extras"      # dep group survives every deploy
cp extras/stagehand/stagehand_browser.py "$KBOTS_OVERLAY/tools/"
scripts/sync.sh
# restart the service
```

## Config (all optional)

```yaml
stagehand:
  model: ""            # API override, e.g. google/gemini-2.5-flash (needs vault key below)
  local_model: ""      # which local model the callback uses (default: defaults.llm.local.model, else first available)
  base_url: ""         # local endpoint override (default: defaults.llm.local.base_url, else probe :11434 then :1234)
  headless: true
```

Optional API override for stronger act/extract: store an API key as vault key
`secrets/stagehand-api-key` and set `stagehand.model`.

## Honest expectations with local models

Verified live (stagehand 4.0.x + Ollama qwen3.5:9b): **extract works well**
(schema-constrained decoding, with automatic fallback to schema-in-prompt when
Ollama's grammar compiler rejects a schema); **act is fallible** — the model
sometimes answers "no action found", and the tool says so plainly and suggests
either a more specific instruction, the selector-based `browser` tool, or the
API-model override. This matches Stagehand's own guidance that small local
models struggle with its structured outputs.

## Security notes

- Page URLs are validated against the SSRF policy (`src/lib/ssrf`) before
  navigation, and the landed URL is re-validated after every goto/act —
  a page that navigates the session to a blocked address gets the session
  closed.
- Unlike `browser`, there is **no per-request interception**: Stagehand's page
  object exposes no route hook, so subresource requests are not individually
  re-validated. Prefer `browser` for deliberately hostile pages.
- The model endpoint (localhost) is config-derived and never taken from model
  or page input — the SSRF localhost block applies to page URLs only.

## Spike findings (2026-08-18, pinned `stagehand>=4.0,<5`)

- `Stagehand.create(browser=…, model=<async callback>)` — callables are
  auto-wrapped into `ClientLLM`; fully local, no API key, verified no cloud calls.
- `local_browser.launch(headless=True)` for own Chromium;
  `local_browser.connect(cdp_url=…)` exists (future: attach to the
  chrome-debug instance).
- Callback contract gotchas (all handled in `stagehand_browser.py`):
  content blocks are `RootModel` unions (unwrap `.root`); `response_format`
  dumps its schema under the alias `schema_`; results are returned as plain
  dicts with `output_format` + `structured_content`; callback exceptions
  surface as `RPCError` with the message text; local models fence JSON in
  markdown (unfence before parsing).
- `page.url` is a method, not a property, on Stagehand's page wrapper.
