"""Machine-owner rehearsal bridge to the running alert service, never a model tool."""

import asyncio
import contextlib
import ctypes
import hashlib
import hmac
import json
import os
import secrets
import socket
import stat
import struct
import time
import uuid
from pathlib import Path

from src.core.alert_channels import AlertError
from src.core.alert_credentials import CredentialEntry
from src.core.alert_diagnosis import public_text
from src.core.alert_errors import failure_reason, log_failure
from src.core.base import resolve_vault_key_file


def proof_key(path):
    """Read only a private, owned, regular key file; never unlock a second vault."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("Expected an owned private vault key file")
        if not 1 <= info.st_size <= 4096:
            raise PermissionError("Vault key file is empty or exceeds the bound")
        value = stream.read(4097).strip()
    if not value or len(value) > 4096:
        raise PermissionError("Vault key file is empty or exceeds the bound")
    return hashlib.sha256(b"kbots-alert-operator-v1\0" + value).digest()


def peer_uid(sock):
    if hasattr(socket, "SO_PEERCRED"):
        return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    # macOS/BSD expose getpeereid in libc, but not in every Python socket build.
    fn = getattr(ctypes.CDLL(None, use_errno=True), "getpeereid", None)
    if fn is None:
        raise PermissionError("Peer credential verification is unavailable")
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    fn.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
    fn.restype = ctypes.c_int
    if fn(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise PermissionError("Peer credential verification failed")
    return uid.value


def signature(key, challenge, body):
    data = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hmac.new(key, challenge.encode() + b"\0" + data, hashlib.sha256).hexdigest()


def canonical_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise AlertError("Use a full canonical registration or session UUID")
    return value


class OperatorRehearsal(CredentialEntry):
    def __init__(self, alerts, directory, key_file=None):
        super().__init__(directory, alerts.connector.vault)
        self.path = Path(directory) / "operator.sock"
        self.key_file = Path(key_file) if key_file is not None else resolve_vault_key_file()
        self.alerts, self.store = alerts, alerts.store
        self.requests = set()
        self.session_locks = {}
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS operator_rehearsals (
                id TEXT PRIMARY KEY, source_id TEXT UNIQUE NOT NULL,
                parent_id TEXT NOT NULL, parent_revision INTEGER NOT NULL,
                account TEXT NOT NULL, owner TEXT NOT NULL, created REAL NOT NULL,
                expires REAL NOT NULL, state TEXT NOT NULL DEFAULT 'open', prompt TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS operator_answers (
                session_id TEXT NOT NULL, sequence INTEGER NOT NULL, input_hash TEXT NOT NULL,
                state TEXT NOT NULL, reply TEXT, PRIMARY KEY(session_id,sequence)
            );
        """)

    async def start(self):
        proof_key(self.key_file)  # Validate the server's boundary before binding.
        if not getattr(self.vault, "_fernet", None):
            raise AlertError("Operator rehearsal requires the running encrypted vault")
        await super().start()

    async def stop(self):
        if self.server:
            self.server.close()
        tasks = list(self.requests)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await super().stop()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.requests.add(task)
        result = {"ok": False, "error": "Operator authentication or request rejected"}
        authenticated = False
        try:
            if peer_uid(writer.get_extra_info("socket")) != os.getuid():
                raise PermissionError("Peer user mismatch")
            key = proof_key(self.key_file)
            challenge = secrets.token_hex(32)
            writer.write(json.dumps({"challenge": challenge}).encode() + b"\n")
            await writer.drain()
            request = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
            if (
                not isinstance(request, dict)
                or set(request) != {"body", "proof"}
                or not isinstance(request["body"], dict)
                or not isinstance(request["proof"], str)
                or not hmac.compare_digest(signature(key, challenge, request["body"]), request["proof"])
            ):
                raise PermissionError("Machine-owner proof failed")
            authenticated = True
            result = {"ok": True, **await self.dispatch(request["body"])}
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_failure(error, "local operator rehearsal")
            if authenticated:
                result["error"] = public_text(failure_reason(error))
        finally:
            try:
                if not task.cancelling():
                    writer.write(json.dumps(result).encode() + b"\n")
                    await writer.drain()
            except (ConnectionError, OSError):
                pass  # The durable step journal, not the socket write, owns outcome.
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError, OSError):
                    await writer.wait_closed()
                self.requests.discard(task)

    def session(self, session_id):
        row = self.store.db.execute("SELECT * FROM operator_rehearsals WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise AlertError("Operator rehearsal session was not found")
        return dict(row)

    def bound_parent(self, session):
        parent = self.store.get(session["parent_id"])
        if (
            session["state"] != "open"
            or session["expires"] <= time.time()
            or not parent
            or parent["state"] != "active"
            or parent["revision"] != session["parent_revision"]
            or parent["account"] != session["account"]
            or parent["owner"] != session["owner"]
        ):
            raise AlertError("The bound parent registration changed or this rehearsal expired; inspect status")
        return parent

    def allow_duplicate(self, source, other):
        row = self.store.db.execute("SELECT * FROM operator_rehearsals WHERE source_id=?", (source["id"],)).fetchone()
        if not row or source["dm_id"] != "operator:" + row["id"]:
            return False
        parent = self.bound_parent(dict(row))
        return (
            other["id"] == parent["id"]
            and source["account"] == parent["account"]
            and source["owner"] == parent["owner"]
            and source["guild_id"] == parent["guild_id"]
            and all(source["config"].get(k) == parent["config"].get(k) for k in ("service", "host", "project", "repo"))
        )

    def expire(self):
        # Existing lifecycle owns disabling and cleanup; never mutate the parent.
        for row in self.store.db.execute(
            "SELECT * FROM operator_rehearsals WHERE state='open' AND expires<=?", (time.time(),)
        ).fetchall():
            source = self.store.get(row["source_id"])
            if source and source["state"] not in {"disabled", "deleting"}:
                self.alerts.lifecycle.request(source["id"], "unsubscribe")
                self.store.notify_setup(
                    source,
                    "Operator rehearsal expired. Monitoring is stopping; its channel is retained for review.",
                    "operator-expired",
                )
            self.store.db.execute("UPDATE operator_rehearsals SET state='expired' WHERE id=?", (row["id"],))

    def status(self, session_id):
        session = self.session(session_id)
        source = self.store.get(session["source_id"])
        replies = self.store.db.execute(
            "SELECT sequence,state,reply FROM operator_answers WHERE session_id=? ORDER BY sequence", (session_id,)
        ).fetchall()
        notices = self.store.db.execute(
            "SELECT id,text,state FROM lifecycle_notices WHERE json_extract(context,'$.id')=? "
            "ORDER BY rowid DESC LIMIT 20",
            (session["source_id"],),
        ).fetchall()
        return {
            "session": session_id,
            "source_id": session["source_id"],
            "parent_id": session["parent_id"],
            "state": session["state"],
            "source_state": source["state"] if source else "removed",
            "channel_id": source["channel_id"] if source else None,
            "expires": session["expires"],
            "next_sequence": len(replies),
            "last_step_state": replies[-1]["state"] if replies else None,
            "reply": replies[-1]["reply"] if replies else session["prompt"],
            "notices": [dict(row) for row in reversed(notices)],
            "provenance": "local machine operator; no human Discord DM was received",
        }

    async def dispatch(self, body):
        fields = {
            "start": {"operation", "session", "parent", "account"},
            "answer": {"operation", "session", "sequence", "text"},
            "status": {"operation", "session"},
            "resume": {"operation", "session"},
            "stop": {"operation", "session"},
        }
        operation = body.get("operation")
        if operation not in fields or set(body) != fields[operation]:
            raise AlertError("Invalid operator rehearsal request")
        session_id = canonical_id(body["session"])
        async with self.session_locks.setdefault(session_id, asyncio.Lock()):
            self.expire()
            if operation == "start":
                return self.begin(session_id, canonical_id(body["parent"]), body["account"])
            session = self.session(session_id)
            if operation == "status":
                return self.status(session_id)
            async with self.alerts.locks.setdefault(session["source_id"], asyncio.Lock()):
                source = self.store.get(session["source_id"])
                if not source:
                    raise AlertError("Rehearsal registration has been removed")
                if operation == "stop":
                    self.alerts.lifecycle.request(source["id"], "unsubscribe")
                    self.store.db.execute("UPDATE operator_rehearsals SET state='stopped' WHERE id=?", (session_id,))
                    return self.status(session_id)
                self.bound_parent(session)
                bot = self.alerts.connector.bots[session["account"]]
                if operation == "resume":
                    if source["state"] in {"provisioning", "provisional"}:
                        self.validate_identity(session, source)
                        reply = await self.alerts.provision(source)
                    elif source["state"] == "draft":
                        source = self.label(source)
                        reply = "Interrupted step reconciled to the current prompt. Inspect before continuing.\n"
                        reply += self.alerts.question(source, bot)
                    else:
                        reply = self.alerts.status(source)
                    self.store.db.execute(
                        "UPDATE operator_answers SET state='complete',reply=? "
                        "WHERE session_id=? AND state IN ('uncertain','pending')",
                        (public_text(reply), session_id),
                    )
                    return self.status(session_id)
                return await self.answer(session, source, bot, body["sequence"], body["text"])

    def begin(self, session_id, parent_id, account):
        if not isinstance(account, str) or not 1 <= len(account) <= 100:
            raise AlertError("Choose a configured bot account")
        old = self.store.db.execute("SELECT * FROM operator_rehearsals WHERE id=?", (session_id,)).fetchone()
        if old:
            if old["parent_id"] != parent_id or old["account"] != account:
                raise AlertError("This session is already bound to a different parent or account")
            return self.status(session_id)
        parent = self.store.get(parent_id)
        if not parent or parent["state"] != "active" or parent["account"] != account:
            raise AlertError("Choose an active parent registration on that bot account")
        if parent["dm_id"].startswith("operator:"):
            raise AlertError("A rehearsal cannot be the parent of another rehearsal")
        bot = self.alerts.connector.bots.get(account)
        if not bot or not bot.client.user or account not in self.alerts.worker.accounts:
            raise AlertError("The chosen bot account has not completed startup reconciliation")
        if self.store.db.execute(
            "SELECT 1 FROM operator_rehearsals o JOIN sources s ON s.id=o.source_id WHERE o.parent_id=? "
            "AND (s.state!='disabled' OR s.channel_id IS NOT NULL "
            "OR EXISTS (SELECT 1 FROM operations WHERE source_id=s.id))",
            (parent_id,),
        ).fetchone():
            raise AlertError("A rehearsal for this parent is still retained; finish its channel teardown first")
        source_id, now = str(uuid.uuid4()), time.time()
        with self.store.transaction():
            self.store.db.execute(
                "INSERT INTO sources(id,owner,user_id,account,dm_id,nonce,config,created,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    source_id,
                    parent["owner"],
                    str(bot.client.user.id),
                    account,
                    "operator:" + session_id,
                    uuid.uuid4().hex,
                    json.dumps({"service": parent["config"]["service"], "message_format": 2}),
                    now,
                    now,
                ),
            )
            self.store.db.execute(
                "INSERT INTO operator_rehearsals(id,source_id,parent_id,parent_revision,account,owner,created,expires) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (session_id, source_id, parent_id, parent["revision"], account, parent["owner"], now, now + 3600),
            )
        prompt = self.alerts.question(self.store.get(source_id), bot)
        self.store.db.execute("UPDATE operator_rehearsals SET prompt=? WHERE id=?", (public_text(prompt), session_id))
        return self.status(session_id)

    def label(self, source):
        app = source["config"].get("app")
        if app and not app.endswith("-rehearsal"):
            return self.store.update(
                source["id"], config={**source["config"], "app": app[:31].rstrip("-") + "-rehearsal"}
            )
        return source

    def check_creation(self, source):
        """Revalidate after credential I/O, before a cancelled draft can revive."""
        row = self.store.db.execute("SELECT * FROM operator_rehearsals WHERE source_id=?", (source["id"],)).fetchone()
        if not row:
            return
        current = self.store.get(source["id"])
        if (
            not current
            or current["revision"] != source["revision"]
            or current["state"] != "draft"
            or current["config"] != source["config"]
        ):
            raise AlertError("Operator rehearsal was revoked or changed; inspect status before continuing")
        self.validate_identity(dict(row), current)

    def validate_identity(self, session, source):
        parent = self.bound_parent(session)
        if not self.allow_duplicate(source, parent):
            raise AlertError("Rehearsal must use its bound parent's repository, project, service and server")
        if not source["config"].get("app", "").endswith("-rehearsal"):
            raise AlertError("Rehearsal channel name must be clearly labelled")

    async def answer(self, session, source, bot, sequence, text):
        session_id = session["id"]
        if type(sequence) is not int or not 0 <= sequence < 32 or not isinstance(text, str) or len(text) > 2000:
            raise AlertError("Use at most 32 bounded setup answers")
        digest = hashlib.sha256(text.encode()).hexdigest()
        old = self.store.db.execute(
            "SELECT * FROM operator_answers WHERE session_id=? AND sequence=?", (session_id, sequence)
        ).fetchone()
        if old:
            if old["input_hash"] != digest:
                raise AlertError("This step already has different input; inspect the existing journal")
            if old["state"] != "complete":
                raise AlertError("Previous step outcome is uncertain; inspect status and explicitly resume")
            return {**self.status(session_id), "reply": old["reply"], "replayed": True}
        count = self.store.db.execute(
            "SELECT count(*) FROM operator_answers WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        if sequence != count or source["state"] != "draft":
            raise AlertError("Use the next step of the draft; inspect status before continuing")
        if self.store.db.execute(
            "SELECT 1 FROM operator_answers WHERE session_id=? AND state!='complete'", (session_id,)
        ).fetchone():
            raise AlertError("An earlier step is uncertain; explicitly resume first")
        if "triggers" in source["config"] and text.strip().upper() == "CREATE":
            self.validate_identity(session, source)
        self.store.db.execute(
            "INSERT INTO operator_answers VALUES(?,?,?,'pending',NULL)",
            (session_id, sequence, digest),
        )
        self.store.update(source["id"], waiting=0)
        try:
            reply = await self.alerts.answer(source, bot, text)
            updated = self.store.get(source["id"])
            if "app" not in source["config"] and updated and "app" in updated["config"]:
                self.label(updated)
        except asyncio.CancelledError:
            self.store.db.execute(
                "UPDATE operator_answers SET state='uncertain' WHERE session_id=? AND sequence=?",
                (session_id, sequence),
            )
            raise
        except Exception as error:
            log_failure(error, "operator setup answer")
            reply = public_text(failure_reason(error))
            current = self.store.get(source["id"])
            if current and current["state"] in {"provisioning", "provisional"}:
                self.store.notify_setup(current, "Alert setup is held: " + reply, "provision-held")
                self.alerts.lifecycle.wake.set()
        finally:
            current = self.store.get(source["id"])
            if current and current["state"] == "draft":
                self.store.update(source["id"], waiting=1)
        self.store.db.execute(
            "UPDATE operator_answers SET state='complete',reply=? WHERE session_id=? AND sequence=?",
            (public_text(reply), session_id, sequence),
        )
        return self.status(session_id)
