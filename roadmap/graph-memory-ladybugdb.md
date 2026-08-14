# Graph memory — LadybugDB layer ("middle way")

Status: planned · Researched: 2026-08

## Context

Goal: store agent memories in a graph database, container-free and simple, in keeping with
the "one process + SQLite, zero maintenance" philosophy.

Research (Aug 2026) settled on **LadybugDB** — the community successor to Kuzu (abandoned
Oct 2025 after Apple acquired the company). PyPI package `ladybug` (v0.19.1, Python
3.10–3.14, MIT): fully in-process like SQLite, single `.lbdb` file, Cypher, async API
(`lb.AsyncConnection`). FalkorDBLite was the runner-up (embedded but spawns a redis-server
child process; its docs discourage production use). Agent-memory frameworks
(Graphiti/Cognee/mem0) were ruled out as too heavy: LLM-driven extraction pipelines and
large dependency trees don't fit the drop-in-module philosophy.

Chosen shape — the **middle way**: keep the battle-tested sqlite backend untouched as the
system of record (facts, FTS, embeddings, decay) and add LadybugDB as an additive, opt-in
**graph layer** for entity/relationship memory. Tool names describe capabilities
(link/related/query), not the engine, so the layer can be promoted to a fuller backend
later without renaming. One **shared graph** for all agents, with scope attribution
(`global` / `agent:<id>` / `group:<name>`) mirroring `SQLiteMemory._scope_filter`.

Key codebase facts driving the design:
- `src/core/registry.py` auto-registers direct `MemoryBackend` subclasses and
  `main.py:234-241` eagerly instantiates them all → the graph store must NOT be a
  `MemoryBackend` subclass; it lives in `src/lib/` (home of `compressor.py`).
- `ToolContext` (`src/core/base.py:144`) is `__slots__`-frozen and constructed in two
  places → tools reach the graph via a module-level singleton, the established pattern of
  `src/tools/browser.py:33` (`_sessions`).
- Ladybug/Kuzu is **single-writer** → only the main kbots process opens the file; the MCP
  subprocess (`src/mcp_server.py`, which auto-surfaces all `@tool`s) gets an explicit
  "unavailable" reason instead of a DB handle.
- SQLite already has a dead graph feature (`relationships` table + `add_relationship`/
  `query_relationships`, zero callers) — left untouched; separate cleanup PR if ever.

## Changes

Branch `feat/graph-memory`, commit style `feat(memory): add opt-in LadybugDB graph layer`.

### 1. `pyproject.toml` — optional extra (existing groups: api, search, embeddings, …)
```toml
graph = ["ladybug>=0.19,<0.20"]
```

### 2. New `src/lib/graph_store.py` (~200 lines) — `GraphMemory` + singleton
- `GraphMemory(config)` wraps `lb.Database`/`lb.AsyncConnection`; **lazy open** on first
  use (guarded by `asyncio.Lock`, lazy `import ladybug`; ImportError → `GraphUnavailable`
  telling the user to `pip install 'kbots[graph]'`). Path resolved against `PROJECT_ROOT`
  like `sqlite.py:35-38`.
- Schema DDL on open (agents never touch DDL):
  `CREATE NODE TABLE IF NOT EXISTS Entity(name STRING PRIMARY KEY, type STRING, scope STRING, created_by STRING, created_at STRING)`
  `CREATE REL TABLE IF NOT EXISTS Related(FROM Entity TO Entity, rel STRING, confidence DOUBLE, scope STRING, created_by STRING, created_at STRING)`
  Timestamps as ISO strings; parameter binding via `$param` (never f-string user values).
