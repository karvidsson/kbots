"""The operator bridge shares the engine, never a human identity or a second vault."""

import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from extras.posthog.alerts import alert_heading, issue_link
from src.core.alert_channels import AlertError
from src.core.alert_operator import OperatorRehearsal, proof_key, signature
from src.core.base import LLMResponse
from tests.test_alert_exception_evidence import response
from tests.test_alert_lifecycle import Harness, http_error
from tests.test_alert_setup_ux import MemoryChannel, visible_text


def cli_module():
    spec = importlib.util.spec_from_file_location(
        "alert_rehearsal_cli", Path(__file__).parents[1] / "scripts/alert-setup-rehearsal.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def operator():
    # Short, owned paths for AF_UNIX on macOS. No install key/socket is used.
    with tempfile.TemporaryDirectory(prefix="ao-", dir="/tmp") as rawroot:
        root = Path(rawroot)
        key = root / "key"
        key.write_text("synthetic-operator-passphrase")
        key.chmod(0o600)
        h = Harness(root / "state")
        h.alerts.worker.accounts.add("one")
        h.bot.client.guilds = [h.guild]
        h.guild.name, h.guild.default_role = "Example server", "everyone"
        h.guild.fetch_member = AsyncMock(side_effect=lambda ident: ident)
        h.bot.client.get_guild = lambda ident: h.guild if ident == 301 else None
        h.vault.list_keys = lambda: list(h.secrets)
        for suffix in ("api-key", "read-key", "test-key"):
            h.secrets["secrets/posthog-" + suffix] = "synthetic-read-key-no-live-access"
        repo = root / "repo"
        (repo / "server/api/debug").mkdir(parents=True)
        (repo / "server/api/debug/boom.get.ts").write_text("// Deliberate drill\nthrow new Error('drill');\n")
        for args in (
            ["init", "-q"],
            ["remote", "add", "origin", "https://code.example/team/sample.git"],
            ["add", "."],
            ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-qm", "fixture"],
        ):
            subprocess.run(["git", *args], cwd=repo, check=True)
        h.alerts.config["repository_roots"] = [str(repo.parent)]
        parent = h.source()
        parent = h.store.update(parent["id"], config={**parent["config"], "repo": str(repo.resolve())})
        room = MemoryChannel(402, guild=h.guild)
        room.topic = None
        hooks = []
        room.webhooks = AsyncMock(return_value=hooks)

        async def create_hook(**kwargs):
            hook = SimpleNamespace(
                id=502,
                name=kwargs["name"],
                user=h.user,
                token="synthetic",
                url="https://discord.com/api/webhooks/502/synthetic-token",
            )
            hooks.append(hook)
            return hook

        room.create_webhook = create_hook
        h.guild.fetch_channels = AsyncMock(return_value=[])

        async def create_channel(name, **kwargs):
            assert name.endswith("-rehearsal")
            assert kwargs["overwrites"]["everyone"].view_channel is False
            room.topic = kwargs["topic"]
            return room

        h.guild.create_text_channel = AsyncMock(side_effect=create_channel)
        context = SimpleNamespace(h=h, root=root, key=key, parent=parent, room=room, removed=False, posts=0)

        async def channel(ident):
            if ident == 402:
                if context.removed:
                    raise http_error(404, 10003)
                return room
            assert ident == 401
            return h.room

        h.bot.client.fetch_channel = AsyncMock(side_effect=channel)
        base_request = h.request
        issue_id = str(uuid.uuid4())

        async def request(config, method, resource, **kwargs):
            if resource == "error_tracking/issues/?limit=1":
                return {"results": [{"id": issue_id}]}
            if resource == f"error_tracking/issues/{issue_id}/":
                return {"id": issue_id, "name": "Error"}
            if resource == "error_tracking/query/issue_events/":
                assert h.adapter._sample_request_allowed(resource, kwargs["payload"])
                return response()
            if method == "POST" and resource == "hog_functions/":
                context.posts += 1
                payload = copy.deepcopy(kwargs["payload"])
                payload["template"] = {"id": payload.pop("template_id")}
                payload["id"], payload["hog"] = str(uuid.uuid4()), "print(inputs.content)"
                payload["template"].update(code=payload["hog"], inputs_schema=[])
                h.remote[payload["id"]] = payload
                return copy.deepcopy(payload)
            if resource.endswith("/invocations/"):
                event = kwargs["payload"]["globals"]["event"]
                source = h.store.channel("402")
                envelope = (
                    f"KBOTS_ALERT_V2 {source['id']} {source['nonce']} {event['event']} "
                    f"{event['uuid']} {event['distinct_id']} drill"
                )
                text = (
                    alert_heading(source) + "\nSetup check\n" + issue_link(source, issue_id) + "\n||" + envelope + "||"
                )
                await h.bot.on_message(
                    SimpleNamespace(
                        id=600,
                        channel=room,
                        guild=h.guild,
                        webhook_id=502,
                        author=SimpleNamespace(id=502, bot=True),
                        content=text,
                    )
                )
                return {"status": "success"}
            return await base_request(config, method, resource, **kwargs)

        h.adapter._request = request
        provider = SimpleNamespace(
            supports_tool_free=True,
            complete=AsyncMock(
                return_value=LLMResponse(content="Verified reporting path in server/api/debug/boom.get.ts.")
            ),
        )
        manager = SimpleNamespace(
            agent_configs={"worker": {"llm": {"model": "fixture"}}},
            defaults={},
            storage=None,
            _apply_provider_override=lambda *args: None,
            _get_agent_llm=lambda *args: provider,
            _effective_model=lambda *args: "fixture",
            active_turns=0,
        )
        h.alerts.worker.manager = manager
        context.provider = provider
        context.bridge = OperatorRehearsal(h.alerts, root / "state", key)
        h.alerts.operator = context.bridge
        h.lifecycle.expire_rehearsals = context.bridge.expire
        await context.bridge.start()
        context.cli = cli_module()
        yield context
        await context.bridge.stop()
        h.close()


async def call(o, operation, session, **kwargs):
    return await asyncio.to_thread(
        o.cli.exchange, o.bridge.path, proof_key(o.key), {"operation": operation, "session": session, **kwargs}
    )


async def start(o):
    session = str(uuid.uuid4())
    result = await call(o, "start", session, parent=o.parent["id"], account="one")
    return session, result


def answers():
    return [
        "Sample App",
        "the repo is https://code.example/team/sample.git",
        "https://eu.posthog.com/project/123/home",
        "use existing one",
        "1",
        "yes",
        "CREATE",
    ]


async def complete(o):
    session, result = await start(o)
    for i, text in enumerate(answers()):
        result = await call(o, "answer", session, sequence=i, text=text)
    return session, result


async def test_cli_drives_real_setup_worker_and_teardown_without_touching_parent(operator, capsys):
    o = operator
    parent_before = copy.deepcopy(o.h.store.get(o.parent["id"]))
    remote_before = copy.deepcopy(o.h.remote)
    inputs = o.root / "inputs.json"
    inputs.write_text(json.dumps(answers()))
    args = SimpleNamespace(
        socket=o.bridge.path,
        key_file=o.key,
        journal=o.root / "journal.json",
        parent=o.parent["id"],
        account="one",
        inputs=inputs,
        action="run",
    )
    await asyncio.to_thread(o.cli.run, args)
    journal = json.loads(args.journal.read_text())
    result = journal["last_status"]
    assert result["source_state"] == "provisional" and o.posts == 1
    child = o.h.store.get(result["source_id"])
    assert child["user_id"] == str(o.h.bot.client.user.id) != o.parent["user_id"]
    assert child["dm_id"] == "operator:" + journal["session"]
    assert child["config"]["app"] == "sample-app-rehearsal"
    assert o.h.store.counts(child["id"]) == {"pending": 1}
    assert o.bridge.store is o.h.store and o.bridge.vault is o.h.vault
    assert o.h.store.db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert o.h.store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert not o.h.store.db.in_transaction
    # An acknowledged/replayed scripted CREATE is not another provision.
    await asyncio.to_thread(o.cli.run, args)
    assert o.posts == 1 and o.h.guild.create_text_channel.await_count == 1
    await o.h.alerts.worker.once()
    await o.h.alerts.worker.once()
    await o.h.lifecycle.deliver_notices()
    status = await call(o, "status", journal["session"])
    assert status["source_state"] == "active"
    assert any("active" in n["text"] and n["state"] == "complete" for n in status["notices"])
    assert len(o.room.messages) == 1 and "boom.get.ts" in visible_text(o.room.messages[0])
    assert o.h.home_messages == []  # No parent, home-channel or human-DM transcript was borrowed.
    o.provider.complete.assert_awaited_once()
    captured = capsys.readouterr().out
    assert "OPERATOR: Sample App" in captured and "BOT:" in captured and "CREATE" in captured
    assert "synthetic-operator-passphrase" not in captured + args.journal.read_text()
    o.removed = True
    await o.h.lifecycle.reconcile("one")
    await o.h.lifecycle.cleanup_due()
    await o.h.lifecycle.deliver_notices()
    assert o.h.store.get(child["id"]) is None
    assert o.h.store.get(o.parent["id"]) == parent_before and o.h.remote == remote_before
    assert (await call(o, "status", journal["session"]))["source_state"] == "removed"
    assert o.h.store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


async def test_same_session_replays_answer_but_rejects_changed_step(operator):
    o = operator
    session, _ = await start(o)
    first = await call(o, "answer", session, sequence=0, text="Sample App")
    again = await call(o, "answer", session, sequence=0, text="Sample App")
    assert first["reply"] == again["reply"] and again["replayed"] is True
    with pytest.raises(ValueError, match="different input"):
        await call(o, "answer", session, sequence=0, text="Other App")
    with pytest.raises(ValueError, match="next step"):
        await call(o, "answer", session, sequence=3, text="yes")


async def test_rehearsal_does_not_reuse_or_consume_human_draft(operator):
    o = operator
    draft = o.h.store.begin("worker", "101", "one", "201")
    o.h.store.update(draft["id"], waiting=0)
    session, result = await start(o)
    assert result["source_id"] != draft["id"]
    assert o.h.store.get(draft["id"])["waiting"] == 0
    assert not o.h.store.get(result["source_id"])["user_id"] == "101"
    # No Discord message with a fabricated owner identity is constructed.
    assert o.h.bot.client.fetch_channel.await_count == 0


async def test_normal_dm_duplicate_guard_is_unchanged(operator):
    o = operator
    ordinary = o.h.store.begin("worker", "101", "one", "201")
    ordinary = o.h.store.update(ordinary["id"], guild_id="301", config={**o.parent["config"], "app": "other-name"})
    with pytest.raises(AlertError, match="already has an alert"):
        await o.h.alerts.answer(ordinary, o.h.bot, "CREATE")
    assert o.posts == 0


async def test_rehearsal_scope_is_one_parent_one_child_and_exact_identity(operator):
    o = operator
    session, result = await start(o)
    with pytest.raises(ValueError, match="still retained"):
        await start(o)
    child = o.h.store.update(
        result["source_id"], guild_id="301", config={**o.parent["config"], "app": "sample-rehearsal", "project": "999"}
    )
    with pytest.raises(ValueError, match="bound parent"):
        await call(o, "answer", session, sequence=0, text="CREATE")
    assert o.posts == 0
    assert not o.bridge.allow_duplicate(child, o.parent)
    o.h.store.update(o.parent["id"], state="disabled")
    with pytest.raises(ValueError, match="parent registration changed"):
        await call(o, "answer", session, sequence=0, text="CREATE")


async def test_expiry_revokes_only_child_and_retains_journal(operator):
    o = operator
    session, result = await complete(o)
    before = copy.deepcopy(o.h.store.get(o.parent["id"]))
    o.h.store.db.execute("UPDATE operator_rehearsals SET expires=? WHERE id=?", (time.time() - 1, session))
    o.bridge.expire()
    assert o.h.store.get(result["source_id"])["state"] == "disabled"
    await o.h.lifecycle.cleanup_due()
    assert o.h.remote[o.h.store.get(result["source_id"])["config"]["destination_id"]]["enabled"] is False
    assert o.h.store.get(o.parent["id"]) == before
    status = await call(o, "status", session)
    assert status["state"] == "expired" and status["channel_id"] == "402"
    assert any("expired" in n["text"] for n in status["notices"])
    with pytest.raises(ValueError, match="expired"):
        await call(o, "answer", session, sequence=7, text="CREATE")


async def test_socket_reconnect_preserves_completed_steps_and_refuses_uncertain_replay(operator):
    o = operator
    session, result = await start(o)
    await call(o, "answer", session, sequence=0, text="Sample App")
    digest = hashlib.sha256(b"the repo").hexdigest()
    o.h.store.db.execute("INSERT INTO operator_answers VALUES(?,1,?,'pending',NULL)", (session, digest))
    await o.bridge.stop()
    o.bridge = OperatorRehearsal(o.h.alerts, o.root / "state", o.key)
    o.h.alerts.operator = o.bridge
    await o.bridge.start()
    with pytest.raises(ValueError, match="uncertain"):
        await call(o, "answer", session, sequence=1, text="the repo")
    with pytest.raises(ValueError, match="earlier step"):
        await call(o, "answer", session, sequence=2, text="yes")
    resumed = await call(o, "resume", session)
    assert "Inspect before continuing" in resumed["reply"]
    assert resumed["last_step_state"] == "complete" and o.posts == 0


async def test_lost_response_after_create_resumes_actual_provision_without_duplicate(operator):
    o = operator
    session, result = await complete(o)
    o.h.store.db.execute(
        "UPDATE operator_answers SET state='uncertain',reply=NULL WHERE session_id=? AND sequence=6", (session,)
    )
    resumed = await call(o, "resume", session)
    assert resumed["source_state"] == "provisional" and resumed["last_step_state"] == "complete"
    assert o.posts == 1 and o.h.guild.create_text_channel.await_count == 1


async def test_another_account_or_rehearsal_parent_is_rejected(operator):
    o = operator
    with pytest.raises(ValueError, match="active parent"):
        await call(o, "start", str(uuid.uuid4()), parent=o.parent["id"], account="other")
    session, result = await complete(o)
    o.h.store.update(result["source_id"], state="active")
    with pytest.raises(ValueError, match="cannot be the parent"):
        await call(o, "start", str(uuid.uuid4()), parent=result["source_id"], account="one")


async def test_bad_proof_and_replayed_challenge_do_not_reach_setup(operator):
    o = operator
    body = {"operation": "start", "session": str(uuid.uuid4()), "parent": o.parent["id"], "account": "one"}
    reader, writer = await asyncio.open_unix_connection(str(o.bridge.path))
    old = json.loads(await reader.readline())["challenge"]
    writer.write(json.dumps({"body": body, "proof": "0" * 64}).encode() + b"\n")
    await writer.drain()
    assert json.loads(await reader.readline())["ok"] is False
    writer.close()
    await writer.wait_closed()
    reader, writer = await asyncio.open_unix_connection(str(o.bridge.path))
    new = json.loads(await reader.readline())["challenge"]
    assert new != old
    writer.write(json.dumps({"body": body, "proof": signature(proof_key(o.key), old, body)}).encode() + b"\n")
    await writer.drain()
    assert json.loads(await reader.readline())["ok"] is False
    writer.close()
    await writer.wait_closed()
    assert o.h.store.db.execute("SELECT count(*) FROM operator_rehearsals").fetchone()[0] == 0


async def test_wrong_peer_uid_is_rejected_before_challenge(operator, monkeypatch):
    from src.core import alert_operator

    o = operator
    monkeypatch.setattr(alert_operator, "peer_uid", lambda _: os.getuid() + 1)
    reader, writer = await asyncio.open_unix_connection(str(o.bridge.path))
    assert json.loads(await reader.readline())["ok"] is False
    writer.close()
    await writer.wait_closed()
    assert o.h.store.db.execute("SELECT count(*) FROM operator_rehearsals").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["missing", "public", "symlink", "empty"])
