"""Shared graph memory layer (LadybugDB) — additive to the sqlite memory backend.

Entity/relationship storage only; facts stay in sqlite. One .lbdb file shared by
all agents, with scope attribution on edges (global / agent:<id> / group:<name>)
mirroring SQLiteMemory._scope_filter semantics: entities are shared identifiers,
visibility lives on the edges.

LadybugDB (PyPI: `ladybug`, the Kuzu fork) is fully in-process and SINGLE-WRITER:
only the main kbots process may open the file. Tool subprocesses (the MCP
server) never call init_graph(); they call init_graph_client() and forward
every graph call to the main process over the loopback internal API (see
mcp_server.build_server and internal_api._handle_graph). Install with:
pip install 'kbots[graph]'.
"""

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from src.core.base import PROJECT_ROOT
from src.lib.canonical import SINGLE_VALUED, entity_key, normalize_rel

logger = logging.getLogger(__name__)

MAX_DEPTH = 3

_SCOPE_FILTER = "(r.scope = 'global' OR r.scope STARTS WITH 'group:' OR r.scope = $agent_scope)"

_DDL = [
    """CREATE NODE TABLE IF NOT EXISTS Entity(
        name STRING PRIMARY KEY,
        type STRING,
        scope STRING,
        created_by STRING,
        created_at STRING)""",
    """CREATE REL TABLE IF NOT EXISTS Related(
        FROM Entity TO Entity,
        rel STRING,
        confidence DOUBLE,
        scope STRING,
        created_by STRING,
        created_at STRING)""",
]

# Added after the tables existed on live deployments, so they arrive by ALTER
# rather than in the CREATE above. Existing rows get NULL, which reads
# correctly: a NULL valid_to means "still true", and a NULL valid_from means
# "true for as long as we have known about it".
_MIGRATIONS = [
    "ALTER TABLE Entity ADD IF NOT EXISTS entity_key STRING",
    "ALTER TABLE Entity ADD IF NOT EXISTS aliases STRING",
    "ALTER TABLE Related ADD IF NOT EXISTS valid_from STRING",
    "ALTER TABLE Related ADD IF NOT EXISTS valid_to STRING",
]

# Only edges that have not been superseded. Every read applies this unless it
# is explicitly asking about history, so "what is true" never silently mixes
# with "what was once true".
_OPEN_ONLY = "r.valid_to IS NULL"