- Methods: `link` (MERGE-based, idempotent per (a, rel, b); entities are shared
  identifiers, **visibility lives on edges**; scope `"agent"` normalizes to
  `agent:<created_by>` like `sqlite.py:160`), `related` (Python-side BFS one hop per
  frontier — Kuzu can't parameterize variable-length bounds; depth capped at 3), `unlink`
  (own/agent-scoped edges only), `entities`, `query` (raw Cypher), `close`.
- Edge scope filter mirroring sqlite:
  `r.scope = 'global' OR r.scope STARTS WITH 'group:' OR r.scope = $agent_scope`.
- Singleton API: `init_graph(memory_cfg) -> GraphMemory | None` (called once by main.py;
  None when disabled), `get_graph()` (raises `GraphUnavailable` with recorded reason),
  `set_unavailable(reason)`, `close_graph()`.

### 3. New `src/tools/graph.py` (~130 lines) — 4 auto-discovered tools, category "memory"
- `memory_link(ctx, a, rel, b, confidence=0.7, scope="agent")`
- `memory_related(ctx, entity, depth=1, limit=25)` — renders "a —rel→ b (conf, scope)"
  grouped by hop
- `memory_unlink(ctx, a, rel, b)`
- `memory_graph_query(ctx, cypher, limit=50)` — **read-only**: word-boundary regex rejects
  CREATE/MERGE/SET/DELETE/DETACH/DROP/ALTER/COPY/CALL/LOAD/INSTALL/ATTACH/IMPORT/EXPORT
  (defense-in-depth, documented as such); writes only via memory_link/unlink so scope
  attribution can't be bypassed.
- Each tool converts `GraphUnavailable` into a clear returned string (style of
  `src/tools/memory.py:15-19`), never raises.

### 4. Config
`config/config.yaml.example` under `defaults.memory` (after line ~78):
```yaml
    graph:
      enabled: false
      path: data/graph/memory.lbdb   # data/ is gitignored; verify entry during impl
```
`src/core/config_schema.py:45-50` — add to the memory sub-schema:
`"graph": (dict, False, None, {"enabled": (bool,...), "path": (str,...)})`, plus the
drive-by one-liner `"reflection": (dict, False, None, None)` (confirmed missing today, so
the example config currently triggers an unknown-key warning).

### 5. Lifecycle — `src/main.py`
- After the memory-backends block (~line 242):
  `graph = init_graph(defaults.get("memory", {}))`; log engine+path if enabled; cheap
  `importlib.util.find_spec("ladybug")` warning at startup if enabled-but-missing (open
  itself stays lazy).
- `close_graph()` beside `storage.close()` at both exit paths (`main.py:557` and the
  no-connectors early return ~`main.py:406`); no-op if never opened.

### 6. MCP coexistence — `src/mcp_server.py` (~line 326)
`set_unavailable("Graph memory is owned by the main kbots process (single-writer database)…")`
so the subprocess never opens the `.lbdb`. Follow-up (out of scope): route MCP-side graph
reads through the internal loopback API.

### 7. Tests — new `tests/test_graph_memory.py` (style of `tests/test_lessons.py`)
`pytest.importorskip("ladybug")` for DB tests; `tmp_path` databases. Cover: link+related,
depth-2 BFS with cap, scope isolation (agent-scoped edges invisible to other agents;
global/group visible), idempotent MERGE, unlink-own-only, reopen persistence,
read-only-guard regex (pure function, no DB), graceful degradation without ladybug
(returns string mentioning `kbots[graph]`), `init_graph` disabled → `get_graph()` raises.

## macOS 14: `uv sync --extra graph` fails — patched local wheel needed

`ladybug` publishes only `macosx_15_0` wheels, and building its sdist on
macOS 14 fails: `src/c_api/{connection,database}.cpp` use `std::atomic_ref`,
which Apple's toolchain only supports from the macOS 15 SDK (libc++ >= 17;
upstream documents Xcode 16+ as the minimum — LadybugDB/ladybug#779). Linux and
macOS 15+ are unaffected.

Workaround for a macOS 14 deployment:

1. Download the 0.19.1 sdist and, in `ladybug-source/src/c_api/`, replace the
   two `std::atomic_ref<void*>(...).exchange(nullptr)` call sites
   (`connection.cpp` and `database.cpp`) with the equivalent builtin:
   `__atomic_exchange_n(&<member>, static_cast<void*>(nullptr), __ATOMIC_SEQ_CST)`
   — same semantics, no behavior change.
2. Build a wheel (`python setup.py bdist_wheel`; needs cmake + a C++20
   compiler) and copy it into the overlay, e.g. `$KBOTS_OVERLAY/wheels/`.
3. Reference it from `$KBOTS_OVERLAY/requirements.txt`
   (`ladybug @ file://<path-to-wheel>`) — sync.sh installs Layer 3 after the
   core `uv sync`, so the wheel is re-applied on every deploy.
4. Do **not** add `graph` to `$KBOTS_OVERLAY/extras`: that makes `uv sync`
   attempt the sdist build, which fails and aborts the deploy. The config
   block below is still just `defaults.memory.graph.enabled: true`.

Remove the workaround (wheel + requirements entry, re-add the `graph` extra)
once the host is on macOS 15+ or upstream ships broader wheels.

## Deferred (designed, not in scope)
- **Auto-recall graph context**: `agent_manager._auto_recall` (~line 326-364) can later
  append a `<graph-neighborhood>` block via `get_graph().related(...)`; gate behind future
  `defaults.memory.graph.auto_recall`. Nothing in this plan blocks it.
- SQLite dead `relationships` code and the pre-existing `discord.py:1427` search
  arg-order bug: untouched here (separate one-line fix).

## Verification
```bash
uv sync                                        # base install, no ladybug
uv run pytest tests/test_graph_memory.py -v    # degradation tests pass, DB tests skip
uv sync --extra graph
uv run pytest tests/test_graph_memory.py -v    # all pass
uv run pytest tests/ -x -q                     # no regressions
# config validation: no unknown-key warnings for the graph block
# Manual smoke: enable graph in config, run `uv run python -m src.main`, have an agent
# memory_link("kbots","uses","LadybugDB",scope="global"), memory_related("kbots"),
# restart, memory_related("kbots") again → persisted.
```