def test_key_gate_refuses_before_client_or_journal_changes(tmp_path, kind):
    cli = cli_module()
    key = tmp_path / "key"
    if kind != "missing":
        key.write_text("synthetic-passphrase" if kind != "empty" else "")
        key.chmod(0o644 if kind == "public" else 0o600)
        if kind == "symlink":
            link = tmp_path / "link"
            link.symlink_to(key)
            key = link
    args = SimpleNamespace(key_file=key, journal=tmp_path / "journal.json")
    with pytest.raises((OSError, PermissionError)):
        cli.run(args)
    assert not args.journal.exists()


async def test_second_socket_server_preserves_live_listener(operator):
    o = operator
    before = o.bridge.path.stat().st_ino
    other = OperatorRehearsal(o.h.alerts, o.root / "state", o.key)
    with pytest.raises(RuntimeError, match="already running"):
        await other.start()
    assert o.bridge.path.stat().st_ino == before


async def test_blank_cancelled_rehearsal_can_be_replaced_without_channel(operator):
    o = operator
    session, result = await start(o)
    await call(o, "answer", session, sequence=0, text="CANCEL")
    other, replacement = await start(o)
    assert other != session and replacement["source_state"] == "draft"


async def ready_to_create(o):
    session, result = await start(o)
    for i, text in enumerate(answers()[:-1]):
        result = await call(o, "answer", session, sequence=i, text=text)
    return session, result


