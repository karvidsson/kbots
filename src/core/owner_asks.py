"""Durable decision requests. Silence records no answer and grants no authority."""

import hashlib
import json
import math
import sqlite3
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

OPEN = ("queued", "posting", "open")
MAX_DELIVERY_ATTEMPTS = 3
DELIVERY_RETRY_DELAYS = (60, 300)
DELIVERY_ERRORS = {
    "goal_budget": "turn budget reached",
    "access_denied": "sender access denied",
    "unknown_agent": "agent unavailable",
    "unknown_connector": "connector unavailable",
    "unknown_skill": "skill unavailable",
    "bot_unavailable": "bot unavailable",
    "provider_error": "agent provider failed",
    "provider_aborted": "agent turn stopped",
    "timeout": "agent turn timed out",
    "interrupted": "agent turn interrupted",
    "unconfirmed": "delivery not confirmed",
}


def settings(config):
    cfg = config.get("waiting_on_you", {}) or {}
    admins = [str(x) for x in config.get("admin_users", {}).get("discord", [])]
    owner = str(cfg.get("owner_id") or (admins[0] if len(admins) == 1 else ""))
    if owner and (not owner.isdecimal() or owner not in admins):
        raise ValueError("waiting_on_you.owner_id must be a configured Discord owner/admin")
    hour = cfg.get("digest_hour", 8)
    if type(hour) is not int or not 0 <= hour <= 23:
        raise ValueError("waiting_on_you.digest_hour must be 0..23")
    remind, stale = cfg.get("remind_after", 14400), cfg.get("stale_after", 604800)
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in (remind, stale)) or not 0 < remind < stale:
        raise ValueError("waiting_on_you requires 0 < remind_after < stale_after (seconds)")
    return dict(
        owner_id=owner,
        enabled=cfg.get("enabled", True) is True,
        digest_hour=hour,
        timezone=ZoneInfo(cfg.get("timezone", "UTC")),
        remind_after=remind,
        stale_after=stale,
    )


