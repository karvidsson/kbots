import asyncio
import json
import platform
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.alert_channels import AlertError
from src.core.alert_fix_publish import FixPublisher
from src.core.alert_fix_sandbox import FixSandbox
from src.core.alert_fixer import FixWorkspace, repair, safe_file
from src.core.alert_git import AlertRepository, git
from src.core.base import LLMResponse
from tests.test_alert_fetched_source import commit, run
from tests.test_alert_fix_jobs import delivered
from tests.test_alert_fix_jobs import setup as setup_fixture

setup = setup_fixture


@pytest.fixture
def workspace(tmp_path):
    registered = tmp_path / "registered"
    registered.mkdir()
    run(registered, "init", "-q", "-b", "main")
    (registered / "package.json").write_text(
        json.dumps(
            {
                "name": "fixture",
                "type": "module",
                "scripts": {
                    "typecheck": "node -e \"console.log('typecheck fixture')\"",
                    "test:unit": "node test/new.test.mjs",
                },
            }
        )
    )
    (registered / "handler.mjs").write_text("export const label = value => value.trim();\n")
    head = commit(registered)
    dependencies = registered / "node_modules"
    (dependencies / ".bin").mkdir(parents=True)
    # A transport double for the Vitest command; the regression itself runs a
    # real Node assertion in the actual OS sandbox, before and after the patch.
    (dependencies / ".bin/vitest").write_text('#!/bin/sh\nexec node "$2"\n')
    (dependencies / ".bin/vitest").chmod(0o755)
    tree = {"repo": str(registered), "default_revision": head, "remote": "https://github.com/example/sample.git"}
    repository = AlertRepository(tmp_path / "data")
    folder = repository.checkout(tree, tmp_path / "after", "alert-fix/aabb-ccdd")
    before = repository.checkout(tree, tmp_path / "before", "alert-fix/aabb-ccdd")
    return FixWorkspace(folder, before, registered, tree)


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS OS-sandbox acceptance")
def test_real_regression_fails_before_and_passes_after_then_commit_is_bound(workspace):
    w = workspace
    w.write(
        "test/new.test.mjs",
        "import assert from 'node:assert/strict'; import {label} from '../handler.mjs';\n"
        "assert.doesNotThrow(()=>label(undefined)); assert.equal(label(undefined), '');\n",
    )
    w.write("handler.mjs", "export const label = value => typeof value === 'string' ? value.trim() : '';\n")
    result = w.check("test/new.test.mjs")
    assert result["passed"] and w.verified["before"]["exit_code"] != 0
    assert "AssertionError" in w.verified["before"]["output"]
    commit_id = w.commit("handle absent input")
    assert git(w.folder, "show", commit_id + ":handler.mjs") == (w.folder / "handler.mjs").read_bytes()
    assert (w.folder / "node_modules").is_symlink() is False


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS OS-sandbox acceptance")
def test_gate_failure_and_cross_file_mutation_cannot_be_committed(workspace):
    w = workspace
    w.write("test/new.test.mjs", "throw new Error('regression placeholder');\n")
    w.write("handler.mjs", "export const label = value => '';\n")
    w.before_runner.run = lambda command: {"command": command, "exit_code": 1, "output": "AssertionError"}
    w.runner.run = lambda command: {"command": command, "exit_code": 1, "output": "gate failed"}
    assert w.check("test/new.test.mjs")["passed"] is False
    with pytest.raises(AlertError, match="not passed"):
        w.commit("not verified")

    def mutate(command):
        (w.folder / "package.json").write_text("{}")
        return {"command": command, "exit_code": 0, "output": "passed"}

    w.runner.run = mutate
    with pytest.raises(AlertError, match="changed repair files"):
        w.check("test/new.test.mjs")


@pytest.mark.parametrize(
    "path",
    [
        "../outside.ts",
        "/tmp/outside.ts",
        ".github/workflows/task.yml",
        ".git/config",
        "node_modules/run.js",
        "scripts/gate.js",
        "vitest.config.ts",
    ],
)
def test_fixer_cannot_write_privileged_paths(tmp_path, path):
    with pytest.raises(AlertError):
        safe_file(tmp_path, path, write=True)