async def test_concurrent_create_shares_source_lock_and_has_no_open_sqlite_transaction(operator):
    o = operator
    session, result = await ready_to_create(o)
    entered, release = asyncio.Event(), asyncio.Event()
    original = o.h.adapter.check_credentials

    async def check(config):
        assert o.h.alerts.locks[result["source_id"]].locked()
        assert not o.h.store.db.in_transaction
        entered.set()
        await release.wait()
        return await original(config)

    o.h.adapter.check_credentials = AsyncMock(side_effect=check)
    first = asyncio.create_task(call(o, "answer", session, sequence=6, text="CREATE"))
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(call(o, "answer", session, sequence=6, text="CREATE"))
    # Ordinary work can write through the same connection while the API read waits.
    ordinary = o.h.store.begin("worker", "101", "one", "201")
    assert o.h.store.get(ordinary["id"])["state"] == "draft"
    release.set()
    a, b = await asyncio.wait_for(asyncio.gather(first, second), 3)
    assert a["source_id"] == b["source_id"] and b["replayed"] is True
    assert o.h.adapter.check_credentials.await_count == 1
    assert o.posts == 1 and o.h.guild.create_text_channel.await_count == 1
    assert o.h.store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("change", ["expiry", "parent_revision"])
async def test_inflight_credential_check_cannot_revive_expired_or_changed_rehearsal(operator, change):
    o = operator
    session, result = await ready_to_create(o)
    entered, release = asyncio.Event(), asyncio.Event()

    async def check(config):
        entered.set()
        await release.wait()

    o.h.adapter.check_credentials = check
    pending = asyncio.create_task(call(o, "answer", session, sequence=6, text="CREATE"))
    await asyncio.wait_for(entered.wait(), 2)
    if change == "expiry":
        o.h.store.db.execute("UPDATE operator_rehearsals SET expires=0 WHERE id=?", (session,))
        o.h.lifecycle.expire_rehearsals()
    else:
        o.h.store.db.execute("UPDATE sources SET revision=revision+1 WHERE id=?", (o.parent["id"],))
    release.set()
    result = await asyncio.wait_for(pending, 3)
    assert result["source_state"] == ("disabled" if change == "expiry" else "draft")
    assert "revoked" in result["reply"] or "parent registration changed" in result["reply"]
    assert o.posts == 0 and o.h.guild.create_text_channel.await_count == 0


