"""Channel existence reconciliation and durable, engine-only alert teardown."""

import asyncio
import json
import logging
import time

from src.core.alert_channels import AlertError

logger = logging.getLogger(__name__)


class AlertLifecycle:
    def __init__(self, store, adapters, transport, worker, locks, vault):
        self.store, self.adapters, self.transport = store, adapters, transport
        self.worker, self.locks, self.vault = worker, locks, vault
        self.accounts = set()
        self.wake = asyncio.Event()
        self.reconcile_lock = asyncio.Lock()
        self.cleanup_lock = asyncio.Lock()

    def request(self, source_id, kind):
        # Never wait for the provisioning lock before revoking local work.
        self.store.request_teardown(source_id, kind)
        self.worker.cancel_source(source_id)
        self.wake.set()

    def inaccessible(self, source, scope, reason):
        key = f"{scope}:{source['account']}:" + (source["guild_id"] if scope == "guild" else source["id"])
        subject = f"guild {source['guild_id']}" if scope == "guild" else f"channel {source['channel_id']}"
        self.store.notify_lifecycle(
            source,
            f"Alert access could not be verified for {subject} ({reason}). "
            "Registrations are retained; no destination was disabled. Check bot access.",
            condition=key,
        )

    async def reconcile(self, account, channel=None):
        async with self.reconcile_lock:
            rows = list(self.store.db.execute("SELECT id FROM sources WHERE account=?", (account,)))
            guilds = {}
            for row in rows:
                source = self.store.get(row["id"])
                if not source or not source["guild_id"] or source["state"] == "deleting":
                    continue
                # A channel create can finish before channel_id is copied to sources.
                if not source["channel_id"]:
                    operation = self.store.db.execute(
                        "SELECT result FROM operations WHERE source_id=? AND step='channel' AND state='complete' "
                        "ORDER BY revision DESC LIMIT 1",
                        (source["id"],),
                    ).fetchone()
                    if operation:
                        source = self.store.update(source["id"], channel_id=json.loads(operation["result"])["id"])
                    elif (
                        channel is not None
                        and str(channel.guild.id) == source["guild_id"]
                        and getattr(channel, "topic", None) == "kbots-alert:" + source["id"]
                    ):
                        # Event metadata only locates a candidate. It never proves deletion.
                        source = {**source, "channel_id": str(channel.id)}
                    else:
                        if self.store.db.execute(
                            "SELECT 1 FROM operations WHERE source_id=? AND step='channel' AND state='intent'",
                            (source["id"],),
                        ).fetchone():
                            self.store.notify_lifecycle(
                                source,
                                f"Alert setup {source['id']} has an uncertain channel creation. "
                                "The journal is retained; use setup status and inspect before resuming.",
                                condition=f"channel-intent:{source['id']}",
                            )
                        continue
                if channel is not None and str(channel.id) != source["channel_id"]:
                    continue
                guild_id = source["guild_id"]
                if guild_id not in guilds:
                    guilds[guild_id] = await self.transport.guild_status(source)
                if guilds[guild_id] != "present":
                    self.inaccessible(source, "guild", guilds[guild_id])
                    continue
                self.store.clear_condition(f"guild:{account}:{guild_id}")
                status = await self.transport.channel_status(source, channel)
                if status == "missing":
                    # Removal from the guild during the channel request is not mass deletion.
                    fresh_guild = await self.transport.guild_status(source)
                    if fresh_guild != "present":
                        guilds[guild_id] = fresh_guild
                        self.inaccessible(source, "guild", fresh_guild)
                        continue
                    current = self.store.get(source["id"])
                    if not current or current["channel_id"] not in {None, source["channel_id"]}:
                        continue
                    if not current["channel_id"]:
                        self.store.update(source["id"], channel_id=source["channel_id"])
                    self.request(source["id"], "deleted")
                elif status == "present":
                    self.store.clear_condition(f"channel:{account}:{source['id']}")
                else:
                    self.inaccessible(source, "channel", status)

    async def guild_lost(self, account, guild_id):
        # Gateway guild loss is never proof that the contained channels were deleted.
        row = self.store.db.execute(
            "SELECT id FROM sources WHERE account=? AND guild_id=? LIMIT 1", (account, str(guild_id))
        ).fetchone()
        if row:
            self.inaccessible(self.store.get(row["id"]), "guild", "guild unavailable")
        self.wake.set()

    async def _cleanup(self, source, job):
        if not await self.worker.drain_source(source["id"]):
            raise AlertError("Diagnosis cancellation is still pending")
        self.store.remember_revision(source)
        snapshots = {
            row["revision"]: row
            for row in self.store.db.execute("SELECT * FROM alert_revisions WHERE source_id=?", (source["id"],))
        }
        operation_revisions = {
            row["revision"]
            for row in self.store.db.execute(
                "SELECT revision FROM operations WHERE source_id=? AND step='destination'", (source["id"],)
            )
        }
        if operation_revisions - snapshots.keys():
            raise AlertError("Historical destination ownership context is missing; review required")
        while True:
            kind = job["kind"]
            completed = "removed" if kind == "deleted" else "cleaned"
            for revision, row in snapshots.items():
                if row[completed]:
                    continue
                owned = json.loads(row["snapshot"])
                if revision in operation_revisions or owned["config"].get("destination_id"):
                    adapter = self.adapters.get(owned["config"].get("service"))
                    if not adapter:
                        raise AlertError("Registered adapter is unavailable for cleanup")
                    destination_id = await adapter.reconcile_destination(owned)
                    if destination_id:
                        if kind == "deleted":
                            await adapter.remove(owned, destination_id)
                        else:
                            await adapter.disable(owned, destination_id)
                self.store.db.execute(
                    "UPDATE alert_revisions SET cleaned=1,removed=MAX(removed,?) "
                    "WHERE source_id=? AND revision=?",
                    (int(kind == "deleted"), source["id"], revision),
                )
            job = self.store.db.execute("SELECT * FROM teardowns WHERE source_id=?", (source["id"],)).fetchone()
            if job["kind"] == kind:
                break
            # Deletion can supersede unsubscribe during vendor I/O. Its terminal
            # removal must still run, even for revisions just marked disabled.
            snapshots = {
                row["revision"]: row
                for row in self.store.db.execute("SELECT * FROM alert_revisions WHERE source_id=?", (source["id"],))
            }
        if job["kind"] == "deleted":
            if self.store.db.execute(
                "SELECT 1 FROM alert_revisions WHERE source_id=? AND removed=0", (source["id"],)
            ).fetchone():
                raise AlertError("Channel deletion requires another ownership check")
            # Delete only this registration's per-revision webhook references.
            # The shared service credential belongs to the administrator.
            for revision in snapshots:
                self.vault.delete(f"secrets/alert-webhook-{source['id']}-r{revision}")
            self.store.purge_deleted(source["id"])
        else:
            with self.store.transaction():
                self.store.db.execute(
                    "UPDATE teardowns SET state='complete',error=NULL WHERE source_id=?", (source["id"],)
                )
                self.store.notify_lifecycle(
                    source,
                    f"Alert setup {source['id']} unsubscribed. Vendor cleanup confirmed; "
                    "the existing channel remains reserved.",
                )

    async def cleanup_due(self, now=None):
        now = time.time() if now is None else now
        async with self.cleanup_lock:
            jobs = list(self.store.db.execute("SELECT * FROM teardowns WHERE state='pending' AND available<=?", (now,)))
            for job in jobs:
                source = self.store.get(job["source_id"])
                if not source or source["account"] not in self.accounts:
                    continue
                lock = self.locks.setdefault(source["id"], asyncio.Lock())
                if lock.locked():
                    continue  # In-flight setup must journal its result before cleanup.
                async with lock:
                    try:
                        # A channel deletion can upgrade an unsubscribe while a prior job awaits I/O.
                        job = self.store.db.execute(
                            "SELECT * FROM teardowns WHERE source_id=?", (source["id"],)
                        ).fetchone()
                        await self._cleanup(self.store.get(source["id"]), job)
                    except Exception as error:
                        attempts = job["attempts"] + 1
                        state = "held" if attempts >= 8 else "pending"
                        # Type only: vendor exception messages can contain credentials.
                        with self.store.transaction():
                            self.store.db.execute(
                                "UPDATE teardowns SET state=?,attempts=?,available=?,error=? WHERE source_id=?",
                                (
                                    state,
                                    attempts,
                                    now + min(30 * 2 ** (attempts - 1), 3600),
                                    type(error).__name__,
                                    source["id"],
                                ),
                            )
                            self.store.notify_lifecycle(
                                source,
                                f"Alert setup {source['id']} has revoked intake, but vendor cleanup "
                                + (
                                    "needs operator review after eight failed attempts. "
                                    if state == "held"
                                    else "is incomplete and will retry automatically. "
                                )
                                + f"Use /alerts status {source['id']}; "
                                + f"/alerts resume {source['id']} retries cleanup only.",
                                condition=f"cleanup:{source['id']}:{state}",
                            )

    async def deliver_notices(self, now=None):
        now = time.time() if now is None else now
        for row in list(
            self.store.db.execute(
                "SELECT * FROM lifecycle_notices WHERE state IN ('pending','sending') AND available<=?", (now,)
            )
        ):
            if json.loads(row["context"])["account"] not in self.accounts:
                continue
            try:
                await self.transport.lifecycle_notice(dict(row))
            except Exception as error:
                self.store.db.execute("UPDATE lifecycle_notices SET available=? WHERE id=?", (now + 60, row["id"]))
                logger.warning("Alert lifecycle notice retained: %s", type(error).__name__)

    async def run(self):
        next_reconcile = 0
        while True:
            try:
                if time.monotonic() >= next_reconcile:
                    for account in tuple(self.accounts):
                        await self.reconcile(account)
                    next_reconcile = time.monotonic() + 60
                await self.cleanup_due()
                await self.deliver_notices()
            except Exception as error:
                logger.warning("Alert lifecycle retry retained: %s", type(error).__name__)
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=15)
            except TimeoutError:
                pass