def test_symlink_escape_and_secret_files_are_not_readable(tmp_path):
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    for path in ("link/outside.ts", ".env", ".ssh/key"):
        with pytest.raises(AlertError):
            safe_file(tmp_path, path)


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS OS-sandbox acceptance")
def test_os_sandbox_denies_outside_read_write_network_and_git_metadata(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (root / ".git").mkdir()
    secret = tmp_path / "private.txt"
    secret.write_text("synthetic sentinel")
    outside = tmp_path / "outside.txt"
    program = """const fs=require('fs'),net=require('net'); let n=0;
for (const action of [()=>fs.readFileSync(process.argv[2]),()=>fs.writeFileSync(process.argv[3],'bad'),
()=>fs.writeFileSync('.git/config','bad')]) { try {action()} catch {n++} }
const c=net.connect(9,'127.0.0.1');c.on('error',()=>{console.log('denied',n+1);process.exit(n===3?0:1)});
"""
    (root / "probe.cjs").write_text(program)
    result = FixSandbox(root).run(["node", "probe.cjs", str(secret), str(outside)])
    assert result["exit_code"] == 0 and "denied 4" in result["output"]
    assert not outside.exists() and not (root / ".git/config").exists()


async def test_owning_provider_has_no_native_tools_and_only_mediated_actions(tmp_path):
    checked = []
    workspace = SimpleNamespace(
        inventory=["handler.ts"],
        folder=tmp_path,
        gates=["typecheck", "test:unit"],
        verified=None,
        read=lambda name: "untrusted source text",
        write=lambda *a: checked.append(a),
    )

    def check(name):
        workspace.verified = {"regression": name}
        return {"passed": True}

    workspace.check = check
    actions = [
        {"action": "read", "path": "handler.ts"},
        {"action": "write", "path": "handler.ts", "content": "fixed"},
        {"action": "check", "regression": "test/new.test.ts"},
        {"action": "finish", "cause": "missing guard", "fix": "check input"},
    ]
    provider = SimpleNamespace(
        supports_tool_free=True,
        complete=AsyncMock(side_effect=[LLMResponse(content=json.dumps(action)) for action in actions]),
    )
    manager = SimpleNamespace(
        agent_configs={"owner": {"llm": {"model": "fixture"}}},
        storage=None,
        defaults={},
        _get_agent_llm=lambda owner: provider,
        _effective_model=lambda *a: "fixture",
        _apply_provider_override=lambda *a: None,
    )
    result = await repair(
        manager,
        {"owner": "owner"},
        workspace,
        {"diagnosis": {"cause": "untrusted quoted evidence"}},
        lambda: None,
        tmp_path,
    )
    assert result["fix"] == "check input" and checked == [("handler.ts", "fixed")]
    for call in provider.complete.call_args_list:
        assert call.kwargs["tool_free"] is True and call.kwargs["tools"] is None
        assert call.kwargs["agent_id"] == "owner" and call.kwargs["session_id"] is None


def test_publisher_uses_exact_ready_pr_payload_and_never_merge(monkeypatch):
    from src.core import alert_fix_publish

    calls = []
    tree = {"identity": "example/sample", "default_branch": "main"}
    issue = str(uuid.uuid4())
    url = "https://eu.posthog.com/project/123/error_tracking/" + issue

    def api(endpoint, payload):
        calls.append((endpoint, payload))
        return {
            "number": 7,
            "html_url": "https://github.com/example/sample/pull/7",
            "state": "open",
            "draft": False,
            "merged_at": None,
            "head": {"ref": "alert-fix/aa-bb", "sha": "a" * 40},
        }

    monkeypatch.setattr(alert_fix_publish, "github", api)
    result = FixPublisher().create(
        tree,
        "alert-fix/aa-bb",
        issue,
        url,
        {"cause": "missing guard", "fix": "check input"},
        {"regression": "test/new.test.ts", "checks": [{"command": ["pnpm", "run", "typecheck"]}]},
    )
    assert result["number"] == 7 and calls[0][0] == "repos/example/sample/pulls"
    body = calls[0][1]
    assert body["draft"] is False and body["base"] == "main"
    assert url in body["body"] and "Opened automatically from alert " + issue in body["body"]
    assert "merge" not in body and "auto_merge" not in body


def test_pr_dedup_uses_paginated_list_not_search_index(monkeypatch):
    from src.core import alert_fix_publish

    calls = []
    url = "https://eu.posthog.com/project/123/error_tracking/" + str(uuid.uuid4())

    def api(endpoint):
        calls.append(endpoint)
        if endpoint.endswith("&page=1"):
            return [{"body": "unrelated"}] * 100
        return [{"body": url, "number": 7, "html_url": "https://github.com/example/sample/pull/7", "state": "open"}]

    monkeypatch.setattr(alert_fix_publish, "github", api)
    result = FixPublisher().find({"identity": "example/sample"}, "unused", url)
    assert result["number"] == 7 and len(calls) == 2 and all("search" not in c for c in calls)


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS OS-sandbox acceptance")
@pytest.mark.parametrize("failure", ["none", "lost_ack", "restart_push", "restart_post", "manual_no_frame"])
async def test_actual_fix_pipeline_tests_commits_and_edits_one_card(setup, workspace, monkeypatch, failure):
    from src.core.alert_fix_worker import AlertFixWorker

    o = setup
    o.source = o.h.store.update(o.source["id"], config={**o.source["config"], "repo": workspace.tree["repo"]})
    receipt = await delivered(o)
    test = (
        "import assert from 'node:assert/strict'; import {label} from '../handler.mjs';\n"
        "assert.doesNotThrow(()=>label(undefined)); assert.equal(label(undefined), '');\n"
    )
    actions = [
        {"action": "write", "path": "test/new.test.mjs", "content": test},
        {
            "action": "write",
            "path": "handler.mjs",
            "content": "export const label = value => typeof value === 'string' ? value.trim() : '';\n",
        },
        {"action": "check", "regression": "test/new.test.mjs"},
        {"action": "finish", "cause": "handle missing input", "fix": "guard before trim"},
    ]
    if failure == "manual_no_frame":
        actions.insert(0, {"action": "search", "query": "trim"})
    provider = SimpleNamespace(
        supports_tool_free=True, complete=AsyncMock(side_effect=[LLMResponse(content=json.dumps(a)) for a in actions])
    )
    manager = SimpleNamespace(
        agent_configs={"worker": {"llm": {"model": "fixture"}}},
        storage=None,
        defaults={},
        _get_agent_llm=lambda owner: provider,
        _effective_model=lambda *a: "fixture",
        _apply_provider_override=lambda *a: None,
        active_turns=0,
    )
    worker = AlertFixWorker(o.h.store, manager, o.h.alerts.transport, o.root / "fix-data")
    worker.accounts.add("one")
    tree = {**workspace.tree, "identity": "example/sample", "default_branch": "main"}
    worker.repositories.fetch = lambda *args: tree
    monkeypatch.setattr("src.core.alert_fix_worker.registered_remote", lambda p: (tree["remote"], tree["identity"]))
    remote, calls = {}, []
    loop = asyncio.get_running_loop()
    run_task = None

    def push(folder, snapshot, branch, sha):
        assert not o.h.store.db.in_transaction
        assert git(folder, "show", sha + ":handler.mjs").decode().startswith("export const label")
        remote.update(head=sha, branch=branch)
        calls.append("push")
        if failure == "restart_push" and calls.count("push") == 1:
            loop.call_soon_threadsafe(run_task.cancel)
            raise asyncio.CancelledError()

    def create(snapshot, branch, issue, url, report, proof):
        assert proof["before"]["exit_code"] != 0 and all(c["exit_code"] == 0 for c in proof["checks"])
        calls.append("create")
        remote["pr"] = {
            "number": 31,
            "url": "https://github.com/example/sample/pull/31",
            "state": "open",
            "draft": False,
            "merged": False,
            "head": remote["head"],
            "branch": branch,
        }
        if failure == "lost_ack":
            raise AlertError("synthetic lost acknowledgement")
        if failure == "restart_post":
            loop.call_soon_threadsafe(run_task.cancel)
            raise asyncio.CancelledError()
        return remote["pr"]

    worker.publisher.find = lambda *args: remote.get("pr")
    worker.publisher.push, worker.publisher.create = push, create
    if failure == "manual_no_frame":
        from src.connectors.alert_fix_controls import FixControls
        from tests.test_alert_fix_controls import interaction

        o.source = o.h.store.update(o.source["id"], config={**o.source["config"], "auto_fix_pr": False})
        receipt["fix_context"]["evidence"] = {}
        o.h.store.db.execute(
            "UPDATE receipts SET fix_context=? WHERE id=?", (json.dumps(receipt["fix_context"]), receipt["id"])
        )
        o.h.alerts.fixer = worker
        o.h.alerts.fix_controls = FixControls(o.h.alerts)
        o.h.alerts.transport.fix_controls = o.h.alerts.fix_controls
        assert not await worker.once()
        await o.h.alerts.fix_controls.view(o.source, receipt).children[0].callback(interaction(o))
    if failure.startswith("restart"):
        run_task = asyncio.create_task(worker.once())
        with pytest.raises(asyncio.CancelledError):
            await run_task
        from src.core.alert_channels import AlertStore

        o.h.store.close()
        o.h.store = AlertStore(o.h.directory)
        o.h.alerts.transport.store = o.h.store
        o.h.store.db.execute("UPDATE alert_fixes SET lease_until=0")
        worker = AlertFixWorker(o.h.store, manager, o.h.alerts.transport, o.root / "fix-data")
        worker.accounts.add("one")
        worker.publisher.find = lambda *args: remote.get("pr")
        worker.publisher.push, worker.publisher.create = push, create
        worker.repositories.fetch = lambda *args: pytest.fail("Persisted tree must survive restart")
    assert await worker.once()
    assert calls.count("create") == 1 and calls.count("push") == (2 if failure == "restart_push" else 1)
    assert provider.complete.call_count == len(actions)
    assert not await worker.once()
    assert manager.active_turns == 0 and len(o.room.messages) == 1
    from tests.test_alert_setup_ux import visible_text

    assert "PR #31 ready" in visible_text(o.room.messages[0])
    row = o.h.store.db.execute("SELECT * FROM alert_fixes").fetchone()
    assert row["state"] == "complete"


def test_in_tree_symlink_cannot_bypass_dependency_or_metadata_surface(tmp_path):
    for name in ("node_modules", ".git"):
        (tmp_path / name).mkdir()
        alias = tmp_path / ("alias" + name.replace(".", ""))
        alias.symlink_to(tmp_path / name, target_is_directory=True)
        with pytest.raises(AlertError, match="symlink"):
            safe_file(tmp_path, alias.name + "/gate.js", write=True)


def test_secret_shaped_repair_is_rejected_before_writing(workspace):
    with pytest.raises(AlertError, match="credential"):
        workspace.write("handler.mjs", 'export const key="' + "sk-" + "x" * 24 + '"')
    assert "handler.mjs" not in workspace.changed


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS OS-sandbox acceptance")
def test_gate_cannot_temporarily_replace_script_source_or_dependency(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (root / "node_modules").mkdir()
    (root / "package.json").write_text("{}")
    (root / "code.js").write_text("original")
    program = """const fs=require('fs');let n=0;
for(const p of ['package.json','code.js','node_modules/tool.js']) {
 try {fs.writeFileSync(p,'tampered')} catch {n++}
}
console.log('denied',n);process.exit(n===3?0:1);
"""
    (root / "probe.cjs").write_text(program)
    result = FixSandbox(root).run(["node", "probe.cjs"])
    assert result["exit_code"] == 0 and "denied 3" in result["output"]
    assert (root / "code.js").read_text() == "original"
