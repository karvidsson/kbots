"""Issue-level fix dedupe, explicit manual retries and a durable card outbox."""

import hashlib
import json
import time
import uuid

from src.core.alert_channels import AlertError


def declared_drill(source, receipt):
    return bool(
        receipt.get("drill")
        or receipt.get("setup_test")
        or receipt.get("sample_drill_status") == "drill"
        or receipt.get("evidence", {}).get("issue", {}).get("sample", {}).get("drill_status") == "drill"
        or receipt.get("event_id") == str(uuid.uuid5(uuid.UUID(source["id"]), source["nonce"]))
    )


class FixJobs:
    def __init__(self, store, limit=3):
        self.store, self.db = store, store.db
        self.limit = max(1, min(20, int(limit)))
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS alert_fixes (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL, revision INTEGER NOT NULL,
                issue_id TEXT NOT NULL, state TEXT NOT NULL, lease TEXT, lease_until REAL,
                attempts INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alert_fix_cards (
                receipt_id TEXT PRIMARY KEY, fix_id TEXT NOT NULL, delivered REAL NOT NULL DEFAULT 0
            );
        """)
        if "started" not in {row[1] for row in self.db.execute("PRAGMA table_info(alert_fixes)")}:
            self.db.execute("ALTER TABLE alert_fixes ADD COLUMN started REAL")
        if "cycle" not in {row[1] for row in self.db.execute("PRAGMA table_info(alert_fixes)")}:
            self.db.execute("ALTER TABLE alert_fixes ADD COLUMN cycle INTEGER NOT NULL DEFAULT 0")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS alert_fix_runs (job_id TEXT,cycle INTEGER,source_id TEXT,"
            "started REAL,result TEXT NOT NULL DEFAULT '{}',PRIMARY KEY(job_id,cycle))"
        )
        self.db.execute(
            "INSERT OR IGNORE INTO alert_fix_runs(job_id,cycle,source_id,started) "
            "SELECT id,cycle,source_id,COALESCE(started,created) FROM alert_fixes WHERE attempts>0"
        )

    @staticmethod
    def decode(row):
        return (
            {**dict(row), "payload": json.loads(row["payload"]), "result": json.loads(row["result"])} if row else None
        )

    def get(self, identifier):
        return self.decode(self.db.execute("SELECT * FROM alert_fixes WHERE id=?", (identifier,)).fetchone())

    def enqueue(self, source, receipt, *, manual=False):
        context = receipt.get("fix_context") or {}
        if not context.get("evidence"):
            sample = receipt.get("evidence", {}).get("issue", {}).get("sample", {})
            context = {
                **context,
                "evidence": {},
                "trigger_unmarked": context.get(
                    "trigger_unmarked",
                    sample.get("matches_trigger") is True and sample.get("drill_status") == "unmarked",
                ),
            }
        repo = source["config"].get("repo", "")
        config = source["config"]
        key = hashlib.sha256(
            json.dumps(
                [config.get("service"), config.get("host"), config.get("project"), repo, receipt["issue_id"]]
            ).encode()
        ).hexdigest()
        reason = None
        if declared_drill(source, receipt):
            reason = "drill or setup check"
        elif not receipt["success"]:
            reason = "diagnosis is held"
        elif receipt["kind"] not in {"$error_tracking_issue_created", "$error_tracking_issue_reopened"}:
            reason = "only created and reopened incidents start fixes"
        elif not config.get("repo"):
            reason = "no registered repository"
        elif not manual and context.get("trigger_unmarked") is not True:
            reason = "trigger drill classification could not be confirmed"
        elif not manual and (
            not context.get("evidence", {}).get("frame_matches") or not context.get("evidence", {}).get("snippets")
        ):
            reason = "no tracked source file resolved"
        # Skips for drills must not claim an entire issue and block a later real event.
        drill = declared_drill(source, receipt)
        if drill:
            key = hashlib.sha256((key + receipt["id"]).encode()).hexdigest()
        now = time.time()
        payload = {
            "context": context,
            "diagnosis": receipt["result"],
            "receipt_id": receipt["id"],
            "manual": manual,
            "drill_status_unconfirmed": manual and context.get("trigger_unmarked") is not True,
        }
        self.db.execute(
            "INSERT OR IGNORE INTO alert_fixes(id,source_id,revision,issue_id,state,payload,result,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                key,
                source["id"],
                source["revision"],
                receipt["issue_id"],
                "skipped"
                if drill
                else "waiting"
                if not manual and source["config"].get("auto_fix_pr") is not True
                else "skipped"
                if reason
                else "pending",
                json.dumps(payload),
                json.dumps({"reason": reason} if reason else {}),
                now,
                now,
            ),
        )
        self.db.execute("INSERT OR IGNORE INTO alert_fix_cards(receipt_id,fix_id) VALUES(?,?)", (receipt["id"], key))
        job = self.get(key)
        if manual and not drill and job["state"] in {"waiting", "failed", "skipped"} and not job["result"].get("pr"):
            # An uncertain publication is only reconciled; never discard its intent.
            uncertain = job["result"].get("push_intent") or job["result"].get("pr_intent")
            if uncertain and job["payload"].get("drill_status_unconfirmed") is True:
                payload["drill_status_unconfirmed"] = True
            result = job["result"] if uncertain else ({"reason": reason} if reason else {})
            self.db.execute(
                "UPDATE alert_fix_runs SET result=? WHERE job_id=? AND cycle=?",
                (json.dumps(job["result"]), key, job["cycle"]),
            )
            self.db.execute(
                "UPDATE alert_fixes SET source_id=?,revision=?,state=?,payload=?,result=?,lease=NULL,lease_until=NULL,"
                "attempts=0,started=NULL,cycle=cycle+1,updated=? WHERE id=?",
                (
                    source["id"],
                    source["revision"],
                    "skipped" if reason else "pending",
                    json.dumps(payload),
                    json.dumps(result),
                    now,
                    key,
                ),
            )
        if not manual and source["config"].get("auto_fix_pr") is True and job["state"] == "waiting":
            self.db.execute(
                "UPDATE alert_fixes SET state=?,payload=?,result=?,updated=? WHERE id=?",
                (
                    "skipped" if reason else "pending",
                    json.dumps(payload),
                    json.dumps({"reason": reason} if reason else {}),
                    now,
                    key,
                ),
            )
        return self.get(key)

    def for_receipt(self, receipt):
        row = self.db.execute("SELECT fix_id FROM alert_fix_cards WHERE receipt_id=?", (receipt["id"],)).fetchone()
        return self.get(row[0]) if row else None

    def refresh_cards(self, source_id):
        self.db.execute(
            "UPDATE alert_fix_cards SET delivered=0 WHERE receipt_id IN (SELECT id FROM receipts WHERE source_id=?)",
            (source_id,),
        )

    def claim(self, accounts=None, now=None):
        now = time.time() if now is None else now
        with self.store.transaction():
            rows = self.db.execute(
                "SELECT f.* FROM alert_fixes f JOIN sources s ON s.id=f.source_id AND s.revision=f.revision "
                "WHERE s.state='active' AND (f.state='pending' OR (f.state='running' AND f.lease_until<?)) "
                "ORDER BY f.created",
                (now,),
            ).fetchall()
            for row in rows:
                source = self.store.get(row["source_id"])
                if accounts is not None and source["account"] not in accounts:
                    continue
                payload = json.loads(row["payload"])
                if (
                    source["config"].get("auto_fix_pr") is not True
                    and not payload.get("manual")
                    and not row["attempts"]
                ):
                    self.db.execute("UPDATE alert_fixes SET state='waiting',updated=? WHERE id=?", (now, row["id"]))
                    continue
                used = self.db.execute(
                    "SELECT count(*) FROM alert_fix_runs WHERE source_id=? AND started>?",
                    (source["id"], now - 86400),
                ).fetchone()[0]
                limit = max(1, min(20, int(source["config"].get("fix_runs_per_day", self.limit))))
                if (not row["attempts"] and used >= limit) or row["attempts"] >= 3:
                    reason = "daily fix-run limit reached" if not row["attempts"] else "fix interrupted repeatedly"
                    self.db.execute(
                        "UPDATE alert_fixes SET state='failed',result=?,updated=? WHERE id=?",
                        (json.dumps({**json.loads(row["result"]), "reason": reason}), now, row["id"]),
                    )
                    continue
                if not row["attempts"]:
                    self.db.execute(
                        "INSERT INTO alert_fix_runs(job_id,cycle,source_id,started) VALUES(?,?,?,?)",
                        (row["id"], row["cycle"], source["id"], now),
                    )
                lease = uuid.uuid4().hex
                self.db.execute(
                    "UPDATE alert_fixes SET state='running',lease=?,lease_until=?,attempts=attempts+1,"
                    "started=COALESCE(started,?),updated=? WHERE id=?",
                    (lease, now + 2400, now, now, row["id"]),
                )
                return self.get(row["id"])
        return None

    def guard(self, job):
        current = self.get(job["id"])
        source = self.store.get(job["source_id"])
        if (
            not current
            or current["state"] != "running"
            or current["lease"] != job["lease"]
            or current["lease_until"] <= time.time()
            or not source
            or source["state"] != "active"
            or source["revision"] != job["revision"]
        ):
            raise AlertError("Fix registration or lease is no longer active")
        return source

    def save(self, job, *, state="running", **result):
        self.guard(job)
        value = {**job["result"], **result}
        now = time.time()
        self.db.execute(
            "UPDATE alert_fixes SET state=?,result=?,updated=? WHERE id=? AND lease=?",
            (state, json.dumps(value), now, job["id"], job["lease"]),
        )
        job.update(state=state, result=value, updated=now)

    def cancelled(self, job):
        # Revocation deliberately invalidates guard(). Finish only this exact
        # lease without permitting further work or discarding publication intent.
        self.db.execute(
            "UPDATE alert_fixes SET state='failed',result=?,updated=? WHERE id=? AND state='running' AND lease=?",
            (
                json.dumps({**job["result"], "reason": "repair cancelled; local evidence retained"}),
                time.time(),
                job["id"],
                job["lease"],
            ),
        )

    def cards(self, accounts):
        if accounts is not None and not accounts:
            return []
        account_filter = "" if accounts is None else " AND s.account IN (" + ",".join("?" for _ in accounts) + ")"
        rows = self.db.execute(
            "SELECT r.*,c.fix_id FROM alert_fix_cards c JOIN alert_fixes f ON f.id=c.fix_id "
            "JOIN receipts r ON r.id=c.receipt_id JOIN sources s ON s.id=r.source_id AND s.revision=r.revision "
            "WHERE r.state='complete' AND s.state='active' AND c.delivered<f.updated" + account_filter + " LIMIT 20",
            tuple(accounts or ()),
        ).fetchall()
        return [
            (self.store._receipt(row), self.get(row["fix_id"]))
            for row in rows
            if accounts is None or self.store.get(row["source_id"])["account"] in accounts
        ]

    def card_delivered(self, receipt, job):
        self.db.execute("UPDATE alert_fix_cards SET delivered=? WHERE receipt_id=?", (job["updated"], receipt["id"]))
