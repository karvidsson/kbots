"""Shared graph memory layer (LadybugDB) — additive to the sqlite memory backend.

Entity/relationship storage only; facts stay in sqlite. One .lbdb file shared by
all agents, with scope attribution on edges (global / agent:<id> / group:<name>)
mirroring SQLiteMemory._scope_filter semantics: entities are shared identifiers,
visibility lives on the edges.

LadybugDB (PyPI: `ladybug`, the Kuzu fork) is fully in-process and SINGLE-WRITER:
only the main kbots process may open the file. The MCP subprocess never calls
init_graph() and gets a clear GraphUnavailableError message instead (see
mcp_server.build_server). Install with: pip install 'kbots[graph]'.
"""

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from src.core.base import PROJECT_ROOT

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
            self._conn = conn
            logger.info(f"Graph memory: LadybugDB open ({self.path})")
            return self._conn

    async def link(self, a: str, rel: str, b: str, *, confidence: float = 0.7,
                   scope: str = "global", created_by: str | None = None) -> dict:
        """Create/refresh a relationship. Idempotent per (a, rel, b)."""
        conn = await self._ensure_open()
        scope = _normalize_scope(scope, created_by)
        ts = _now()
        for name in (a, b):
            await conn.execute(
                "MERGE (e:Entity {name: $name}) "
                "ON CREATE SET e.type = 'entity', e.scope = $scope, "
                "e.created_by = $by, e.created_at = $ts",
                {"name": name, "scope": scope, "by": created_by or "", "ts": ts},
            )
        await conn.execute(
            "MATCH (a:Entity {name: $a}), (b:Entity {name: $b}) "
            "MERGE (a)-[r:Related {rel: $rel}]->(b) "
            "ON CREATE SET r.confidence = $conf, r.scope = $scope, "
            "r.created_by = $by, r.created_at = $ts "
            "ON MATCH SET r.confidence = $conf",
            {"a": a, "b": b, "rel": rel, "conf": float(confidence),
             "scope": scope, "by": created_by or "", "ts": ts},
        )
        return {"a": a, "rel": rel, "b": b, "confidence": confidence, "scope": scope}

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
                f"AND {_SCOPE_FILTER} "
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
        edges scoped to them. Returns number of edges removed (0 or 1)."""
        conn = await self._ensure_open()
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
            f"WITH e, r WHERE r IS NULL OR {_SCOPE_FILTER} "
            "RETURN e.name AS name, e.type AS type, COUNT(r) AS degree "
            f"ORDER BY degree DESC, name LIMIT {limit}",
            {"agent_scope": agent_scope},
        )
        return _rows(result)

    async def export(self, *, agent_id: str | None = None, limit: int = 400) -> dict:
        """All visible edges + their endpoints, for visualization.

        Returns {"nodes": [{name, type, degree}], "edges": [{src, rel, dst, ...}]}.
        """
        limit = max(1, min(int(limit), 2000))
        conn = await self._ensure_open()
        agent_scope = f"agent:{agent_id}" if agent_id else "agent:"
        result = await conn.execute(
            "MATCH (a:Entity)-[r:Related]->(b:Entity) "
            f"WHERE {_SCOPE_FILTER} "
            "RETURN a.name AS src, a.type AS src_type, b.name AS dst, b.type AS dst_type, "
            "r.rel AS rel, r.confidence AS confidence, r.scope AS scope, "
            "r.created_by AS created_by "
            f"LIMIT {limit}",
            {"agent_scope": agent_scope},
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
        clauses = [_SCOPE_FILTER]
        params: dict = {"agent_scope": agent_scope}
        if rel:
            clauses.append("r.rel = $rel")
            params["rel"] = rel
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


# --- Module-level singleton (pattern of src/tools/browser.py _sessions) ---

_graph: GraphMemory | None = None
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


def get_graph() -> GraphMemory:
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