async def test_shutdown_cancels_inflight_answer_and_restart_requires_explicit_resume(operator):
    o = operator
    session, result = await ready_to_create(o)
    entered = asyncio.Event()
    original = o.h.adapter.check_credentials

    async def check(config):
        entered.set()
        await asyncio.Event().wait()

    o.h.adapter.check_credentials = check
    pending = asyncio.create_task(call(o, "answer", session, sequence=6, text="CREATE"))
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(o.bridge.stop(), 2)
    with pytest.raises((ValueError, OSError)):
        await pending
    assert not o.bridge.requests and not o.h.store.db.in_transaction
    assert o.bridge.status(session)["last_step_state"] == "uncertain"
    o.h.adapter.check_credentials = original
    await o.bridge.start()
    with pytest.raises(ValueError, match="uncertain"):
        await call(o, "answer", session, sequence=6, text="CREATE")
    assert (await call(o, "resume", session))["last_step_state"] == "complete"
    assert o.posts == 0
    # Resume presents the current draft prompt; a new explicit CREATE is required.
    assert (await call(o, "answer", session, sequence=7, text="CREATE"))["source_state"] == "provisional"
    assert o.posts == 1


async def test_reopened_database_retains_steps_parent_binding_and_local_activation_notices(operator):
    from src.core.alert_channels import AlertStore

    o = operator
    session, result = await complete(o)
    await o.h.alerts.worker.once()
    await o.h.alerts.worker.once()
    await o.h.lifecycle.deliver_notices()
    before = await call(o, "status", session)
    await o.bridge.stop()
    o.h.store.close()
    o.h.store = AlertStore(o.root / "state")
    for component in (o.h.alerts, o.h.lifecycle, o.h.alerts.worker, o.h.alerts.transport, o.h.adapter):
        component.store = o.h.store
    o.bridge = OperatorRehearsal(o.h.alerts, o.root / "state", o.key)
    o.h.alerts.operator = o.bridge
    o.h.lifecycle.expire_rehearsals = o.bridge.expire
    await o.bridge.start()
    assert (await call(o, "status", session)) == before
    replay = await call(o, "answer", session, sequence=6, text="CREATE")
    assert replay["replayed"] and replay["source_state"] == "active" and o.posts == 1


