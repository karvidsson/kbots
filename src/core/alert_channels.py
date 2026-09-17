"""Durable alert source registration, receipts, budgets and setup intents.

Only the engine owns this store. Webhook messages carry identifiers, never tool
instructions or permissions. A registration is a routing grant, not vendor trust.
"""

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path


class AlertError(ValueError):
    """A safe, non-secret status suitable for a user-visible result."""


class UncertainOperationError(AlertError):
    pass


class AlertStore:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "alerts.db"
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, user_id TEXT NOT NULL,
                account TEXT NOT NULL, dm_id TEXT NOT NULL, guild_id TEXT,
                channel_id TEXT UNIQUE, webhook_id TEXT UNIQUE,
                state TEXT NOT NULL DEFAULT 'draft', revision INTEGER NOT NULL DEFAULT 1,
                nonce TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}',
                created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations (
                source_id TEXT NOT NULL, revision INTEGER NOT NULL, step TEXT NOT NULL,
                state TEXT NOT NULL, result TEXT, updated REAL NOT NULL,
                PRIMARY KEY(source_id,revision,step)
            );
            CREATE TABLE IF NOT EXISTS receipts (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL, revision INTEGER NOT NULL,
                event_id TEXT NOT NULL, issue_id TEXT NOT NULL, kind TEXT NOT NULL,
                message_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
                available REAL NOT NULL, lease TEXT, lease_until REAL,
                attempts INTEGER NOT NULL DEFAULT 0, result TEXT, result_message TEXT,
                success INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, updated REAL NOT NULL,
                UNIQUE(source_id,revision,event_id)
            );
            CREATE TABLE IF NOT EXISTS budgets (
                scope TEXT NOT NULL, bucket INTEGER NOT NULL, used INTEGER NOT NULL,
                PRIMARY KEY(scope,bucket)
            );
            CREATE INDEX IF NOT EXISTS pending_alerts ON receipts(state,available);
            CREATE TABLE IF NOT EXISTS alert_revisions (
                source_id TEXT NOT NULL, revision INTEGER NOT NULL, snapshot TEXT NOT NULL,
                cleaned INTEGER NOT NULL DEFAULT 0, removed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(source_id,revision)
            );
            CREATE TABLE IF NOT EXISTS teardowns (
                source_id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, available REAL NOT NULL, error TEXT
            );
            CREATE TABLE IF NOT EXISTS lifecycle_conditions (key TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS lifecycle_notices (
                id TEXT PRIMARY KEY, context TEXT NOT NULL, text TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', message_id TEXT,
                available REAL NOT NULL DEFAULT 0
            );
        """)
        if "waiting" not in {row["name"] for row in self.db.execute("PRAGMA table_info(sources)")}:
            self.db.execute("ALTER TABLE sources ADD COLUMN waiting INTEGER NOT NULL DEFAULT 1")
        if "presentation" not in {row["name"] for row in self.db.execute("PRAGMA table_info(receipts)")}:
            self.db.execute("ALTER TABLE receipts ADD COLUMN presentation TEXT NOT NULL DEFAULT '{}'")
        receipt_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(receipts)")}
        if "evidence" not in receipt_columns:
            self.db.execute("ALTER TABLE receipts ADD COLUMN evidence TEXT NOT NULL DEFAULT '{}'")
        if "evidence_retry" not in receipt_columns:
            self.db.execute("ALTER TABLE receipts ADD COLUMN evidence_retry INTEGER NOT NULL DEFAULT 0")
        # A restart during local lookup must not strand an unfinished draft.
        self.db.execute("UPDATE sources SET waiting=1 WHERE state='draft' AND waiting=0")
        # Existing cleaned rows only establish a disable, never a removal.
        if "removed" not in {row["name"] for row in self.db.execute("PRAGMA table_info(alert_revisions)")}:
            self.db.execute("ALTER TABLE alert_revisions ADD COLUMN removed INTEGER NOT NULL DEFAULT 0")

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    @staticmethod
    def _source(row):
        if row is None:
            return None
        value = dict(row)
        value["config"] = json.loads(value["config"])
        return value

    def get(self, source_id):
        return self._source(self.db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone())

    def channel(self, channel_id):
        # Disabled registrations still reserve their room. Falling back to a
        # wildcard agent after unsubscribe would make old webhooks privileged.
        return self._source(self.db.execute("SELECT * FROM sources WHERE channel_id=?", (str(channel_id),)).fetchone())

    def draft(self, account, user_id, dm_id):
        return self._source(
            self.db.execute(
                "SELECT * FROM sources WHERE account=? AND user_id=? AND dm_id=? "
                "AND state='draft' AND waiting=1 ORDER BY created DESC LIMIT 1",
                (account, str(user_id), str(dm_id)),
            ).fetchone()
        )

    def expire_drafts(self, account, user_id, dm_id, now=None):
        now = time.time() if now is None else now
        with self.transaction():
            rows = self.db.execute(
                "SELECT * FROM sources WHERE account=? AND user_id=? AND dm_id=? "
                "AND state='draft' AND waiting=1 AND updated<=? AND channel_id IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM operations WHERE source_id=sources.id)",
                (account, str(user_id), str(dm_id), now - 1800),
            ).fetchall()
            for row in rows:
                source = self._source(row)
                self.update(source["id"], state="disabled", waiting=0)
                self.notify_setup(
                    source,
                    "Setup expired after 30 minutes without an answer. "
                    "You can chat normally or start again with /alerts create.",
                    "expired",
                )
        return bool(rows)

    def begin(self, owner, user_id, account, dm_id):
        with self.transaction():
            old = self._source(
                self.db.execute(
                    "SELECT * FROM sources WHERE account=? AND user_id=? AND dm_id=? AND state='draft' "
                    "ORDER BY created DESC LIMIT 1",
                    (account, str(user_id), str(dm_id)),
                ).fetchone()
            )
            if old:
                return old
            source_id, nonce, now = str(uuid.uuid4()), uuid.uuid4().hex, time.time()
            self.db.execute(
                "INSERT INTO sources(id,owner,user_id,account,dm_id,nonce,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                (source_id, owner, str(user_id), account, str(dm_id), nonce, now, now),
            )
        return self.get(source_id)

    def update(self, source_id, **values):
        permitted = {"config", "guild_id", "channel_id", "webhook_id", "state", "waiting"}
        if not values or set(values) - permitted:
            raise AlertError("Invalid source update")
        if "config" in values:
            values["config"] = json.dumps(values["config"], sort_keys=True)
        values["updated"] = time.time()
        self.db.execute(
            "UPDATE sources SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?", (*values.values(), source_id)
        )
        return self.get(source_id)

    def disable(self, source_id):
        with self.transaction():
            self.update(source_id, state="disabled")
            self.db.execute(
                "UPDATE receipts SET state='cancelled',updated=? "
                "WHERE source_id=? AND state NOT IN ('complete','cancelled')",
                (time.time(), source_id),
            )

    def rotate(self, source_id):
        with self.transaction():
            source = self.get(source_id)
            self.remember_revision(source)
            config = {k: v for k, v in source["config"].items() if k != "destination_id"}
            self.db.execute(
                "UPDATE sources SET revision=revision+1,nonce=?,webhook_id=NULL,"
                "state='provisioning',config=?,updated=? WHERE id=?",
                (uuid.uuid4().hex, json.dumps(config), time.time(), source_id),
            )
            self.db.execute(
                "UPDATE receipts SET state='cancelled',updated=? WHERE source_id=? AND state!='complete'",
                (time.time(), source_id),
            )
        return self.get(source_id)

    def require_setup(self, source):
        current = self.get(source["id"])
        if (
            not current
            or current["revision"] != source["revision"]
            or current["state"] not in {"provisioning", "provisional"}
        ):
            raise AlertError("Setup was revoked or changed; no further resources will be created")
        return current

    def remember_revision(self, source):
        # References and ownership markers only; never webhook URLs or API keys.
        self.db.execute(
            "INSERT INTO alert_revisions(source_id,revision,snapshot) VALUES(?,?,?) "
            "ON CONFLICT(source_id,revision) DO UPDATE SET snapshot=excluded.snapshot",
            (source["id"], source["revision"], json.dumps(source)),
        )

    def request_teardown(self, source_id, kind, now=None):
        if kind not in {"deleted", "unsubscribe"}:
            raise AlertError("Invalid teardown reason")
        now = time.time() if now is None else now
        with self.transaction():
            source = self.get(source_id)
            if not source:
                return
            self.remember_revision(source)
            previous = self.db.execute("SELECT kind FROM teardowns WHERE source_id=?", (source_id,)).fetchone()
            if previous and previous["kind"] == "deleted":
                kind = "deleted"
            if previous and previous["kind"] != kind:
                self.db.execute("UPDATE alert_revisions SET cleaned=0 WHERE source_id=?", (source_id,))
            self.update(source_id, state="deleting" if kind == "deleted" else "disabled")
            self.db.execute(
                "UPDATE receipts SET state='cancelled',lease=NULL,lease_until=NULL,result=NULL,updated=? "
                "WHERE source_id=? AND state!='complete'",
                (now, source_id),
            )
            self.db.execute(
                "INSERT INTO teardowns(source_id,kind,available) VALUES(?,?,?) "
                "ON CONFLICT(source_id) DO UPDATE SET kind=excluded.kind,"
                "state=CASE WHEN teardowns.kind!=excluded.kind THEN 'pending' ELSE teardowns.state END",
                (source_id, kind, now),
            )

    def retry_teardown(self, source_id):
        self.db.execute(
            "UPDATE teardowns SET state='pending',attempts=0,available=0,error=NULL WHERE source_id=?", (source_id,)
        )

    def notify_setup(self, source, text, outcome):
        self.notify_lifecycle(source, text, f"setup:{source['id']}:{source['revision']}:{outcome}", target="dm")

    def notify_lifecycle(self, source, text, condition=None, target="home"):
        with nullcontext() if self.db.in_transaction else self.transaction():
            if condition:
                if not self.db.execute("INSERT OR IGNORE INTO lifecycle_conditions VALUES(?)", (condition,)).rowcount:
                    return
            context = {k: source[k] for k in ("id", "owner", "account", "channel_id")}
            if target == "dm":
                context.update(
                    target="dm",
                    dm_id=source["dm_id"],
                    user_id=source["user_id"],
                    revision=source["revision"],
                    outcome=condition.rsplit(":", 1)[-1],
                )
            if source.get("dm_id", "").startswith("operator:"):
                context["target"] = "operator"
            self.db.execute(
                "INSERT INTO lifecycle_notices(id,context,text) VALUES(?,?,?)",
                (str(uuid.uuid4()), json.dumps(context), text),
            )

    def clear_condition(self, condition):
        self.db.execute("DELETE FROM lifecycle_conditions WHERE key=?", (condition,))

    def purge_deleted(self, source_id):
        with self.transaction():
            row = self.db.execute("SELECT kind FROM teardowns WHERE source_id=?", (source_id,)).fetchone()
            if not row or row["kind"] != "deleted":
                raise AlertError("Only a confirmed deleted channel can be purged")
            if self.db.execute(
                "SELECT 1 FROM alert_revisions WHERE source_id=? AND removed=0", (source_id,)
            ).fetchone():
                raise AlertError("Destination cleanup is still pending")
            source = self.get(source_id)
            self.notify_lifecycle(
                source, f"Alert setup {source_id} removed after channel deletion. Vendor cleanup confirmed."
            )
            for table in ("receipts", "operations", "alert_revisions", "teardowns"):
                self.db.execute(f"DELETE FROM {table} WHERE source_id=?", (source_id,))
            self.db.execute("DELETE FROM budgets WHERE scope=?", ("diagnosis:" + source_id,))
            self.db.execute("DELETE FROM sources WHERE id=?", (source_id,))

    def intent(self, source, step):
        key = (source["id"], source["revision"], step)
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM operations WHERE source_id=? AND revision=? AND step=?", key
            ).fetchone()
            if row:
                return dict(row)
            self.db.execute("INSERT INTO operations VALUES(?,?,?,'intent',NULL,?)", (*key, time.time()))
        return None  # Only this call may issue the first mutation.

    def finish_operation(self, source, step, result):
        self.db.execute(
            "UPDATE operations SET state='complete',result=?,updated=? WHERE source_id=? AND revision=? AND step=?",
            (json.dumps(result), time.time(), source["id"], source["revision"], step),
        )

    def receive(self, source, *, event_id, issue_id, kind, message_id, now=None, drill=False):
        now = time.time() if now is None else now
        with self.transaction():
            current = self.get(source["id"])
            if (
                not current
                or current["revision"] != source["revision"]
                or current["state"] not in {"provisional", "active"}
            ):
                return "inactive"
            if self.db.execute(
                "SELECT 1 FROM receipts WHERE message_id=? OR (source_id=? AND revision=? AND event_id=?)",
                (str(message_id), source["id"], source["revision"], event_id),
            ).fetchone():
                return "duplicate"
            pending = self.db.execute(
                "SELECT count(*) FROM receipts WHERE source_id=? AND state IN ('pending','running','ready')",
                (source["id"],),
            ).fetchone()[0]
            if pending >= 1000:
                self.update(source["id"], state="paused", config={**current["config"], "paused_from": current["state"]})
                self.notify_setup(
                    current,
                    "Alert monitoring paused because the queue is full. Review the pending diagnoses.",
                    "overflow",
                )
                return "overflow"
            changed = self.db.execute(
                "INSERT OR IGNORE INTO receipts(id,source_id,revision,event_id,issue_id,kind,"
                "message_id,state,available,created,updated,presentation) VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    source["id"],
                    source["revision"],
                    event_id,
                    issue_id,
                    kind,
                    str(message_id),
                    now,
                    now,
                    now,
                    json.dumps(
                        {
                            "drill": drill is True,
                            "setup_test": event_id == str(uuid.uuid5(uuid.UUID(source["id"]), source["nonce"])),
                        }
                    ),
                ),
            ).rowcount
            return "queued" if changed else "duplicate"

    @staticmethod
    def _accounts(accounts):
        if accounts is None:
            return "", ()
        values = tuple(sorted(accounts))
        return " AND s.account IN (" + ",".join("?" for _ in values) + ")", values

    def claim(self, now=None, lease_seconds=360, accounts=None):
        now = time.time() if now is None else now
        account_filter, account_values = self._accounts(accounts)
        with self.transaction():
            self.db.execute(
                "UPDATE receipts SET state='ready',success=0,result=?,updated=? "
                "WHERE state='running' AND lease_until<? AND attempts>=3",
                ("Diagnosis interrupted three times; operator review required. No fix was applied.", now, now),
            )
            self.db.execute(
                "UPDATE receipts SET state='pending',lease=NULL,lease_until=NULL "
                "WHERE state='running' AND lease_until<?",
                (now,),
            )
            row = self.db.execute(
                "SELECT r.* FROM receipts r JOIN sources s ON s.id=r.source_id "
                "WHERE r.state='pending' AND r.available<=? AND r.revision=s.revision "
                "AND s.state IN ('active','provisional')"
                + account_filter
                + " ORDER BY r.evidence_retry DESC,r.created LIMIT 1",
                (now, *account_values),
            ).fetchone()
            if not row:
                return None
            # Twelve diagnoses per source per hour. Excess stays durable and
            # visible; the bot-to-bot chain guard remains unchanged elsewhere.
            bucket = int(now // 3600)
            scope = "diagnosis:" + row["source_id"]
            used = self.db.execute("SELECT used FROM budgets WHERE scope=? AND bucket=?", (scope, bucket)).fetchone()
            if not row["evidence_retry"] and used and used[0] >= 12:
                self.notify_lifecycle(
                    self.get(row["source_id"]),
                    "Diagnosis deferred until the next hour: this app has used its 12 diagnoses for this hour.",
                    f"budget:{row['source_id']}:{bucket}",
                )
                self.db.execute(
                    "UPDATE receipts SET available=? WHERE source_id=? AND state='pending' "
                    "AND evidence_retry=0 AND available<?",
                    ((bucket + 1) * 3600, row["source_id"], (bucket + 1) * 3600),
                )
                return None
            # An empty evidence read has not invoked a model. Its scheduled
            # continuation uses the original diagnosis reservation. Interrupted
            # leases still consume another reservation, as before.
            if not row["evidence_retry"]:
                self.db.execute(
                    "INSERT INTO budgets VALUES(?,?,1) ON CONFLICT(scope,bucket) DO UPDATE SET used=used+1",
                    (scope, bucket),
                )
            lease = uuid.uuid4().hex
            self.db.execute(
                "UPDATE receipts SET state='running',lease=?,lease_until=?,attempts=attempts+1,"
                "evidence_retry=0,updated=? WHERE id=?",
                (lease, now + lease_seconds, now, row["id"]),
            )
            receipt = dict(self.db.execute("SELECT * FROM receipts WHERE id=?", (row["id"],)).fetchone())
            return self._receipt(receipt)

    @staticmethod
    def _receipt(row):
        return {**dict(row), **json.loads(row["presentation"]), "evidence": json.loads(row["evidence"])}

    def checkpoint_evidence(self, receipt, evidence):
        changed = self.db.execute(
            "UPDATE receipts SET evidence=?,updated=? WHERE id=? AND lease=? AND state='running' "
            "AND EXISTS (SELECT 1 FROM sources s WHERE s.id=source_id AND s.revision=receipts.revision "
            "AND s.state IN ('active','provisional'))",
            (json.dumps(evidence), time.time(), receipt["id"], receipt["lease"]),
        ).rowcount
        if changed:
            receipt["evidence"] = evidence
        return changed == 1

    def defer_evidence(self, receipt, issue, now=None):
        """Yield the lease after a confirmed empty read; no sleep or I/O in a transaction."""
        now = time.time() if now is None else now
        evidence = dict(receipt["evidence"])
        if evidence["deadline"] <= now:
            return False
        delays = (5, 10, 15, 20, 30)
        poll = evidence.get("empty_reads", 0)
        available = min(evidence["deadline"], now + delays[min(poll, len(delays) - 1)])
        evidence.update(status="waiting", issue=issue, empty_reads=poll + 1)
        changed = self.db.execute(
            "UPDATE receipts SET evidence=?,evidence_retry=1,state='pending',available=?,"
            "lease=NULL,lease_until=NULL,attempts=attempts-1,updated=? "
            "WHERE id=? AND lease=? AND state='running' AND EXISTS "
            "(SELECT 1 FROM sources s WHERE s.id=source_id AND s.revision=receipts.revision "
            "AND s.state IN ('active','provisional'))",
            (json.dumps(evidence), available, now, receipt["id"], receipt["lease"]),
        ).rowcount
        if changed:
            receipt.update(evidence=evidence, state="pending", available=available)
        return changed == 1

    def next_delay(self, accounts=None):
        account_filter, account_values = self._accounts(accounts)
        row = self.db.execute(
            "SELECT min(r.available) FROM receipts r JOIN sources s ON s.id=r.source_id "
            "WHERE r.state='pending' AND r.revision=s.revision "
            "AND s.state IN ('active','provisional')" + account_filter,
            account_values,
        ).fetchone()
        return 15 if row[0] is None else max(0.1, min(15, row[0] - time.time()))

    def annotate(self, receipt, **values):
        allowed = {"issue_name", "issue_title", "setup_test", "drill", "sample_drill_status", "source_summary"}
        if set(values) - allowed:
            raise AlertError("Invalid incident presentation")
        row = self.db.execute(
            "SELECT presentation FROM receipts WHERE id=? AND lease=? AND state='running'",
            (receipt["id"], receipt["lease"]),
        ).fetchone()
        if not row:
            return
        presentation = {**json.loads(row["presentation"]), **values}
        self.db.execute(
            "UPDATE receipts SET presentation=? WHERE id=? AND lease=? AND state='running'",
            (json.dumps(presentation), receipt["id"], receipt["lease"]),
        )
        receipt.update(presentation)

    def save_result(self, receipt, result, success=True):
        return (
            self.db.execute(
                "UPDATE receipts SET state='ready',result=?,success=?,updated=? WHERE id=? AND lease=? "
                "AND state='running' AND EXISTS (SELECT 1 FROM sources s WHERE s.id=source_id "
                "AND s.revision=receipts.revision AND s.state IN ('active','provisional'))",
                (result, int(success), time.time(), receipt["id"], receipt["lease"]),
            ).rowcount
            == 1
        )

    def pending(self, accounts=None):
        account_filter, account_values = self._accounts(accounts)
        return [
            self._receipt(r)
            for r in self.db.execute(
                "SELECT r.* FROM receipts r JOIN sources s ON s.id=r.source_id "
                "WHERE r.state='pending' AND r.revision=s.revision AND s.state IN ('active','provisional')"
                + account_filter
                + " ORDER BY r.created LIMIT 20",
                account_values,
            )
        ]

    def ready(self, accounts=None):
        account_filter, account_values = self._accounts(accounts)
        return [
            self._receipt(r)
            for r in self.db.execute(
                "SELECT r.* FROM receipts r JOIN sources s ON s.id=r.source_id "
                "WHERE r.state='ready' AND r.revision=s.revision "
                "AND s.state IN ('active','provisional')" + account_filter + " ORDER BY r.created LIMIT 20",
                account_values,
            )
        ]

    def delivered(self, receipt, message_id):
        with self.transaction():
            changed = self.db.execute(
                "UPDATE receipts SET state='complete',result_message=?,updated=? WHERE id=? AND state='ready'",
                (str(message_id), time.time(), receipt["id"]),
            ).rowcount
            source = self.get(receipt["source_id"])
            if not source:
                return
            expected_test = str(uuid.uuid5(uuid.UUID(source["id"]), source["nonce"]))
            if changed and receipt["success"] and receipt["event_id"] == expected_test:
                activated = self.db.execute(
                    "UPDATE sources SET state='active',updated=? WHERE id=? AND state='provisional' AND revision=?",
                    (time.time(), receipt["source_id"], receipt["revision"]),
                ).rowcount
                if activated:
                    link = f"https://discord.com/channels/{source['guild_id']}/{source['channel_id']}"
                    self.notify_setup(
                        source,
                        f"Alerts for {source['config'].get('app', 'your app')} are active. "
                        f"The setup test and diagnosis were delivered. {link}",
                        "active",
                    )
            elif changed and not receipt["success"] and receipt["event_id"] == expected_test:
                self.notify_setup(source, "Alert setup is held: " + receipt["result"], "diagnosis-held")

    def counts(self, source_id):
        return {
            r["state"]: r["n"]
            for r in self.db.execute(
                "SELECT state,count(*) n FROM receipts WHERE source_id=? GROUP BY state", (source_id,)
            )
        }


async def ensure_operation(store, source, step, find, create):
    """One initial dispatch; retries reconcile a persisted intent before acting.

    Absence after an ambiguous outcome is not permission to reissue the POST.
    A deterministic remote marker lets successful-but-unacknowledged work resume.
    """
    previous = store.intent(source, step)
    if previous and previous["state"] == "complete":
        return json.loads(previous["result"])
    matches = await find()
    if len(matches) > 1:
        raise UncertainOperationError(f"Multiple resources match setup step {step}; review required")
    if matches:
        result = matches[0]
    elif previous:
        raise UncertainOperationError(f"Setup step {step} has an unknown outcome; no duplicate submitted")
    else:
        result = await create()
    store.finish_operation(source, step, result)
    return result
