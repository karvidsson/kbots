"""Durable requests for the owner to put a secret into the vault.

An agent names a vault key and says why it needs it; the owner types the value
into a private Discord form and the engine writes it straight to the vault. The
value never reaches the agent, the card or this store. Only keys under an
allowed prefix can be requested, so an agent cannot get the owner to overwrite
the bot token or another integration's credential.
"""

import re
import sqlite3
import time
import uuid
from pathlib import Path

OPEN = ("queued", "posting", "open")
STALE_AFTER = 7 * 86400
DEFAULT_PREFIXES = ("kvm_",)
_KEY = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")


def allowed_prefixes(config: dict) -> tuple[str, ...]:
    raw = (config.get("owner_secrets", {}) or {}).get("allowed_prefixes", DEFAULT_PREFIXES)
    if isinstance(raw, str):
        raw = [raw]
    return tuple(str(p) for p in raw if str(p).strip())


def validate(key, label, purpose, request_key="", prefixes=DEFAULT_PREFIXES):
    """Normalised fields, or ValueError with a message the agent can act on."""
    for name, value, limit in [("key", key, 100), ("label", label, 45), ("purpose", purpose, 300),
                               ("request_key", request_key, 100)]:
        if not isinstance(value, str) or "\x00" in value or len(value) > limit:
            raise ValueError(f"{name} must be text of at most {limit} characters")
    key, label, purpose = key.strip(), " ".join(label.split()), purpose.strip()
    if not _KEY.match(key):
        raise ValueError("key must be lowercase letters, digits, '_', '.' or '-'")
    if not any(key.startswith(p) for p in prefixes):
        raise ValueError(f"key must start with one of: {', '.join(prefixes) or '(none configured)'}")
    if not label or not purpose:
        raise ValueError("label and purpose are required: say what the secret is and why it is needed")
    return {"key": key, "label": label, "purpose": purpose, "request_key": request_key.strip()}


class SecretStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS owner_secrets (
                id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, recipient_id TEXT NOT NULL,
                account TEXT NOT NULL, channel_id TEXT NOT NULL, guild_id TEXT NOT NULL,
                bot_id TEXT NOT NULL, message_id TEXT, key TEXT NOT NULL, label TEXT NOT NULL,
                purpose TEXT NOT NULL, request_key TEXT, created REAL NOT NULL,
                stale_at REAL NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                done_by TEXT, done_at REAL, dirty INTEGER NOT NULL DEFAULT 0,
                UNIQUE(agent_id, request_key)
            )""")

    def close(self):
        self.db.close()

    def get(self, request_id):
        row = self.db.execute("SELECT * FROM owner_secrets WHERE id=?", (request_id,)).fetchone()
        return dict(row) if row else None

    def rows(self, states=OPEN):
        marks = ",".join("?" for _ in states)
        return [dict(r) for r in self.db.execute(
            f"SELECT * FROM owner_secrets WHERE state IN ({marks}) ORDER BY created", tuple(states))]

    def create(self, *, agent_id, recipient_id, account, channel_id, guild_id, bot_id, fields, now=None):
        """A new request, or the open/matching one this agent already has for that key."""
        now = time.time() if now is None else now
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = None
            if fields["request_key"]:
                row = self.db.execute("SELECT * FROM owner_secrets WHERE agent_id=? AND request_key=?",
                                      (agent_id, fields["request_key"])).fetchone()
                if row and row["key"] != fields["key"]:
                    raise ValueError("request_key already belongs to a request for a different key")
            if not row:
                row = self.db.execute(
                    "SELECT * FROM owner_secrets WHERE agent_id=? AND key=? AND state IN ('queued','posting','open')",
                    (agent_id, fields["key"])).fetchone()
            if not row:
                request_id = uuid.uuid4().hex
                self.db.execute(
                    """INSERT INTO owner_secrets (id,agent_id,recipient_id,account,channel_id,guild_id,bot_id,
                       key,label,purpose,request_key,created,stale_at,state)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'queued')""",
                    (request_id, agent_id, recipient_id, account, channel_id, guild_id, bot_id, fields["key"],
                     fields["label"], fields["purpose"], fields["request_key"] or None, now, now + STALE_AFTER))
                row = self.db.execute("SELECT * FROM owner_secrets WHERE id=?", (request_id,)).fetchone()
            self.db.commit()
            return dict(row)
        except BaseException:
            self.db.rollback()
            raise

    def update(self, request_id, **fields):
        allowed = {"message_id", "state", "dirty"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid secret request update")
        self.db.execute("UPDATE owner_secrets SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?",
                        (*fields.values(), request_id))

    def finish(self, request_id, *, state, actor, revision, now=None):
        """Close an open request as 'saved' or 'declined'. Only the recipient, only
        at the revision the control was drawn for, only before it goes stale."""
        now = time.time() if now is None else now
        if state not in ("saved", "declined"):
            raise ValueError("Invalid final state")
        return self.db.execute(
            """UPDATE owner_secrets SET state=?,revision=revision+1,done_by=?,done_at=?,dirty=1
               WHERE id=? AND state='open' AND recipient_id=? AND revision=? AND stale_at>?""",
            (state, actor, now, request_id, actor, revision, now)).rowcount == 1

    def expire(self, now=None):
        now = time.time() if now is None else now
        cur = self.db.execute(
            "UPDATE owner_secrets SET state='stale',revision=revision+1,dirty=1 "
            "WHERE state IN ('queued','posting','open') AND stale_at<=?", (now,))
        return cur.rowcount
