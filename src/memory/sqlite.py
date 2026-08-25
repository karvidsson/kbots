"""SQLite memory backend — direct replacement for the legacy memory-api Docker container.

Same schema, same FTS5 indexes, same embedding storage format. All operations
that previously went over HTTP to :8897 now happen in-process.
"""

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from src.core.base import PROJECT_ROOT, MemoryBackend
from src.core.embedding import EmbeddingEngine
from src.lib.canonical import entity_key
from src.memory.query import fts_query

logger = logging.getLogger(__name__)


class SQLiteMemory(MemoryBackend):
    """In-process SQLite memory with FTS5 + vector search."""

    name = "sqlite"

    def __init__(self, config: dict):
        super().__init__(config)
        db_path = config.get("path", "data/memory.db")
        model_dir = config.get("model_dir", "data/models/bge-small-en-v1.5")

        # Resolve relative paths against the repo root, not CWD. MCP tool
        # subprocesses inherit CWD from Claude Code (the agent's project_dir),
        # which would silently create empty per-agent DBs at
        # <project_dir>/data/memory.db instead of sharing the real one. See
        # ARCHITECTURE.md — "All paths must be absolute".
        if not Path(db_path).is_absolute():
            db_path = str(PROJECT_ROOT / db_path)
        if not Path(model_dir).is_absolute():
            model_dir = str(PROJECT_ROOT / model_dir)

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        from src.core.base import harden_path
        harden_path(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA synchronous=NORMAL")
        # Deleting a row normally only unlinks it: the text stays readable in
        # the freed page until something happens to overwrite it. For a store
        # whose delete is called to honour an erasure request, that is the
        # whole point, so pay the small write cost and zero the bytes.
        self.db.execute("PRAGMA secure_delete=ON")

        self.embedding = EmbeddingEngine(model_dir=model_dir)
        self._embed_failures = 0
        # Whether one agent's memories are readable by the rest of the fleet.
        # On by default: these are one owner's agents working on one owner's
        # business, and knowledge that cannot leave the agent that learned it
        # is relearned by seven others. `private:<id>` opts a memory out.
        self.fleet_read = bool(config.get("fleet_read", True))
        self._ensure_schema()

        count = self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        db_size = Path(db_path).stat().st_size / (1024 * 1024)
        logger.info(f"SQLite memory: {count} memories, {db_size:.1f}MB ({db_path})")
        # Said at boot, where it is read, rather than only at the moment of
        # failure. A store whose vectors are missing looks healthy from every
        # angle except the one query that needs them.
        missing = self.missing_embeddings()
        if missing:
            logger.warning(
                f"{missing} of {count} memories have no embedding and are invisible to "
                f"semantic search. Backfill: scripts/memory-backfill.py --only embeddings")

    def missing_embeddings(self) -> int:
        """How many stored memories have no vector."""
        return self.db.execute(
            "SELECT COUNT(*) FROM memories WHERE embedding IS NULL").fetchone()[0]

    def _ensure_schema(self) -> None:
        """Create tables if they don't exist. Matches legacy memory-api schema exactly."""
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL CHECK(type IN ('semantic', 'episodic', 'procedural')),
                content TEXT NOT NULL,
                category TEXT,
                confidence REAL DEFAULT 0.5,
                access_count INTEGER DEFAULT 0,
                last_accessed DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                created_by TEXT,
                scope TEXT DEFAULT 'global',
                scope_target TEXT,
                tags TEXT,
                metadata TEXT,
                pinned INTEGER DEFAULT 0,
                embedding BLOB
            );

            CREATE TABLE IF NOT EXISTS relationships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_a TEXT NOT NULL,
                relationship TEXT NOT NULL,
                entity_b TEXT NOT NULL,
                confidence REAL DEFAULT 0.7,
                created_by TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS changelog (
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                agent TEXT,
                table_name TEXT,
                action TEXT,
                record_id TEXT,
                old_value TEXT,
                new_value TEXT,
                reason TEXT
            );

            -- Entity anchors: which graph entities a memory mentions.
            -- Without these the two stores can only be joined by matching
            -- names in prose, so a memory found by search offers no way into
            -- the graph and an entity found by traversal offers no way back
            -- to the facts that mention it. The reflector already resolves
            -- each extracted edge to its source memory id and used to throw
            -- that link away; this is where it lands now.
            CREATE TABLE IF NOT EXISTS memory_entities (
                memory_id TEXT NOT NULL,
                entity TEXT NOT NULL,
                entity_key TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (memory_id, entity_key)
            );

            CREATE INDEX IF NOT EXISTS idx_memory_entities_key
                ON memory_entities(entity_key);

            CREATE TABLE IF NOT EXISTS tracking (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                date TEXT,
                time TEXT,
                data TEXT,
                notes TEXT,
                created_by TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # FTS5 virtual table — create only if not exists
        # Legacy format uses content='memories', content_rowid='rowid'
        try:
            self.db.execute("SELECT 1 FROM memories_fts LIMIT 1")
        except sqlite3.OperationalError:
            self.db.execute("""
                CREATE VIRTUAL TABLE memories_fts USING fts5(
                    content, category, tags,
                    content='memories', content_rowid='rowid'
                )
            """)
            # Backfill FTS from existing data
            self.db.execute("""
                INSERT INTO memories_fts(rowid, content, category, tags)
                SELECT rowid, content, category, COALESCE(tags, '') FROM memories
            """)

        # Create FTS sync triggers if not present (use rowid, not id)
        for trigger_sql in [
            """CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, content, category, tags)
                VALUES (new.rowid, new.content, new.category, COALESCE(new.tags, ''));
            END""",
            """CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
                VALUES ('delete', old.rowid, old.content, old.category, COALESCE(old.tags, ''));
            END""",
            """CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
                VALUES ('delete', old.rowid, old.content, old.category, COALESCE(old.tags, ''));
                INSERT INTO memories_fts(rowid, content, category, tags)
                VALUES (new.rowid, new.content, new.category, COALESCE(new.tags, ''));
            END""",
        ]:
            self.db.execute(trigger_sql)

        self.db.commit()

    # === Core interface (MemoryBackend ABC) ===

    async def store(self, content: str, type: str, agent_id: str | None = None,
                    tags: list[str] | None = None, **kwargs) -> str:
        """Store a memory. Generates embedding automatically. Returns UUID id."""
        category = kwargs.get("category", "general")
        scope = kwargs.get("scope", "agent")
        scope_target = kwargs.get("scope_target", agent_id)
        pinned = kwargs.get("pinned", 0)
        metadata = kwargs.get("metadata")
        confidence = kwargs.get("confidence", 0.7)

        # Match legacy scope format: 'agent:main' instead of just 'agent'
        if scope == "agent" and agent_id:
            scope = f"agent:{agent_id}"
        elif scope == "private" and agent_id:
            scope = f"private:{agent_id}"

        # Generate embedding. A failure here is survivable — the memory is
        # still stored and still findable by keyword — but it silently removes
        # that memory from semantic search forever, because semantic_search
        # filters on `embedding IS NOT NULL`. It was survivable 237 times in a
        # row on the live fleet without anyone noticing, so it is counted and
        # reported rather than logged once per memory at warning level.
        try:
            vec = self.embedding.embed_one(content)
            blob = EmbeddingEngine.to_blob(vec)
        except Exception as e:
            blob = None
            self._embed_failures += 1
            if self._embed_failures == 1:
                logger.error(f"Embedding generation failed, memories are being stored "
                             f"WITHOUT vectors and will not be reachable by semantic "
                             f"search: {e}")
            elif self._embed_failures % 25 == 0:
                logger.error(f"{self._embed_failures} memories stored without vectors "
                             f"so far. Run scripts/memory-backfill.py --only embeddings "
                             f"once the model is available.")

        memory_id = str(uuid.uuid4())
        tags_json = json.dumps(tags) if tags else None
        meta_json = json.dumps(metadata) if metadata else None

        self.db.execute(
            """INSERT INTO memories (id, type, content, category, confidence, scope,
               scope_target, tags, metadata, pinned, embedding, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (memory_id, type, content, category, confidence, scope, scope_target,
             tags_json, meta_json, pinned, blob, agent_id),
        )

        self._log_change(agent_id, "memories", "insert", memory_id, None, content)
        self.db.commit()
        return memory_id

    def _scope_filter(self, agent_id: str | None, prefix: str = "") -> tuple[str, list]:
        """Build parameterised scope filter. Returns (sql_fragment, params).

        Four scopes, and two of them are new because the old two did not work:

        `global`      everyone.
        `agent:<id>`  authored by <id>. Readable by the whole fleet when
                      `fleet_read` is on, which is the default. Measured on the
                      live store: 235 of 237 memories were agent-scoped and 0
                      were global, so every agent was searching a corpus of its
                      own handful of notes while six weeks of the fleet's
                      lessons sat one row away and unreadable. Nothing chose
                      that: it is the default of memory_store, which no caller
                      ever overrode.
        `private:<id>`only <id>, always, regardless of `fleet_read`. Sharing by
                      default is only defensible if there is somewhere to put
                      the things that should not be shared.
        `group:<name>`members only, from `defaults.memory.groups`. This used to
                      match `group:%` for everyone, so a group scope was a
                      global scope with a misleading name.
        """
        col = f"{prefix}scope" if prefix else "scope"
        target_col = f"{prefix}scope_target" if prefix else "scope_target"
        clauses = [f"{col} LIKE ?"]
        params: list = ["global%"]

        if self.fleet_read:
            clauses.append(f"{col} LIKE ?")
            params.append("agent:%")

        if agent_id:
            clauses.append(f"{col} LIKE ?")
            params.append(f"agent:{agent_id}%")
            clauses.append(f"{col} = ?")
            params.append(f"private:{agent_id}")
            clauses.append(f"({target_col} = ? AND {col} NOT LIKE 'private:%')")
            params.append(agent_id)
            for group in self.groups_for(agent_id):
                clauses.append(f"{col} = ?")
                params.append(f"group:{group}")

        return " OR ".join(clauses), params

    def groups_for(self, agent_id: str) -> list[str]:
        """Groups this agent belongs to, from `defaults.memory.groups`."""
        out = []
        for name, members in (self.config.get("groups") or {}).items():
            if agent_id in (members or []):
                out.append(str(name))
        return out

    async def search(self, query: str, agent_id: str | None = None,
                     limit: int = 10, **kwargs) -> list[dict]:
        """FTS5 keyword search with scope filtering.

        The query is rewritten into a safe FTS5 expression before it reaches
        MATCH. Callers pass raw user text here (auto-recall passes the whole
        message), and a sentence is not an FTS5 query: on the live store, 71%
        of real user messages raised a syntax error and fell through to a
        `LIKE '%whole message%'` that matched nothing. See memory/query.py.
        """
        type_filter = kwargs.get("type")
        category_filter = kwargs.get("category")

        scope_sql, scope_params = self._scope_filter(agent_id, prefix="m.")

        match_expr = fts_query(query)
        if match_expr is None:
            # Nothing searchable in the input. Returning everything would be
            # worse than returning nothing, so return nothing.
            return []

        sql = f"""
            SELECT m.*, rank
            FROM memories_fts fts
            JOIN memories m ON m.rowid = fts.rowid
            WHERE memories_fts MATCH ?
              AND ({scope_sql})
              AND m.scope NOT LIKE 'archived%'
        """
        params: list = [match_expr] + scope_params

        if type_filter:
            sql += " AND m.type = ?"
            params.append(type_filter)
        if category_filter:
            sql += " AND m.category = ?"
            params.append(category_filter)

        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)

        try:
            rows = self.db.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # Should be unreachable now that the expression is built rather
            # than passed through, but a corrupt FTS index lands here too.
            # Fall back on the longest single term, NOT the whole input: the
            # old `LIKE '%<entire user message>%'` could only match a memory
            # that contained the message verbatim, so it always returned zero.
            longest = max(match_expr.replace('"', "").split(" OR "),
                          key=len, default="")
            rows = self.db.execute(
                f"""SELECT m.*, 0 as rank FROM memories m
                    WHERE m.content LIKE ?
                      AND ({scope_sql})
                      AND m.scope NOT LIKE 'archived%'
                    ORDER BY m.updated_at DESC LIMIT ?""",
                [f"%{longest}%"] + scope_params + [limit],
            ).fetchall()

        results = [self._row_to_dict(r) for r in rows]

        # Update access counts + last_accessed
        now = datetime.utcnow().isoformat()
        for r in results:
            self.db.execute(
                "UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                (now, r["id"]),
            )
        if results:
            self.db.commit()

        return results

    async def semantic_search(self, query: str, agent_id: str | None = None,
                              limit: int = 5) -> list[dict]:
        """Embedding-based semantic search with cosine similarity."""
        try:
            query_vec = self.embedding.embed_one(query)
        except Exception as e:
            logger.warning(f"Embedding failed for semantic search: {e}")
            return await self.search(query, agent_id, limit)

        scope_sql, scope_params = self._scope_filter(agent_id)

        rows = self.db.execute(
            f"""SELECT id, content, type, category, confidence, tags, pinned,
                       created_at, created_by, embedding
                FROM memories
                WHERE embedding IS NOT NULL
                  AND ({scope_sql})
                  AND scope NOT LIKE 'archived%'""",
            scope_params,
        ).fetchall()

        # Score by cosine similarity
        scored = []
        for row in rows:
            vec = EmbeddingEngine.from_blob(row["embedding"])
            sim = EmbeddingEngine.cosine_similarity(query_vec, vec)
            d = dict(row)
            del d["embedding"]
            d["similarity"] = round(sim, 4)
            scored.append(d)

        scored.sort(key=lambda x: x["similarity"], reverse=True)

        # Update access counts + last_accessed
        now = datetime.utcnow().isoformat()
        for r in scored[:limit]:
            self.db.execute(
                "UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                (now, r["id"]),
            )
        if scored[:limit]:
            self.db.commit()

        return scored[:limit]

    # === Entity anchors (the join between this store and the graph) ===

    async def anchor_entities(self, memory_id: str, entities) -> int:
        """Record that `memory_id` mentions these graph entities.

        Idempotent: re-extracting the same memory re-asserts the same anchors
        rather than duplicating them. Keyed on the canonical entity key, so
        "Dr. Sable" and "Dr.Sable" anchor the same memory once.
        """
        n = 0
        for name in entities or []:
            key = entity_key(str(name))
            if not key:
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO memory_entities (memory_id, entity, entity_key) "
                "VALUES (?, ?, ?)", (str(memory_id), str(name), key))
            n += 1
        if n:
            self.db.commit()
        return n

    async def entities_for_memories(self, memory_ids) -> list[str]:
        """Entities anchored to any of these memories, most-mentioned first.

        Ordered rather than DISTINCT-in-scan-order because the caller takes
        only the top few as traversal anchors. An entity mentioned by several
        of the hits is what the results are collectively about; one mentioned
        by a single hit is a detail of that one memory. Unordered, which anchor
        got the budget depended on SQLite's scan order.
        """
        ids = [str(m) for m in memory_ids or []]
        if not ids:
            return []
        rows = self.db.execute(
            f"""SELECT entity, COUNT(*) AS n FROM memory_entities
                WHERE memory_id IN ({','.join('?' * len(ids))})
                GROUP BY entity_key
                ORDER BY n DESC, entity""", ids).fetchall()
        return [r["entity"] for r in rows]

    async def memories_for_entities(self, entities, agent_id: str | None = None,
                                    limit: int = 10) -> list[dict]:
        """Memories anchored to any of these entities, scope-filtered.

        This is the step that makes a graph hop worth taking: traversal yields
        entity names, and what the agent needs is the facts about them.
        """
        keys = [k for k in (entity_key(str(e)) for e in entities or []) if k]
        if not keys:
            return []
        scope_sql, scope_params = self._scope_filter(agent_id, prefix="m.")
        rows = self.db.execute(
            f"""SELECT DISTINCT m.* FROM memories m
                JOIN memory_entities me ON me.memory_id = m.id
                WHERE me.entity_key IN ({','.join('?' * len(keys))})
                  AND ({scope_sql})
                  AND m.scope NOT LIKE 'archived%'
                ORDER BY m.pinned DESC, m.confidence DESC, m.updated_at DESC
                LIMIT ?""",
            keys + scope_params + [limit],
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def forget(self, memory_id: int | str) -> None:
        """Delete a memory by ID (string UUID or int rowid). Content does not survive.

        Forgetting used to leave the text in the database twice over. `store`
        writes the full content into changelog.new_value, and `forget` wrote it
        again into changelog.old_value, so a memory deleted on request read
        clean by SELECT and was still there in full. Both records are purged
        here, along with the row and its FTS entry.

        What remains is a content-free tombstone: which id was deleted, when,
        and by whom. That keeps the audit trail answering "was this removed?"
        without being a copy of the thing removed.
        """
        row = self.db.execute("SELECT id FROM memories WHERE id = ?",
                              (str(memory_id),)).fetchone()
        if row:
            record_id = str(memory_id)
            self.db.execute("DELETE FROM memories WHERE id = ?", (record_id,))
        else:
            # Legacy callers may pass a rowid; resolve it to the real id so the
            # changelog purge below targets the right records.
            row = self.db.execute("SELECT id FROM memories WHERE rowid = ?",
                                  (memory_id,)).fetchone()
            record_id = str(row["id"]) if row else str(memory_id)
            self.db.execute("DELETE FROM memories WHERE rowid = ?", (memory_id,))

        # Every prior trace of this record, including the insert that carried
        # the content and any update diffs.
        self.db.execute("DELETE FROM changelog WHERE table_name = 'memories' AND record_id = ?",
                        (record_id,))
        # Anchors name entities the deleted memory mentioned. Leaving them
        # behind would keep a forgotten memory reachable by entity and leak
        # which entities it was about.
        self.db.execute("DELETE FROM memory_entities WHERE memory_id = ?", (record_id,))
        self._log_change(None, "memories", "delete", record_id, None, None)
        self.db.commit()

        # secure_delete zeroes the page as written, but the pre-delete image is
        # still sitting in the write-ahead log until it is folded back in. A
        # checkpoint is what actually removes it from disk.
        try:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as e:
            logger.warning(f"forget({record_id}): WAL checkpoint failed, deleted content "
                           f"may persist in the -wal file until the next one: {e}")

    # === Extended operations (used by tools and agent_manager) ===

    async def context(self, agent_id: str, limit: int = 20) -> list[dict]:
        """Fetch pinned + high-confidence recent memories for agent context."""
        scope_sql, scope_params = self._scope_filter(agent_id)

        rows = self.db.execute(
            f"""SELECT * FROM memories
                WHERE ({scope_sql})
                  AND scope NOT LIKE 'archived%'
                  AND (pinned = 1 OR confidence >= 0.8)
                ORDER BY pinned DESC, confidence DESC, updated_at DESC
                LIMIT ?""",
            scope_params + [limit],
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get(self, memory_id: int) -> dict | None:
        """Get a single memory by ID."""
        row = self.db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    async def list_by_category(self, agent_id: str, category: str,
                               limit: int = 100) -> list[dict]:
        """List an agent's non-archived memories in a category (e.g. 'lesson').

        Unlike search() this needs no FTS query — used by the reflector to
        consolidate all of an agent's lessons regardless of confidence.
        """
        scope_sql, scope_params = self._scope_filter(agent_id)
        rows = self.db.execute(
            f"""SELECT * FROM memories
                WHERE ({scope_sql})
                  AND scope NOT LIKE 'archived%'
                  AND category = ?
                ORDER BY confidence DESC, updated_at DESC
                LIMIT ?""",
            scope_params + [category, limit],
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def list_since(self, agent_id: str, since: tuple[str, str] | None = None,
                         limit: int = 100) -> list[dict]:
        """An agent's visible memories after the cursor (any category), oldest
        first — a stable cursor for incremental consumers (the reflector's
        graph extraction walks the store batch by batch).

        `since` is (updated_at, id) of the last row already consumed. The id
        tiebreak makes the walk lossless across rows sharing one second-
        precision timestamp, without ever re-reading a full batch."""
        s_ts, s_id = since or ("", "")
        scope_sql, scope_params = self._scope_filter(agent_id)
        rows = self.db.execute(
            f"""SELECT * FROM memories
                WHERE ({scope_sql})
                  AND scope NOT LIKE 'archived%'
                  AND (updated_at > ? OR (updated_at = ? AND id > ?))
                ORDER BY updated_at ASC, id ASC
                LIMIT ?""",
            scope_params + [s_ts, s_ts, s_id, limit],
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def update(self, memory_id: int, agent_id: str | None = None, **fields) -> bool:
        """Update fields on a memory."""
        if not fields:
            return False
        sets = []
        params = []
        for k, v in fields.items():
            if k in ("content", "type", "category", "confidence", "scope",
                     "scope_target", "tags", "metadata", "pinned"):
                if k == "tags" and isinstance(v, list):
                    v = json.dumps(v)
                if k == "metadata" and isinstance(v, dict):
                    v = json.dumps(v)
                sets.append(f"{k} = ?")
                params.append(v)
        if not sets:
            return False

        sets.append("updated_at = CURRENT_TIMESTAMP")
        params.append(memory_id)
        self.db.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", params)

        # Re-embed if content changed
        if "content" in fields:
            try:
                vec = self.embedding.embed_one(fields["content"])
                self.db.execute(
                    "UPDATE memories SET embedding = ? WHERE id = ?",
                    (EmbeddingEngine.to_blob(vec), memory_id),
                )
            except Exception:
                pass

        self._log_change(agent_id, "memories", "update", str(memory_id), None,
                         json.dumps(fields))
        self.db.commit()
        return True

    # === Relationships ===

    async def add_relationship(self, entity_a: str, rel: str, entity_b: str,
                               confidence: float = 0.7, agent_id: str | None = None) -> int:
        cursor = self.db.execute(
            """INSERT INTO relationships (entity_a, relationship, entity_b, confidence, created_by)
               VALUES (?, ?, ?, ?, ?)""",
            (entity_a, rel, entity_b, confidence, agent_id),
        )
        self.db.commit()
        return cursor.lastrowid

    async def query_relationships(self, entity: str, depth: int = 1) -> list[dict]:
        """Query entity relationships with multi-hop traversal."""
        seen = set()
        results = []
        frontier = [entity]

        for _ in range(depth):
            if not frontier:
                break
            next_frontier = []
            for ent in frontier:
                if ent in seen:
                    continue
                seen.add(ent)
                rows = self.db.execute(
                    """SELECT * FROM relationships
                       WHERE entity_a = ? OR entity_b = ?""",
                    (ent, ent),
                ).fetchall()
                for r in rows:
                    d = dict(r)
                    results.append(d)
                    other = d["entity_b"] if d["entity_a"] == ent else d["entity_a"]
                    if other not in seen:
                        next_frontier.append(other)
            frontier = next_frontier

        return results

    async def list_entities(self) -> list[dict]:
        """List all entities ranked by connection count."""
        rows = self.db.execute("""
            SELECT entity, COUNT(*) as connections FROM (
                SELECT entity_a as entity FROM relationships
                UNION ALL
                SELECT entity_b as entity FROM relationships
            ) GROUP BY entity ORDER BY connections DESC
        """).fetchall()
        return [dict(r) for r in rows]

    # === Maintenance ===

    async def dedup(self, threshold: float = 0.80) -> dict:
        """Semantic deduplication — find and merge near-duplicate memories."""
        rows = self.db.execute(
            "SELECT id, content, embedding FROM memories WHERE embedding IS NOT NULL"
        ).fetchall()

        duplicates = []
        removed = 0
        checked = set()

        for i, row_a in enumerate(rows):
            if row_a["id"] in checked:
                continue
            vec_a = EmbeddingEngine.from_blob(row_a["embedding"])
            for row_b in rows[i + 1:]:
                if row_b["id"] in checked:
                    continue
                vec_b = EmbeddingEngine.from_blob(row_b["embedding"])
                sim = EmbeddingEngine.cosine_similarity(vec_a, vec_b)
                if sim >= threshold:
                    # Keep the older one, remove the newer
                    duplicates.append({
                        "kept": row_a["id"],
                        "removed": row_b["id"],
                        "similarity": round(sim, 4),
                    })
                    self.db.execute("DELETE FROM memories WHERE id = ?", (row_b["id"],))
                    checked.add(row_b["id"])
                    removed += 1

        self.db.commit()
        return {"duplicates_found": len(duplicates), "removed": removed, "details": duplicates[:20]}

    # Categories decay never touches. A lesson is the most expensive kind of
    # memory the fleet holds: it exists because something went wrong once and
    # somebody paid for finding out. Forgetting it means paying again, and a
    # lesson that has not been recalled for sixty days is not stale, it is a
    # mistake that has not recurred yet.
    IMMUNE_CATEGORIES = ("lesson",)

    async def decay(self, decay_rate: float = 0.0108, archive_threshold: float = 0.05,
                    purge: bool = False, purge_after_days: int = 90) -> dict:
        """Decay confidence of unaccessed memories, and archive the faded ones.

        Memories not accessed in 24h lose confidence at a flat rate. From the
        default 0.7 at 0.0108/day that is roughly 60 days to the archive
        threshold. Recall resets last_accessed, so anything the fleet actually
        uses never decays.

            Day 0:  0.70 (new memory)
            Day 30: 0.38
            Day 60: 0.05 -> archived (hidden from every read, still on disk)
            Accessed -> decay pauses
            Pinned, or category in IMMUNE_CATEGORIES -> never decays

        `purge` deletes archived memories older than `purge_after_days`. It is
        OFF by default and the caller has to ask for it: archiving is
        reversible by editing one column and deleting is not, so the two do not
        belong behind the same switch. This was previously unconditional, which
        made "decay" a synonym for "delete in 150 days" on a store where
        nothing had ever been recalled.
        """
        yesterday = (datetime.utcnow() - timedelta(days=1)).isoformat()
        immune = ",".join("?" * len(self.IMMUNE_CATEGORIES))

        # Decay: reduce confidence for non-pinned memories not accessed in 24h
        cursor = self.db.execute(
            f"""UPDATE memories
               SET confidence = MAX(confidence - ?, 0.0),
                   updated_at = CURRENT_TIMESTAMP
               WHERE pinned = 0
                 AND scope NOT LIKE 'archived%'
                 AND (category IS NULL OR category NOT IN ({immune}))
                 AND (last_accessed IS NULL OR last_accessed < ?)""",
            (decay_rate, *self.IMMUNE_CATEGORIES, yesterday),
        )
        decayed = cursor.rowcount

        # Archive: memories below threshold. Not a delete — the row keeps its
        # original scope behind an `archived:` prefix, so restoring one is a
        # string edit rather than a restore from backup.
        cursor = self.db.execute(
            f"""UPDATE memories
               SET scope = 'archived:' || scope,
                   updated_at = CURRENT_TIMESTAMP
               WHERE pinned = 0
                 AND scope NOT LIKE 'archived%'
                 AND (category IS NULL OR category NOT IN ({immune}))
                 AND confidence < ?""",
            (*self.IMMUNE_CATEGORIES, archive_threshold),
        )
        archived = cursor.rowcount

        if decayed or archived:
            self._log_change("system", "memories", "decay",
                             "batch", None,
                             json.dumps({"decayed": decayed, "archived": archived}))
            self.db.commit()

        purged = 0
        if purge:
            purge_cutoff = (datetime.utcnow()
                            - timedelta(days=purge_after_days)).isoformat()
            cursor = self.db.execute(
                """DELETE FROM memories
                   WHERE scope LIKE 'archived%'
                     AND updated_at < ?""",
                (purge_cutoff,),
            )
            purged = cursor.rowcount
            if purged:
                self._log_change("system", "memories", "purge",
                                 "batch", None,
                                 json.dumps({"purged": purged}))
                self.db.commit()

        return {"decayed": decayed, "archived": archived, "purged": purged,
                "decay_rate": decay_rate, "archive_threshold": archive_threshold,
                "purge_enabled": purge}

    async def backfill_embeddings(self, batch_size: int = 50) -> dict:
        """Generate embeddings for memories that don't have them yet."""
        rows = self.db.execute(
            "SELECT id, content FROM memories WHERE embedding IS NULL LIMIT ?",
            (batch_size,),
        ).fetchall()

        if not rows:
            return {"backfilled": 0, "message": "All memories have embeddings"}

        texts = [r["content"] for r in rows]
        ids = [r["id"] for r in rows]

        try:
            vectors = self.embedding.embed(texts)
        except Exception as e:
            return {"backfilled": 0, "error": str(e)}

        for mid, vec in zip(ids, vectors):
            self.db.execute(
                "UPDATE memories SET embedding = ? WHERE id = ?",
                (EmbeddingEngine.to_blob(vec), mid),
            )
        self.db.commit()

        remaining = self.db.execute(
            "SELECT COUNT(*) FROM memories WHERE embedding IS NULL"
        ).fetchone()[0]

        return {"backfilled": len(ids), "remaining": remaining}

    async def status(self) -> dict:
        """Memory system status — counts, types, db size."""
        total = self.db.execute("SELECT COUNT(*) FROM memories WHERE scope NOT LIKE 'archived%'").fetchone()[0]
        pinned = self.db.execute("SELECT COUNT(*) FROM memories WHERE pinned = 1").fetchone()[0]
        archived = self.db.execute("SELECT COUNT(*) FROM memories WHERE scope LIKE 'archived%'").fetchone()[0]

        types = {}
        for row in self.db.execute(
            "SELECT type, COUNT(*) as cnt FROM memories WHERE scope NOT LIKE 'archived%' GROUP BY type"
        ).fetchall():
            types[row["type"]] = row["cnt"]

        # Last 24h activity
        yesterday = (datetime.utcnow() - timedelta(days=1)).isoformat()
        inserts = self.db.execute(
            "SELECT COUNT(*) FROM memories WHERE created_at > ?", (yesterday,)
        ).fetchone()[0]

        db_path = self.db.execute("PRAGMA database_list").fetchone()[2]
        db_size = Path(db_path).stat().st_size / (1024 * 1024) if db_path else 0

        embedded = self.db.execute(
            "SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL AND scope NOT LIKE 'archived%'"
        ).fetchone()[0]

        # Low confidence memories at risk of archival
        at_risk = self.db.execute(
            "SELECT COUNT(*) FROM memories WHERE pinned = 0 AND confidence < 0.15 AND scope NOT LIKE 'archived%'"
        ).fetchone()[0]

        return {
            "total": total,
            "pinned": pinned,
            "archived": archived,
            "at_risk": at_risk,
            "by_type": types,
            "embedded": embedded,
            "last_24h_inserts": inserts,
            "db_size_mb": round(db_size, 2),
        }

    # === Internal helpers ===

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """Convert a Row to dict, parsing JSON fields and stripping embedding blob."""
        d = dict(row)
        d.pop("embedding", None)  # never expose raw blob
        # Parse JSON fields
        for field in ("tags", "metadata"):
            if field in d and isinstance(d[field], str):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    def _log_change(self, agent: str | None, table: str, action: str,
                    record_id: str, old_value: str | None, new_value: str | None) -> None:
        """Write to changelog for audit trail."""
        self.db.execute(
            """INSERT INTO changelog (agent, table_name, action, record_id, old_value, new_value)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (agent or "system", table, action, record_id, old_value, new_value),
        )

    def close(self) -> None:
        """Close the database connection."""
        self.db.close()
