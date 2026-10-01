"""Email draft storage and approval workflow.

Outbound emails park as drafts awaiting owner approval before sending.
Integrates with the owner_asks system for the approval UI.
"""

import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

DRAFT_SCHEMA = """
CREATE TABLE IF NOT EXISTS email_drafts (
    draft_id TEXT PRIMARY KEY,
    ask_id TEXT UNIQUE,
    agent_id TEXT NOT NULL,
    account TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    reply_to_id TEXT,
    attachments TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT,
    created_at REAL NOT NULL,
    resolved_at REAL
);
CREATE INDEX IF NOT EXISTS idx_email_drafts_ask_id ON email_drafts(ask_id);
CREATE INDEX IF NOT EXISTS idx_email_drafts_status ON email_drafts(status);
"""


class EmailDraftStore:
    """Persistent storage for email drafts awaiting approval."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(DRAFT_SCHEMA)

    def close(self):
        self.db.close()

    def create(
        self,
        *,
        agent_id: str,
        account: str,
        recipient: str,
        subject: str,
        body: str,
        reply_to_id: str = "",
        attachments: str = "",
    ) -> dict:
        """Create a new email draft awaiting approval."""
        draft_id = uuid.uuid4().hex[:16]
        now = time.time()
        self.db.execute(
            """INSERT INTO email_drafts
            (draft_id, agent_id, account, recipient, subject, body, reply_to_id,
             attachments, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (draft_id, agent_id, account, recipient, subject, body, reply_to_id,
             attachments, now),
        )
        return self.get(draft_id)

    def get(self, draft_id: str) -> dict | None:
        """Get a draft by ID."""
        row = self.db.execute(
            "SELECT * FROM email_drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_by_ask(self, ask_id: str) -> dict | None:
        """Get a draft by its associated owner_ask ID."""
        row = self.db.execute(
            "SELECT * FROM email_drafts WHERE ask_id = ?", (ask_id,)
        ).fetchone()
        return dict(row) if row else None

    def link_ask(self, draft_id: str, ask_id: str) -> None:
        """Link a draft to its owner_ask."""
        self.db.execute(
            "UPDATE email_drafts SET ask_id = ? WHERE draft_id = ?",
            (ask_id, draft_id),
        )

    def resolve(self, draft_id: str, status: str, result: str = "") -> None:
        """Mark a draft as resolved with the given status."""
        self.db.execute(
            "UPDATE email_drafts SET status = ?, result = ?, resolved_at = ? WHERE draft_id = ?",
            (status, result, time.time(), draft_id),
        )

    def pending(self, agent_id: str | None = None) -> list[dict]:
        """List pending drafts, optionally filtered by agent."""
        query = "SELECT * FROM email_drafts WHERE status = 'pending'"
        args = ()
        if agent_id:
            query += " AND agent_id = ?"
            args = (agent_id,)
        return [dict(row) for row in self.db.execute(query + " ORDER BY created_at DESC", args)]


def truncate_preview(text: str, max_chars: int = 400) -> str:
    """Truncate text for preview, preserving word boundaries."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars].rsplit(" ", 1)[0]
    return truncated + "..." if truncated else text[:max_chars] + "..."


def build_email_context(recipient: str, subject: str, body: str) -> str:
    """Build the context string for the owner_ask card."""
    lines = [f"To: {recipient}", f"Subject: {subject}"]
    body_preview = truncate_preview(body, 300)
    if body_preview:
        lines.append(f"Body: {body_preview}")
    return "\n".join(lines)