def validate(question, default, options=None, context="", request_key=""):
    values = {"question": question, "default": default, "context": context, "request_key": request_key}
    for name, limit in [("question", 400), ("default", 300), ("context", 400), ("request_key", 100)]:
        value = values[name]
        if not isinstance(value, str) or len(value.encode("utf-16-le")) // 2 > limit or "\x00" in value:
            raise ValueError(f"{name} must be text of at most {limit} characters")
        values[name] = value.strip()
    if not values["question"] or not values["default"]:
        raise ValueError("Question and default are required. State what happens if nobody answers.")
    if options is not None:
        if (
            not isinstance(options, list)
            or not 2 <= len(options) <= 5
            or any(not isinstance(x, str) or not x.strip() or len(x.encode("utf-16-le")) // 2 > 50 for x in options)
        ):
            raise ValueError("options must contain 2..5 nonempty labels, at most 50 characters each")
        options = [x.strip() for x in options]
        if len({x.casefold() for x in options}) != len(options):
            raise ValueError("Option labels must be distinct")
    values["options"] = options
    if sum(len(x.encode("utf-16-le")) // 2 for x in (question, default, context, *(options or []))) > 1100:
        raise ValueError("Keep question, context, default and option labels within 1100 characters total")
    return values


class AskStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS owner_asks (
                id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, recipient_id TEXT NOT NULL,
                account TEXT NOT NULL, channel_id TEXT NOT NULL, guild_id TEXT NOT NULL,
                bot_id TEXT NOT NULL, message_id TEXT, payload TEXT NOT NULL,
                request_key TEXT, fingerprint TEXT NOT NULL, created REAL NOT NULL,
                remind_at REAL NOT NULL, stale_at REAL NOT NULL, state TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1, answer TEXT, answered_by TEXT,
                answered_at REAL, reminder_claimed REAL, reminder_status TEXT,
                dirty INTEGER NOT NULL DEFAULT 0, seed_failed INTEGER NOT NULL DEFAULT 0,
                delivery_error TEXT,
                UNIQUE(agent_id, request_key), UNIQUE(account, message_id)
            );
            CREATE INDEX IF NOT EXISTS owner_asks_open ON owner_asks(state, created);
            CREATE TABLE IF NOT EXISTS owner_ask_events (
                id TEXT PRIMARY KEY, ask_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS owner_ask_digests (
                recipient_id TEXT NOT NULL, day TEXT NOT NULL, status TEXT NOT NULL,
                PRIMARY KEY(recipient_id, day)
            );
        """)

        # V1 databases keep all asks, cards and event identities.
        for table, additions in {
            "owner_asks": {"continuation_error": "TEXT", "card_version": "INTEGER NOT NULL DEFAULT 1"},
            "owner_ask_events": {
                "attempts": "INTEGER NOT NULL DEFAULT 0",
                "next_attempt": "REAL NOT NULL DEFAULT 0",
                "last_error": "TEXT",
            },
        }.items():
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(" + table + ")")}
            for name, kind in additions.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    def failed_rows(self, recipient=None):
        query = "SELECT * FROM owner_asks WHERE continuation_error IS NOT NULL"
        args = () if recipient is None else (recipient,)
        if recipient is not None:
            query += " AND recipient_id=?"
        return [self.decode(row) for row in self.db.execute(query + " ORDER BY created DESC", args)]

    def claim_event(self, event_id, now):
        return (
            self.db.execute(
                "UPDATE owner_ask_events SET state='running',attempts=attempts+1 "
                "WHERE id=? AND state='pending' AND attempts<? AND next_attempt<=?",
                (event_id, MAX_DELIVERY_ATTEMPTS, now),
            ).rowcount
            == 1
        )

    def reject_event(self, event_id, reason, *, retryable, now=None):
        now = time.time() if now is None else now
        reason = reason if reason in DELIVERY_ERRORS else "unconfirmed"
        self.db.execute("BEGIN IMMEDIATE")
        try:
            event = self.db.execute("SELECT * FROM owner_ask_events WHERE id=?", (event_id,)).fetchone()
            failed = not retryable or event["attempts"] >= MAX_DELIVERY_ATTEMPTS
            delay = DELIVERY_RETRY_DELAYS[min(max(event["attempts"] - 1, 0), 1)]
            self.db.execute(
                "UPDATE owner_ask_events SET state=?,next_attempt=?,last_error=? WHERE id=?",
                ("failed" if failed else "pending", now + delay, reason, event_id),
            )
            if failed:
                self.db.execute(
                    "UPDATE owner_asks SET continuation_error=?,dirty=1 WHERE id=?", (reason, event["ask_id"])
                )
            self.db.commit()
            return failed
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    def get(self, ask_id):
        return self.decode(self.db.execute("SELECT * FROM owner_asks WHERE id=?", (ask_id,)).fetchone())

    def for_message(self, message_id):
        return self.decode(
            self.db.execute("SELECT * FROM owner_asks WHERE message_id=?", (str(message_id),)).fetchone()
        )

    def rows(self, states=OPEN, recipient=None):
        query = "SELECT * FROM owner_asks WHERE state IN (" + ",".join("?" for _ in states) + ")"
        args = list(states)
        if recipient is not None:
            query += " AND recipient_id=?"
            args.append(recipient)
        return [self.decode(row) for row in self.db.execute(query + " ORDER BY created DESC, id", args)]

    def create(self, *, agent_id, recipient_id, account, channel_id, guild_id, bot_id, payload, cfg, now=None):
        now = time.time() if now is None else now
        key = payload["request_key"] or None
        fingerprint = hashlib.sha256(
            json.dumps([recipient_id, account, channel_id, payload], sort_keys=True).encode()
        ).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = (
                self.db.execute(
                    "SELECT * FROM owner_asks WHERE agent_id=? AND request_key=?", (agent_id, key)
                ).fetchone()
                if key
                else None
            )
            if row:
                if row["fingerprint"] != fingerprint:
                    raise ValueError("request_key already belongs to a different ask")
            else:
                row = self.db.execute(
                    "SELECT * FROM owner_asks WHERE agent_id=? AND fingerprint=? "
                    "AND state IN ('queued','posting','open')",
                    (agent_id, fingerprint),
                ).fetchone()
            if row:
                result = self.decode(row)
            else:
                ask_id = uuid.uuid4().hex
                self.db.execute(
                    """INSERT INTO owner_asks
                    (id,agent_id,recipient_id,account,channel_id,guild_id,bot_id,payload,request_key,fingerprint,
                     created,remind_at,stale_at,state,card_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',2)""",
                    (
                        ask_id,
                        agent_id,
                        recipient_id,
                        account,
                        channel_id,
                        guild_id,
                        bot_id,
                        json.dumps(payload),
                        key,
                        fingerprint,
                        now,
                        now + cfg["remind_after"],
                        now + cfg["stale_after"],
                    ),
                )
                result = self.get(ask_id)
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def update(self, ask_id, **fields):
        allowed = {"message_id", "state", "dirty", "seed_failed", "delivery_error", "reminder_status"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid ask update")
        self.db.execute(
            "UPDATE owner_asks SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?", (*fields.values(), ask_id)
        )

    def finish(self, ask_id, *, answer=None, actor=None, now=None):
        now = time.time() if now is None else now
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.get(ask_id)
            if not row or row["state"] not in OPEN:
                return False
            stale = now >= row["stale_at"]
            if not stale and (row["state"] != "open" or actor != row["recipient_id"] or not answer):
                return False
            self.db.execute(
                """UPDATE owner_asks SET state=?,revision=revision+1,answer=?,answered_by=?,
                answered_at=?,dirty=1 WHERE id=?""",
                ("stale" if stale else "answered", None if stale else answer, None if stale else actor, now, ask_id),
            )
            self.db.execute("INSERT INTO owner_ask_events(id,ask_id) VALUES(?,?)", ("owner-ask:" + ask_id, ask_id))
            return True
        except BaseException:
            self.db.rollback()
            raise
        finally:
            if self.db.in_transaction:
                self.db.commit()

    def claim_reminder(self, ask_id, now):
        return (
            self.db.execute(
                """UPDATE owner_asks SET reminder_claimed=?,reminder_status='uncertain'
            WHERE id=? AND state='open' AND remind_at<=? AND stale_at>? AND reminder_claimed IS NULL""",
                (now, ask_id, now, now),
            ).rowcount
            == 1
        )

    def claim_digest(self, recipient, day):
        return (
            self.db.execute(
                "INSERT OR IGNORE INTO owner_ask_digests VALUES (?,?,'uncertain')", (recipient, day)
            ).rowcount
            == 1
        )


def jump(row):
    if not row["message_id"]:
        return "(card delivery not yet confirmed)"
    return f"https://discord.com/channels/{row['guild_id'] or '@me'}/{row['channel_id']}/{row['message_id']}"


def clean(value):
    return " ".join(str(value).replace("`", "'").split())


def pending_report(rows, now=None, failed_rows=()):
    now = time.time() if now is None else now
    lines = ["Waiting on you"]
    groups = {}
    links = []
    for row in sorted(rows, key=lambda r: (-r["created"], r["id"])):
        groups.setdefault(row["agent_id"], []).append(row)
    for agent, asks in groups.items():
        lines.append(clean(agent))
        for row in asks:
            minutes = max(0, int((now - row["created"]) / 60))
            age = f"{minutes // 1440}d" if minutes >= 1440 else f"{minutes // 60}h" if minutes >= 60 else f"{minutes}m"
            title = clean(row["payload"]["question"])
            if len(title) > 72:
                title = title[:69].rsplit(" ", 1)[0] + "..."
            number = len(links) + 1
            lines.append(f"  {number:>2}  {age:>4}  {title}")
            url = jump(row)
            links.append(f"[{number}: Open ask]({url})" if row["message_id"] else f"{number}: {url}")
    if not rows:
        lines.append("Nothing waiting.")
    if failed_rows:
        lines.append("Agent notification failed (decision remains closed)")
        for row in failed_rows:
            number = len(links) + 1
            reason = DELIVERY_ERRORS.get(row["continuation_error"], "delivery not confirmed")
            lines.append(f"  {number:>2}  {clean(row['agent_id'])}: {reason}")
            url = jump(row)
            links.append(f"[{number}: Open ask]({url})" if row["message_id"] else f"{number}: {url}")
    # Discord does not make URLs inside code blocks tappable. Keep the aligned
    # list fenced and its numbered jump links immediately below the block.
    return "```\n" + "\n".join(lines) + "\n```" + ("\n" + "\n".join(links) if links else "")
