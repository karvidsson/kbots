"""Post-diagnosis fix execution, durable publication intent and card reconciliation."""

import asyncio
import json
import tempfile
from pathlib import Path

from src.core.alert_channels import AlertError
from src.core.alert_diagnosis import public_text
from src.core.alert_errors import log_failure
from src.core.alert_fix_files import write_bytes
from src.core.alert_fix_publish import FixPublisher
from src.core.alert_fix_store import FixJobs
from src.core.alert_fixer import FixWorkspace, repair
from src.core.alert_git import AlertRepository, git, registered_remote


class AlertFixWorker:
    def __init__(self, store, manager, transport, directory, limit=3):
        self.store, self.manager, self.transport = store, manager, transport
        self.directory = Path(directory) / "fixes"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs = FixJobs(store, limit)
        self.repositories = AlertRepository(directory)
        self.publisher = FixPublisher()
        self.accounts = set()
        self.running = {}

    def cancel_source(self, source_id):
        if task := self.running.get(source_id):
            task.cancel()

    def existing_pr(self, job, pr):
        result = job["result"]
        owned = (
            bool(result.get("commit"))
            and pr.get("head") == result["commit"]
            and pr.get("branch") == result.get("branch")
            and pr.get("state") == "open"
            and not pr.get("draft")
            and not pr.get("merged")
        )
        self.jobs.save(job, state="complete", pr=pr, existing=not owned)

    async def notices(self):
        for receipt, job in self.jobs.cards(self.accounts):
            try:
                source = self.store.get(receipt["source_id"])
                await self.transport.fix_status(source, receipt, job)
                self.jobs.card_delivered(receipt, job)
            except Exception as error:
                log_failure(error, "fix PR card update")

    async def execute(self, job):
        source = self.jobs.guard(job)
        config = source["config"]
        issue_url = f"{config['host']}/project/{config['project']}/error_tracking/{job['issue_id']}"
        try:
            tree = job["result"].get("tree")
            if not tree:
                tree = await asyncio.to_thread(self.repositories.fetch, source)
                self.jobs.save(job, tree=tree)
            if registered_remote(config["repo"])[1] != tree["identity"]:
                raise AlertError("Registered origin changed before repair")
            existing = await asyncio.to_thread(self.publisher.find, tree, job["issue_id"], issue_url)
            self.jobs.guard(job)
            if existing:
                self.existing_pr(job, existing)
                return
            if job["result"].get("pr_intent"):
                raise AlertError("PR creation acknowledgement is uncertain; no duplicate create was attempted")
            if not job["result"].get("commit"):
                branch = f"alert-fix/{job['issue_id'][:8]}-{job['id'][:12]}-{job['cycle']:x}"
                attempt = self.directory / job["id"] / f"{job['cycle']}-{job['attempts']}"
                folder = await asyncio.to_thread(self.repositories.checkout, tree, attempt / "code", branch)
                baseline = await asyncio.to_thread(self.repositories.checkout, tree, attempt / "before", branch)
                workspace = await asyncio.to_thread(FixWorkspace, folder, baseline, config["repo"], tree)
                self.jobs.save(job, branch=branch, workspace=str(folder))
                await self.notices()
                # Parse the bounded diagnosis into labeled data; do not send the
                # raw vendor envelope, issue object, IDs, credentials or sessions.
                from src.connectors.alert_embeds import diagnosis_sections

                context = {
                    "diagnosis": diagnosis_sections(job["payload"]["diagnosis"]),
                    "evidence": job["payload"]["context"]["evidence"],
                    "fix_base_revision": tree["default_revision"],
                }
                with tempfile.TemporaryDirectory(prefix="model-", dir=self.directory) as scratch:
                    report = await repair(
                        self.manager, source, workspace, context, lambda: self.jobs.guard(job), Path(scratch)
                    )
                self.jobs.guard(job)
                commit = await asyncio.to_thread(workspace.commit, report["cause"])
                proof = workspace.verified
                # Preserve full bounded local gate evidence before any outbound write.
                proof_path = attempt / "validation.json"
                write_bytes(attempt, "validation.json", json.dumps(proof, indent=2).encode())
                self.jobs.save(job, commit=commit, report=report, proof=proof, proof_path=str(proof_path))
            current = self.jobs.guard(job)
            if registered_remote(current["config"]["repo"])[1] != tree["identity"]:
                raise AlertError("Registered origin changed before publication")
            result = job["result"]
            # Resume only the exact immutable commit, never a mutable HEAD.
            folder = Path(result["workspace"])
            if git(folder, "rev-parse", result["commit"] + "^{commit}").decode().strip() != result["commit"]:
                raise AlertError("Tested repair commit is unavailable")
            import hashlib

            proof = result["proof"]
            if (
                proof["before"]["exit_code"] == 0
                or not proof["checks"]
                or any(c["exit_code"] != 0 for c in proof["checks"])
            ):
                raise AlertError("Persisted repair validation is incomplete")
            for name, digest in proof["files"].items():
                if hashlib.sha256(git(folder, "show", result["commit"] + ":" + name)).hexdigest() != digest:
                    raise AlertError("Persisted test proof does not match the repair commit")
            self.jobs.save(job, push_intent=True)
            await asyncio.to_thread(self.publisher.push, folder, tree, result["branch"], result["commit"])
            self.jobs.guard(job)
            self.jobs.save(job, pushed=True)
            existing = await asyncio.to_thread(self.publisher.find, tree, job["issue_id"], issue_url)
            self.jobs.guard(job)
            if existing:
                self.existing_pr(job, existing)
                return
            self.jobs.save(job, pr_intent=True)
            pr = await asyncio.to_thread(
                self.publisher.create,
                tree,
                result["branch"],
                job["issue_id"],
                issue_url,
                result["report"],
                result["proof"],
            )
            if pr["head"] != result["commit"] or pr["branch"] != result["branch"]:
                raise AlertError("PR head differs from the tested repair")
            self.jobs.save(job, state="complete", pr=pr)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_failure(error, "fix PR")
            if job["result"].get("pr_intent"):
                # Read back the owned issue PR after ambiguous publication. Never
                # retry POST blindly, including after a process restart.
                try:
                    pr = await asyncio.to_thread(self.publisher.find, tree, job["issue_id"], issue_url)
                    if pr:
                        self.existing_pr(job, pr)
                        return
                except Exception:
                    pass
            reason = str(error) if isinstance(error, AlertError) else "repair failed; retained locally for review"
            if job["result"].get("workspace"):
                reason += "; local work retained for review"
            self.jobs.save(job, state="failed", reason=public_text(reason, 250))

    async def once(self):
        if not self.accounts:
            return False
        # Recover the small gap between committed diagnosis delivery and enqueue.
        for row in self.store.db.execute(
            "SELECT r.* FROM receipts r JOIN sources s ON s.id=r.source_id AND s.revision=r.revision "
            "WHERE r.state='complete' AND s.state='active' "
            "AND NOT EXISTS (SELECT 1 FROM alert_fix_cards c WHERE c.receipt_id=r.id) "
            "AND s.account IN (" + ",".join("?" for _ in self.accounts) + ") LIMIT 20",
            tuple(self.accounts),
        ).fetchall():
            source = self.store.get(row["source_id"])
            if source["account"] in self.accounts:
                with self.store.transaction():
                    self.jobs.enqueue(source, self.store._receipt(row))
        await self.notices()
        job = self.jobs.claim(self.accounts)
        if not job:
            return False
        task = asyncio.create_task(asyncio.wait_for(self.execute(job), timeout=2100))
        self.running[job["source_id"]] = task
        self.manager.active_turns += 1
        try:
            await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise  # Service shutdown retains the lease for restart recovery.
            self.jobs.cancelled(job)  # A revoked job must not kill the worker.
        except TimeoutError:
            self.jobs.save(job, state="failed", reason="fix-run time budget exceeded; local work retained")
        finally:
            self.running.pop(job["source_id"], None)
            self.manager.active_turns -= 1
        await self.notices()
        return True

    async def run(self):
        while True:
            try:
                if await self.once():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log_failure(error, "fix worker")
            await asyncio.sleep(5)
