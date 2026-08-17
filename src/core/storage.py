"""SQLite storage for sessions, messages, and tool logs.

All conversation state lives here, not in Python memory.
Agent manager loads history per-request and discards after the LLM call.
"""

import json
import logging
import time
from pathlib import Path

import aiosqlite

from src.core.base import Message, MessageRole

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    channel_id TEXT,
    user_id TEXT,
    cli_session_id TEXT,
    created_at REAL DEFAULT (unixepoch()),
    last_active REAL DEFAULT (unixepoch()),
    summary TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    name TEXT,
    tool_calls TEXT,
    tool_results TEXT,
    tokens_used INTEGER,
    provider TEXT,
    model TEXT,
    created_at REAL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);

CREATE TABLE IF NOT EXISTS tool_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    session_id TEXT,
    tool_name TEXT NOT NULL,
    input TEXT,
    output TEXT,
    success INTEGER,
    duration_ms INTEGER,
    created_at REAL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_tool_log_agent ON tool_log(agent_id, created_at);

CREATE TABLE IF NOT EXISTS counters (
    day TEXT NOT NULL,
    name TEXT NOT NULL,
    count INTEGER DEFAULT 0,
    PRIMARY KEY (day, name)
);

CREATE TABLE IF NOT EXISTS agent_overrides (
    agent_id TEXT NOT NULL,
    setting  TEXT NOT NULL,
    value    TEXT NOT NULL,
    updated_at REAL DEFAULT (unixepoch()),
    PRIMARY KEY (agent_id, setting)
);
"""


def resolve_db_path(data_dir: str | Path) -> Path:
    """Database path inside a data dir."""
    return Path(data_dir) / "kbots.db"


class Storage:
    """Async SQLite storage for kbots."""

    def __init__(self, db_path: str | Path = "data/kbots.db"):
        self._db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Initialize the database and create tables."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        from src.core.base import harden_path
        harden_path(self._db_path)  # holds tool_log args/output — keep owner-only
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(SCHEMA)
        # Migration: provider/model columns on messages (added 2026-07; records
        # which brain produced each assistant reply — tier router made this
        # essential for debugging local-vs-claude turns).
        async with self._db.execute("PRAGMA table_info(messages)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        for col in ("provider", "model"):
            if col not in cols:
                await self._db.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
        await self._db.commit()
        logger.info(f"Storage initialized: {self._db_path}")

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()

    # --- Sessions ---

    async def get_or_create_session(
        self, session_id: str, agent_id: str,
        channel_id: str | None = None, user_id: str | None = None
    ) -> dict:
        """Get an existing session or create a new one."""
        async with self._db.execute(
            "SELECT id, agent_id, channel_id, user_id, cli_session_id, "
            "created_at, last_active, summary "
            "FROM sessions WHERE id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if row:
            await self._db.execute(
                "UPDATE sessions SET last_active = ? WHERE id = ?",
                (time.time(), session_id)
            )
            await self._db.commit()
            return {
                "id": row[0], "agent_id": row[1], "channel_id": row[2],
                "user_id": row[3], "cli_session_id": row[4],
                "created_at": row[5], "last_active": row[6], "summary": row[7],
            }

        now = time.time()
        await self._db.execute(
            "INSERT INTO sessions (id, agent_id, channel_id, user_id, created_at, last_active) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, agent_id, channel_id, user_id, now, now)
        )
        await self._db.commit()
        return {
            "id": session_id, "agent_id": agent_id, "channel_id": channel_id,
            "user_id": user_id, "cli_session_id": None,
            "created_at": now, "last_active": now, "summary": None,
        }

    async def save_cli_session_id(self, session_id: str, cli_session_id: str) -> None:
        """Persist the Claude Code CLI session ID for --resume across restarts."""
        await self._db.execute(
            "UPDATE sessions SET cli_session_id = ? WHERE id = ?",
            (cli_session_id, session_id)
        )
        await self._db.commit()

    async def save_summary(self, session_id: str, summary: str) -> None:
        """Save a conversation summary for fallback context loading."""
        await self._db.execute(
            "UPDATE sessions SET summary = ? WHERE id = ?",
            (summary, session_id)
        )
        await self._db.commit()

    async def latest_channel_for_agent(self, agent_id: str) -> str | None:
        """The agent's most recently active real (non-internal) channel.

        Used to resolve a wildcard-routed agent's home channel for visible
        inter-agent delivery.
        """
        async with self._db.execute(
            "SELECT channel_id FROM sessions WHERE agent_id = ? "
            "AND channel_id IS NOT NULL AND channel_id NOT LIKE 'internal:%' "
            "ORDER BY last_active DESC LIMIT 1", (agent_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    async def prune_stale_sessions(self, max_age_days: int = 30) -> int:
        """Delete sessions (and their messages/tool logs) older than max_age_days.

        Returns the number of sessions pruned.
        """
        cutoff = time.time() - (max_age_days * 86400)
        async with self._db.execute(
            "SELECT id FROM sessions WHERE last_active < ?", (cutoff,)
        ) as cursor:
            stale = [row[0] for row in await cursor.fetchall()]

        if not stale:
            return 0

        placeholders = ",".join("?" * len(stale))
        await self._db.execute(f"DELETE FROM messages WHERE session_id IN ({placeholders})", stale)
        await self._db.execute(f"DELETE FROM tool_log WHERE session_id IN ({placeholders})", stale)
        await self._db.execute(f"DELETE FROM sessions WHERE id IN ({placeholders})", stale)
        await self._db.commit()
        logger.info(f"Pruned {len(stale)} stale sessions (older than {max_age_days} days)")
        return len(stale)

    # --- Agent overrides (durable per-agent runtime settings) ---

    async def get_agent_override(self, agent_id: str, setting: str) -> str | None:
        """Return the persisted override for (agent_id, setting), or None."""
        async with self._db.execute(
            "SELECT value FROM agent_overrides WHERE agent_id = ? AND setting = ?",
            (agent_id, setting),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    async def set_agent_override(self, agent_id: str, setting: str, value: str | None) -> None:
        """Set or clear a persisted override. value=None removes the row."""
        if value is None:
            await self._db.execute(
                "DELETE FROM agent_overrides WHERE agent_id = ? AND setting = ?",
                (agent_id, setting),
            )
        else:
            await self._db.execute(
                "INSERT INTO agent_overrides (agent_id, setting, value, updated_at) "
                "VALUES (?, ?, ?, unixepoch()) "
                "ON CONFLICT(agent_id, setting) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (agent_id, setting, value),
            )
        await self._db.commit()

    async def get_agent_overrides(self, agent_id: str) -> dict[str, str]:
        """Return all overrides for an agent as {setting: value}."""
        async with self._db.execute(
            "SELECT setting, value FROM agent_overrides WHERE agent_id = ?",
            (agent_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}

    # --- Messages ---

    async def save_message(
        self, session_id: str, role: str, content: str,
        name: str | None = None,
        tool_calls: list[dict] | None = None,
        tool_results: list[dict] | None = None,
        tokens_used: int | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> int:
        """Save a message to the database. Returns the message ID."""
        cursor = await self._db.execute(
            "INSERT INTO messages (session_id, role, content, name, tool_calls, "
            "tool_results, tokens_used, provider, model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, role, content, name,
                json.dumps(tool_calls) if tool_calls else None,
                json.dumps(tool_results) if tool_results else None,
                tokens_used, provider, model,
            )
        )
        await self._db.commit()
        return cursor.lastrowid

    async def get_token_usage(self, days: int = 1) -> list[dict]:
        """Total tokens + message count per agent over the last `days`.

        Returns a list of {agent_id, tokens, messages}, highest tokens first.
        """
        cutoff = time.time() - days * 86400
        async with self._db.execute(
            "SELECT s.agent_id, "
            "       COALESCE(SUM(m.tokens_used), 0) AS tokens, "
            "       COUNT(m.id) AS msgs "
            "FROM messages m JOIN sessions s ON m.session_id = s.id "
            "WHERE m.tokens_used IS NOT NULL AND m.created_at >= ? "
            "GROUP BY s.agent_id ORDER BY tokens DESC",
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
        return [{"agent_id": r[0], "tokens": int(r[1]), "messages": int(r[2])} for r in rows]

    async def bump_counter(self, name: str) -> None:
        """Increment a daily counter (routing/outcome metrics). Never raises —
        metrics must not fail a turn."""
        import contextlib
        with contextlib.suppress(Exception):
            day = time.strftime("%Y-%m-%d")
            await self._db.execute(
                "INSERT INTO counters (day, name, count) VALUES (?, ?, 1) "
                "ON CONFLICT(day, name) DO UPDATE SET count = count + 1",
                (day, name),
            )
            await self._db.commit()

    async def get_counters(self, days: int = 7) -> dict[str, int]:
        """Summed counters over the last `days` (by day string)."""
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        async with self._db.execute(
            "SELECT name, SUM(count) FROM counters WHERE day >= ? GROUP BY name",
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
        return {r[0]: int(r[1]) for r in rows}

    async def load_history(self, session_id: str, limit: int = 50) -> list[Message]:
        """Load recent message history for a session.

        Returns the most recent `limit` messages, oldest first.
        Loaded per-request and discarded after the LLM call — never cached.
        """
        async with self._db.execute(
            "SELECT role, content, name, tool_calls, tool_results FROM "
            "(SELECT role, content, name, tool_calls, tool_results, created_at "
            " FROM messages WHERE session_id = ? ORDER BY created_at DESC LIMIT ?) "
            "ORDER BY created_at ASC",
            (session_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()

        messages = []
        for row in rows:
            role_str, content, name, tc_json, tr_json = row
            messages.append(Message(
                role=MessageRole(role_str),
                content=content,
                name=name,
                tool_calls=json.loads(tc_json) if tc_json else None,
                tool_results=json.loads(tr_json) if tr_json else None,
            ))
        return messages

    # --- Tool log ---

    async def log_tool_call(
        self, agent_id: str, session_id: str | None,
        tool_name: str, input_data: dict | None,
        output_data: str | None, success: bool, duration_ms: int,
    ) -> None:
        """Log a tool execution to the database."""
        await self._db.execute(
            "INSERT INTO tool_log (agent_id, session_id, tool_name, input, output, success, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                agent_id, session_id, tool_name,
                json.dumps(input_data) if input_data else None,
                output_data, int(success), duration_ms,
            )
        )
        await self._db.commit()