class GraphUnavailableError(RuntimeError):
    """Graph memory can't be used; the message says why and what to do."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _normalize_scope(scope: str, created_by: str | None) -> str:
    """Accept 'agent' (→ agent:<created_by>), 'global', 'group:<name>'.

    A raw 'agent:<id>' is only accepted when <id> is the caller — an agent must
    not be able to write edges into another agent's private scope (that would be
    a memory-poisoning primitive: the victim sees an authoritative-looking
    private edge it never created).
    """
    if scope == "agent":
        if not created_by:
            raise ValueError("scope 'agent' requires a creating agent id")
        return f"agent:{created_by}"
    if scope.startswith("agent:"):
        if not created_by or scope != f"agent:{created_by}":
            raise ValueError(
                f"Cannot write to another agent's scope {scope!r} — use scope "
                "'agent' for your own private edges.")
        return scope
    if scope == "global" or scope.startswith("group:"):
        return scope
    raise ValueError(f"Invalid scope {scope!r} — use 'agent', 'global', or 'group:<name>'")


def _rows(result) -> list[dict]:
    """Convert a ladybug QueryResult to a list of dicts."""
    cols = result.get_column_names()
    out = []
    while result.has_next():
        out.append(dict(zip(cols, result.get_next())))
    return out


class GraphMemory:
    """Thin async wrapper around one shared LadybugDB database file."""

    def __init__(self, config: dict):
        self.config = config
        path = config.get("path", "data/graph/memory.lbdb")
        # Resolve against the repo root, not CWD — same rule as SQLiteMemory.
        if not Path(path).is_absolute():
            path = str(PROJECT_ROOT / path)
        self.path = path
        self._db = None
        self._conn = None
        self._open_lock = asyncio.Lock()
        self._entity_index: dict[str, str] = {}

    async def _ensure_open(self):
        """Lazily open the database and create the schema (idempotent)."""
        if self._conn is not None:
            return self._conn
        async with self._open_lock:
            if self._conn is not None:
                return self._conn
            try:
                import ladybug as lb
            except ImportError:
                raise GraphUnavailableError(
                    "Graph memory needs the LadybugDB package — install with: "
                    "pip install 'kbots[graph]' (or: uv sync --extra graph)"
                ) from None
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._db = lb.Database(self.path)
            conn = lb.AsyncConnection(self._db)
            for ddl in _DDL:
                try:
                    await conn.execute(ddl)
                except Exception as e:  # older releases without IF NOT EXISTS
                    if "already exists" not in str(e).lower():
                        raise
            for stmt in _MIGRATIONS:
                try:
                    await conn.execute(stmt)
                except Exception as e:
                    # A store that cannot take the new columns still works; it
                    # just has no history. Failing to open would be worse.
                    if "already exists" not in str(e).lower():
                        logger.warning(f"Graph migration skipped ({stmt}): {e}")
            self._conn = conn
            await self._load_entity_index(conn)
            logger.info(f"Graph memory: LadybugDB open ({self.path}), "
                        f"{len(self._entity_index)} entities indexed")
            return self._conn

    async def _load_entity_index(self, conn) -> None:
        """Map canonical key to the stored display name for every entity.

        Resolution has to happen against what is already in the graph, not
        against the current batch, or two spellings arriving in different
        extraction passes still create two nodes. The index is small (hundreds
        of entities on a mature deployment) and is kept in memory rather than
        queried per link, because link runs once per extracted edge.
        """
        self._entity_index = {}
        try:
            result = await conn.execute("MATCH (e:Entity) RETURN e.name AS name")
        except Exception as e:
            logger.warning(f"Graph: could not build entity index: {e}")
            return
        for row in _rows(result):
            name = row.get("name")
            if not name:
                continue
            self._entity_index.setdefault(entity_key(name), name)

    async def _resolve_entity(self, name: str, conn, scope: str,
                              created_by: str, ts: str) -> str:
        """Return the stored name for `name`, creating the entity if new.

        A different spelling of a known entity resolves to the stored one and
        is recorded as an alias rather than becoming a second node. This is the
        whole of entity resolution and it is deliberately deterministic: same
        name modulo case and punctuation means same entity, nothing else does.
        """
        key = entity_key(name)
        if not key:
            raise ValueError("Entity name must contain at least one alphanumeric character")
        existing = self._entity_index.get(key)
        if existing is not None:
            if existing != name:
                await self._record_alias(conn, existing, name)
            return existing
        await conn.execute(
            "MERGE (e:Entity {name: $name}) "
            "ON CREATE SET e.type = 'entity', e.scope = $scope, "
            "e.created_by = $by, e.created_at = $ts, e.entity_key = $key",
            {"name": name, "scope": scope, "by": created_by, "ts": ts, "key": key},
        )
        self._entity_index[key] = name
        return name

    async def _record_alias(self, conn, canonical: str, alias: str) -> None:
        """Keep the alternative spelling on the node so a merge is auditable."""
        try:
            result = await conn.execute(
                "MATCH (e:Entity {name: $name}) RETURN e.aliases AS aliases",
                {"name": canonical})
            rows = _rows(result)
            current = (rows[0].get("aliases") if rows else None) or ""
            parts = [p for p in current.split("|") if p]
            if alias in parts or alias == canonical:
                return
            parts.append(alias)
            await conn.execute(
                "MATCH (e:Entity {name: $name}) SET e.aliases = $aliases",
                {"name": canonical, "aliases": "|".join(parts[:20])})
        except Exception as e:
            logger.debug(f"Graph: could not record alias {alias!r}: {e}")

    async def link(self, a: str, rel: str, b: str, *, confidence: float = 0.7,
                   scope: str = "global", created_by: str | None = None) -> dict:
        """Create or refresh a relationship. Idempotent per (a, rel, b).

        Three things happen before the write that did not before:

        - both endpoints are resolved against existing entities, so a new
          spelling of a known thing becomes an alias instead of a new node;
        - the relation is normalised onto the shared vocabulary, so `utilizes`
          and `uses` stop being two different edges to traverse;
        - for a single-valued relation, an existing open edge with a different
          object is expired rather than left to sit beside the new one. Both
          used to be readable as currently true at the same time.
        """
        conn = await self._ensure_open()
        scope = _normalize_scope(scope, created_by)
        ts = _now()
        by = created_by or ""

        rel, known = normalize_rel(rel)
        if not known:
            logger.info(f"Graph: relation {rel!r} is outside the shared vocabulary "
                        f"(kept as-is; see src/lib/canonical.py)")

        a = await self._resolve_entity(a, conn, scope, by, ts)
        b = await self._resolve_entity(b, conn, scope, by, ts)

        superseded = 0
        if rel in SINGLE_VALUED:
            superseded = await self._expire_conflicting(conn, a, rel, b, ts)

        # MERGE alone cannot express "the edge that is still open", so an open
        # edge is looked up first and only created when there is none. Without
        # this, re-asserting a fact after a supersession would match the
        # expired edge and revive it.
        result = await conn.execute(
            "MATCH (a:Entity {name: $a})-[r:Related {rel: $rel}]->(b:Entity {name: $b}) "
            f"WHERE {_OPEN_ONLY} RETURN COUNT(*) AS n",
            {"a": a, "b": b, "rel": rel})
        is_open = int(_rows(result)[0]["n"]) > 0

        if is_open:
            await conn.execute(
                "MATCH (a:Entity {name: $a})-[r:Related {rel: $rel}]->(b:Entity {name: $b}) "
                f"WHERE {_OPEN_ONLY} SET r.confidence = $conf",
                {"a": a, "b": b, "rel": rel, "conf": float(confidence)})
        else:
            await conn.execute(
                "MATCH (a:Entity {name: $a}), (b:Entity {name: $b}) "
                "CREATE (a)-[r:Related {rel: $rel, confidence: $conf, scope: $scope, "
                "created_by: $by, created_at: $ts, valid_from: $ts}]->(b)",
                {"a": a, "b": b, "rel": rel, "conf": float(confidence),
                 "scope": scope, "by": by, "ts": ts})

        out = {"a": a, "rel": rel, "b": b, "confidence": confidence, "scope": scope}
        if superseded:
            out["superseded"] = superseded
        return out

    async def _expire_conflicting(self, conn, a: str, rel: str, b: str,
                                  ts: str) -> int:
        """Close open edges (a)-[rel]->(other) where other is not b.

        Only for relations in SINGLE_VALUED, where a second current value is a
        contradiction rather than an addition. The old edge is kept with a
        valid_to so "where did they work before" is still answerable; nothing
        is deleted.
        """
        result = await conn.execute(
            "MATCH (a:Entity {name: $a})-[r:Related {rel: $rel}]->(b:Entity) "
            f"WHERE {_OPEN_ONLY} AND b.name <> $b RETURN COUNT(*) AS n",
            {"a": a, "rel": rel, "b": b})
        n = int(_rows(result)[0]["n"])
        if n:
            await conn.execute(
                "MATCH (a:Entity {name: $a})-[r:Related {rel: $rel}]->(b:Entity) "
                f"WHERE {_OPEN_ONLY} AND b.name <> $b SET r.valid_to = $ts",
                {"a": a, "rel": rel, "b": b, "ts": ts})
            logger.info(f"Graph: {n} edge(s) superseded — {a} {rel} is now {b}")
        return n

    async def history(self, entity: str, *, agent_id: str | None = None,
                      limit: int = 50) -> list[dict]:
        """Every edge touching `entity`, expired ones included.

        The one read that deliberately does not filter to open edges: it
        answers "what changed and when", which is the question the timestamps
        were added for.
        """
        limit = max(1, min(int(limit), 500))
        conn = await self._ensure_open()
        agent_scope = f"agent:{agent_id}" if agent_id else "agent:"
        result = await conn.execute(
            "MATCH (a:Entity)-[r:Related]->(b:Entity) "
            f"WHERE (a.name = $entity OR b.name = $entity) AND {_SCOPE_FILTER} "
            "RETURN a.name AS src, r.rel AS rel, b.name AS dst, "
            "r.confidence AS confidence, r.scope AS scope, "
            "r.created_by AS created_by, r.valid_from AS valid_from, "
            "r.valid_to AS valid_to "
            f"ORDER BY r.valid_from DESC LIMIT {limit}",
            {"entity": entity, "agent_scope": agent_scope},
        )
        rows = _rows(result)
        for row in rows:
            row["current"] = row.get("valid_to") is None
        return rows

    async def related(self, entity: str, depth: int = 1, *,
                      agent_id: str | None = None, limit: int = 50) -> list[dict]:
        """Edges reachable from entity within `depth` hops (scope-filtered BFS).

        Python-side BFS, one single-hop query per frontier — Kuzu/Ladybug can't
        parameterize variable-length bounds, and per-edge scope filtering inside
        recursive patterns is awkward. Each edge dict carries a `hop` key.
        """
        depth = max(1, min(int(depth), MAX_DEPTH))
        limit = max(1, min(int(limit), 500))
        conn = await self._ensure_open()
        agent_scope = f"agent:{agent_id}" if agent_id else "agent:"
        seen_nodes = {entity}
        seen_edges: set[tuple] = set()
        edges: list[dict] = []
        frontier = [entity]
        for hop in range(1, depth + 1):
            if not frontier or len(edges) >= limit:
                break
            result = await conn.execute(
                "MATCH (a:Entity)-[r:Related]->(b:Entity) "
                f"WHERE (list_contains($frontier, a.name) OR list_contains($frontier, b.name)) "
                f"AND {_SCOPE_FILTER} AND {_OPEN_ONLY} "
                "RETURN a.name AS src, r.rel AS rel, b.name AS dst, "
                "r.confidence AS confidence, r.scope AS scope, r.created_by AS created_by",
                {"frontier": frontier, "agent_scope": agent_scope},
            )
            next_frontier = []
            for row in _rows(result):
                key = (row["src"], row["rel"], row["dst"])
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                row["hop"] = hop
                edges.append(row)
                for node in (row["src"], row["dst"]):
                    if node not in seen_nodes:
                        seen_nodes.add(node)
                        next_frontier.append(node)
                if len(edges) >= limit:
                    break
            frontier = next_frontier
        return edges

    async def unlink(self, a: str, rel: str, b: str, *, agent_id: str) -> int:
        """Delete a relationship. Agents may only remove edges they created or
        edges scoped to them. Returns number of edges removed.

        This really deletes, including expired versions of the same edge.
        Supersession is for a fact that changed; unlink is for a fact that
        should never have been recorded, and leaving history behind would make
        it a worse version of the changelog retention bug.
        """
        conn = await self._ensure_open()
        rel, _ = normalize_rel(rel)
        a = self._entity_index.get(entity_key(a), a)
        b = self._entity_index.get(entity_key(b), b)
        params = {"a": a, "b": b, "rel": rel, "agent": agent_id,
                  "agent_scope": f"agent:{agent_id}"}
        guard = "(r.created_by = $agent OR r.scope = $agent_scope)"
        result = await conn.execute(
            "MATCH (a:Entity {name: $a})-[r:Related {rel: $rel}]->(b:Entity {name: $b}) "
            f"WHERE {guard} RETURN COUNT(*) AS n", params)
        n = int(_rows(result)[0]["n"])
        if n:
            await conn.execute(
                "MATCH (a:Entity {name: $a})-[r:Related {rel: $rel}]->(b:Entity {name: $b}) "
                f"WHERE {guard} DELETE r", params)
        return n

    async def entities(self, *, agent_id: str | None = None, limit: int = 50) -> list[dict]:
        """Entities ranked by number of visible connections."""
        limit = max(1, min(int(limit), 500))
        conn = await self._ensure_open()
        agent_scope = f"agent:{agent_id}" if agent_id else "agent:"
        result = await conn.execute(
            "MATCH (e:Entity) OPTIONAL MATCH (e)-[r:Related]-(:Entity) "
            f"WITH e, r WHERE r IS NULL OR ({_SCOPE_FILTER} AND {_OPEN_ONLY}) "
            "RETURN e.name AS name, e.type AS type, COUNT(r) AS degree "
            f"ORDER BY degree DESC, name LIMIT {limit}",
            {"agent_scope": agent_scope},
        )
        return _rows(result)

    async def export(self, *, agent_id: str | None = None, limit: int = 400,
                     own_only: bool = False, all_scopes: bool = False) -> dict:
        """Edges + their endpoints, for visualization.

        Default: everything visible to the agent (own + global + group edges).
        own_only: just this agent's memories — edges it created or scoped to it.
        all_scopes: every edge regardless of scope — the fleet-wide admin view.
            Callers must gate this themselves (memory_graph restricts it to
            coordinator/privileged agents); the store trusts its caller.
        Returns {"nodes": [{name, type, degree}], "edges": [{src, rel, dst, ...}]}.
        """
        limit = max(1, min(int(limit), 2000))
        conn = await self._ensure_open()
        agent_scope = f"agent:{agent_id}" if agent_id else "agent:"
        if all_scopes:
            where = "r.rel IS NOT NULL"
            params = {}
        elif own_only:
            where = "(r.created_by = $agent OR r.scope = $agent_scope)"
            params = {"agent": agent_id or "", "agent_scope": agent_scope}
        else:
            where = _SCOPE_FILTER
            params = {"agent_scope": agent_scope}
        # A visualization of the graph should show what is currently true; an
        # expired edge drawn beside its replacement reads as a contradiction.
        where = f"({where}) AND {_OPEN_ONLY}"
        result = await conn.execute(
            "MATCH (a:Entity)-[r:Related]->(b:Entity) "
            f"WHERE {where} "
            "RETURN a.name AS src, a.type AS src_type, b.name AS dst, b.type AS dst_type, "
            "r.rel AS rel, r.confidence AS confidence, r.scope AS scope, "
            "r.created_by AS created_by "
            f"LIMIT {limit}",
            params,
        )
        edges = _rows(result)
        nodes: dict[str, dict] = {}
        for e in edges:
            for name, ntype in ((e["src"], e.pop("src_type")), (e["dst"], e.pop("dst_type"))):
                node = nodes.setdefault(name, {"name": name, "type": ntype or "entity", "degree": 0})
                node["degree"] += 1
        return {"nodes": list(nodes.values()), "edges": edges}

    async def find(self, *, rel: str | None = None, entity: str | None = None,
                   agent_id: str | None = None, limit: int = 50) -> list[dict]:
        """Scope-filtered edge search by relationship and/or entity.

        Replaces the old raw-Cypher `query()`: raw Cypher could not have the
        per-edge scope predicate enforced, so any agent could read every other
        agent's private edges. This builds a fully parameterized, scope-filtered
        query instead. `rel` and `entity` are optional filters.
        """
        limit = max(1, min(int(limit), 500))
        conn = await self._ensure_open()
        agent_scope = f"agent:{agent_id}" if agent_id else "agent:"
        clauses = [_SCOPE_FILTER, _OPEN_ONLY]
        params: dict = {"agent_scope": agent_scope}
        if rel:
            # Normalised on the way in as well as on the way out, or a search
            # for "utilizes" would miss every edge stored as "uses".
            clauses.append("r.rel = $rel")
            params["rel"] = normalize_rel(rel)[0]
        if entity:
            clauses.append("(a.name = $entity OR b.name = $entity)")
            params["entity"] = entity
        where = " AND ".join(clauses)
        result = await conn.execute(
            "MATCH (a:Entity)-[r:Related]->(b:Entity) "
            f"WHERE {where} "
            "RETURN a.name AS src, r.rel AS rel, b.name AS dst, "
            "r.confidence AS confidence, r.scope AS scope, r.created_by AS created_by "
            f"LIMIT {limit}",
            params,
        )
        return _rows(result)

    def close(self) -> None:
        """Close connection and database. Safe to call twice or before open."""
        for attr in ("_conn", "_db"):
            obj = getattr(self, attr)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)


class GraphClient:
    """Loopback proxy to the main process's GraphMemory.

    Tool subprocesses (the MCP server, a stdio child of each agent's CLI) must
    never open the single-writer .lbdb file. Instead they forward every call to
    the internal API in the main process (src/core/internal_api.py, /graph
    route), which executes it against the one real GraphMemory. Same method
    signatures as GraphMemory, so get_graph() callers can't tell the difference.
    """

    def __init__(self, api: str, token: str):
        self._api = api
        self._token = token

    async def _call(self, method: str, **kwargs):
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=60)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self._api}/graph",
                    json={"method": method, "kwargs": kwargs},
                    headers={"Authorization": f"Bearer {self._token}"},
                ) as resp:
                    data = await resp.json(content_type=None)
        except GraphUnavailableError:
            raise
        except Exception as e:
            raise GraphUnavailableError(
                f"Graph memory unreachable (loopback API): {e}") from None
        if "error" in data:
            if data.get("kind") == "value":
                raise ValueError(data["error"])
            raise GraphUnavailableError(data["error"])
        return data.get("result")

    async def link(self, a: str, rel: str, b: str, *, confidence: float = 0.7,
                   scope: str = "global", created_by: str | None = None) -> dict:
        return await self._call("link", a=a, rel=rel, b=b, confidence=confidence,
                                scope=scope, created_by=created_by)

    async def related(self, entity: str, depth: int = 1, *,
                      agent_id: str | None = None, limit: int = 50) -> list[dict]:
        return await self._call("related", entity=entity, depth=depth,
                                agent_id=agent_id, limit=limit)

    async def history(self, entity: str, *, agent_id: str | None = None,
                      limit: int = 50) -> list[dict]:
        return await self._call("history", entity=entity, agent_id=agent_id,
                                limit=limit)

    async def unlink(self, a: str, rel: str, b: str, *, agent_id: str) -> int:
        return await self._call("unlink", a=a, rel=rel, b=b, agent_id=agent_id)

    async def entities(self, *, agent_id: str | None = None, limit: int = 50) -> list[dict]:
        return await self._call("entities", agent_id=agent_id, limit=limit)

    async def export(self, *, agent_id: str | None = None, limit: int = 400,
                     own_only: bool = False, all_scopes: bool = False) -> dict:
        return await self._call("export", agent_id=agent_id, limit=limit,
                                own_only=own_only, all_scopes=all_scopes)

    async def find(self, *, rel: str | None = None, entity: str | None = None,
                   agent_id: str | None = None, limit: int = 50) -> list[dict]:
        return await self._call("find", rel=rel, entity=entity,
                                agent_id=agent_id, limit=limit)

    def close(self) -> None:
        """Nothing to close — sessions are per-call."""


# --- Module-level singleton (pattern of src/tools/browser.py _sessions) ---

_graph: GraphMemory | GraphClient | None = None
_unavailable_reason = "Graph memory is not enabled (set defaults.memory.graph.enabled in config.yaml)"


def init_graph(memory_cfg: dict) -> GraphMemory | None:
    """Called once by the owning process (main.py) with defaults.memory.

    Returns None when disabled. When enabled but the package is missing, still
    returns the GraphMemory (open is lazy) so tools give a clear install hint.
    """
    global _graph
    cfg = (memory_cfg or {}).get("graph") or {}
    if not cfg.get("enabled"):
        _graph = None
        return None
    _graph = GraphMemory(cfg)
    import importlib.util
    if importlib.util.find_spec("ladybug") is None:
        logger.warning("Graph memory enabled but the 'ladybug' package is not installed — "
                       "run: uv sync --extra graph")
    return _graph


def init_graph_client() -> bool:
    """Called by tool subprocesses (mcp_server): route graph calls to the main
    process over the loopback internal API. Returns False when the loopback
    env (KBOTS_INTERNAL_API / KBOTS_INTERNAL_TOKEN) isn't present — e.g. a
    standalone `kbots-mcp` run with no main process to forward to."""
    global _graph
    import os
    api = os.environ.get("KBOTS_INTERNAL_API", "")
    token = os.environ.get("KBOTS_INTERNAL_TOKEN", "")
    if not api or not token:
        return False
    _graph = GraphClient(api, token)
    return True


def get_graph() -> GraphMemory | GraphClient:
    """Tool-facing accessor. Raises GraphUnavailableError with the recorded reason."""
    if _graph is None:
        raise GraphUnavailableError(_unavailable_reason)
    return _graph


def set_unavailable(reason: str) -> None:
    """Record why graph memory is unavailable in this process (e.g. MCP subprocess)."""
    global _graph, _unavailable_reason
    _graph = None
    _unavailable_reason = reason


def close_graph() -> None:
    global _graph
    if _graph is not None:
        _graph.close()
        _graph = None