@pytest.mark.parametrize("body", [{"operation": "delete"}, {"operation": "status", "sql": "DROP TABLE sources"}])
async def test_authenticated_bridge_rejects_unlisted_operations_and_fields(operator, body):
    o = operator
    session, _ = await start(o)
    with pytest.raises(ValueError, match="Invalid operator rehearsal request"):
        await asyncio.to_thread(o.cli.exchange, o.bridge.path, proof_key(o.key), {**body, "session": session})
    assert o.h.store.get(o.parent["id"])["state"] == "active"


def test_key_fifo_and_oversized_whitespace_refuse_without_blocking(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(PermissionError):
        proof_key(fifo)
    key = tmp_path / "key"
    key.write_text("x" + " " * 4096)
    key.chmod(0o600)
    with pytest.raises(PermissionError):
        proof_key(key)


async def test_operator_startup_is_opt_in_and_failure_does_not_disable_monitoring(operator):
    o = operator
    a = o.h.alerts
    # Reuse only synthetic objects; this tests the running service's startup wiring.
    await o.bridge.stop()
    a.credentials.start = AsyncMock()
    a.operator.start = AsyncMock(side_effect=PermissionError("synthetic refused key"))
    a.lifecycle.reconcile = AsyncMock()
    a.worker.run = AsyncMock()
    a.lifecycle.run = AsyncMock()
    await a.start("one")
    a.operator.start.assert_not_awaited()
    await asyncio.gather(a.task, a.lifecycle_task)
    a.task = None
    a.config["operator_rehearsal"] = True
    await a.start("one")
    a.operator.start.assert_awaited_once()
    await asyncio.gather(a.task, a.lifecycle_task)
    assert "one" in a.worker.accounts
