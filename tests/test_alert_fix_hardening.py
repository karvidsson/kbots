"""The sandbox and controller boundary under detached children and path races."""

import asyncio
import json
import os
import platform
import time
from pathlib import Path

import pytest

from src.core.alert_channels import AlertError
from src.core.alert_fix_files import read_bytes, write_bytes
from src.core.alert_fix_sandbox import FixSandbox
from src.core.alert_fix_worker import AlertFixWorker
from src.core.alert_fixer import safe_file
from tests.test_alert_evidence_wait import delayed as delayed_fixture
from tests.test_alert_fix_execution import workspace as workspace_fixture
from tests.test_alert_fix_jobs import delivered
from tests.test_alert_fix_jobs import setup as setup_fixture

workspace = workspace_fixture
setup = setup_fixture
delayed = delayed_fixture


@pytest.mark.parametrize(
    "name",
    [
        "dist/output.py",
        "coverage/file.ts",
        ".nuxt/file.ts",
        ".output/file.ts",
        "node_modules/file.js",
        "DIST/file.py",
        "Coverage/file.ts",
        "NODE_MODULES/file.js",
    ],
)
@pytest.mark.parametrize("write", [True, False])
def test_artifact_paths_refused_for_model_io(tmp_path, name, write):
    with pytest.raises(AlertError):
        safe_file(tmp_path, name, write=write)


def test_fd_walk_refuses_swapped_parent_and_final_symlink(tmp_path, monkeypatch):
    inside, outside = tmp_path / "inside", tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    (inside / "src").mkdir()
    (outside / "file.ts").write_text("protected")
    original = os.open
    swapped = False

    def race(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "src" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            (inside / "src").rmdir()
            (inside / "src").symlink_to(outside, target_is_directory=True)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race)
    with pytest.raises(AlertError, match="symlink"):
        write_bytes(inside, "src/file.ts", b"overwrite")
    assert (outside / "file.ts").read_text() == "protected"
    (inside / "file.ts").symlink_to(outside / "file.ts")
    for operation in (lambda: write_bytes(inside, "file.ts", b"overwrite"), lambda: read_bytes(inside, "file.ts")):
        with pytest.raises(AlertError, match="symlink"):
            operation()
    assert (outside / "file.ts").read_text() == "protected"


def test_symlink_artifact_directory_refused_before_gate(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "dist").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AlertError, match="symlink"):
        FixSandbox(root)
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS sandbox process acceptance")
def test_detached_swapper_is_killed_and_cannot_replace_artifact_root(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    child = r"""const fs=require('fs');
fs.writeFileSync('dist/child.pid',String(process.pid));
try { fs.renameSync('coverage','moved-coverage'); fs.symlinkSync(process.env.HOME,'coverage'); }
catch { fs.writeFileSync('dist/root-protected','yes'); }
setInterval(()=>fs.writeFileSync('dist/heartbeat',String(Date.now())),5);
"""
    parent = "const fs=require('fs'),cp=require('child_process');\n" + (
        "const c=cp.spawn(process.execPath,['-e',"
        + json.dumps(child)
        + "],{detached:true,stdio:'ignore'}); c.unref();\n"
        "const t=setInterval(()=>{if(fs.existsSync('dist/root-protected')&&fs.existsSync('dist/heartbeat'))"
        "{clearInterval(t);process.exit(0)}},10);\n"
        "setTimeout(()=>process.exit(2),5000);"
    )
    (root / "probe.cjs").write_text(parent)
    result = FixSandbox(root).run(["node", "probe.cjs"], timeout=8)
    assert result["exit_code"] == 0
    assert (root / "coverage").is_dir() and not (root / "coverage").is_symlink()
    pid = int((root / "dist/child.pid").read_text())
    before = (root / "dist/heartbeat").read_text()
    time.sleep(0.1)
    assert (root / "dist/heartbeat").read_text() == before
    # Reaped or zombie is harmless; no running detached process survives.
    import subprocess

    state = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
    assert not state or state.startswith("Z")


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS sandbox process acceptance")
def test_gate_cannot_obscure_inherited_identity_or_read_external_metadata(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    external = tmp_path / "outside"
    external.write_text("private")
    program = """import ctypes,os,sys
lib=ctypes.CDLL('/usr/lib/system/libsystem_sandbox.dylib'); error=ctypes.c_char_p()
assert lib.sandbox_init(b'(version 1)(deny default)',0,ctypes.byref(error)) != 0
try: os.stat(sys.argv[1])
except PermissionError: pass
else: raise AssertionError('external metadata was readable')
print('restrictions retained')
"""
    import sys

    result = FixSandbox(root).run([str(Path(sys.executable).resolve()), "-I", "-S", "-c", program, str(external)])
    assert result["exit_code"] == 0 and "restrictions retained" in result["output"]


async def test_cancelled_job_does_not_cancel_fix_worker(setup):
    o = setup
    worker = AlertFixWorker(o.h.store, o.h.manager, o.h.alerts.transport, o.root)
    worker.accounts.add("one")
    o.h.manager.active_turns = 0
    started = asyncio.Event()

    async def work(job):
        started.set()
        await asyncio.Event().wait()

    worker.execute = work
    await delivered(o)
    task = asyncio.create_task(worker.once())
    await started.wait()
    o.h.store.disable(o.source["id"])
    worker.cancel_source(o.source["id"])
    assert await task
    assert o.h.manager.active_turns == 0 and not worker.running
    assert o.h.store.db.execute("SELECT state FROM alert_fixes").fetchone()[0] == "failed"
    # The worker loop itself is still cancellable for normal service shutdown.
    loop = asyncio.create_task(worker.run())
    await asyncio.sleep(0)
    assert not loop.done()
    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop


async def test_worker_shutdown_keeps_job_lease_recoverable(setup):
    o = setup
    worker = AlertFixWorker(o.h.store, o.h.manager, o.h.alerts.transport, o.root)
    worker.accounts.add("one")
    o.h.manager.active_turns = 0
    started = asyncio.Event()

    async def work(job):
        started.set()
        await asyncio.Event().wait()

    worker.execute = work
    await delivered(o)
    task = asyncio.create_task(worker.once())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert o.h.store.db.execute("SELECT state FROM alert_fixes").fetchone()[0] == "running"
    assert o.h.manager.active_turns == 0


async def test_diagnosis_fallback_is_visible_even_when_model_omits_staleness(delayed, monkeypatch):
    o = delayed
    from src.core.alert_git import AlertRepository
    from tests.test_alert_setup_ux import visible_text

    def failed_fetch(*args, **kwargs):
        raise AlertError("offline transport failure")

    monkeypatch.setattr(AlertRepository, "fetch", failed_fetch)
    o.visible_at = 0
    assert await o.worker.once()
    row = o.h.store.db.execute("SELECT * FROM receipts").fetchone()
    receipt = o.h.store._receipt(row)
    assert receipt["success"] and receipt["source_stale"]
    await o.worker.once()  # Flush the durable result card outbox.
    assert "Local source snapshot; may be stale" in visible_text(o.room.messages[-1])
    prompt = str(o.provider.complete.call_args)
    assert "server/api/debug/boom.get.ts" in prompt and "may be stale" in prompt


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS sandbox process acceptance")
@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancellation_kill_detached_children(tmp_path, cancel):
    import threading

    root = tmp_path / "work"
    root.mkdir()
    child = "const fs=require('fs'); setInterval(()=>fs.writeFileSync('dist/heartbeat',String(Date.now())),5)"
    (root / "probe.cjs").write_text(
        "require('child_process').spawn(process.execPath,['-e',"
        + json.dumps(child)
        + "],{detached:true,stdio:'ignore'}).unref();"
        "setInterval(()=>{},1000);"
    )
    runner = FixSandbox(root)

    def cancel_when_running():
        for _ in range(200):
            if (root / "dist/heartbeat").exists():
                runner.cancelled.set()
                return
            time.sleep(0.01)

    thread = threading.Thread(target=cancel_when_running) if cancel else None
    if thread:
        thread.start()
    try:
        with pytest.raises(AlertError, match="cancelled|time budget"):
            runner.run(["node", "probe.cjs"], timeout=3 if cancel else 0.3)
    finally:
        if thread:
            thread.join()
    before = (root / "dist/heartbeat").read_text()
    time.sleep(0.05)
    assert (root / "dist/heartbeat").read_text() == before
    assert runner.clean


def test_artifact_identity_is_checked_before_model_write(workspace, tmp_path):
    w = workspace
    outside = tmp_path / "outside"
    outside.mkdir()
    (w.folder / "dist").rmdir()
    (w.folder / "dist").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AlertError, match="symlink"):
        w.write("handler.mjs", "overwrite")
    assert w.changed == set() and not list(outside.iterdir())


def test_baseline_regression_copy_refuses_symlinked_parent(workspace, tmp_path):
    w = workspace
    w.write("test/new.test.mjs", 'throw new Error("fixture")')
    w.write("handler.mjs", "export const label=value=>value;")
    outside = tmp_path / "outside"
    outside.mkdir()
    (w.baseline / "test").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AlertError, match="symlink"):
        w.check("test/new.test.mjs")
    assert not list(outside.iterdir())


def test_unverified_gate_cleanup_blocks_controller_writes(workspace, monkeypatch):
    from src.core.alert_fix_processes import GateProcesses

    def uncertain(self):
        raise AlertError("Gate process cleanup could not be verified")

    monkeypatch.setattr(GateProcesses, "terminate", uncertain)
    with pytest.raises(AlertError, match="cleanup could not"):
        workspace.runner.run(["node", "-e", "process.exit(0)"])
    with pytest.raises(AlertError, match="prevents further controller access"):
        workspace.write("handler.mjs", "overwrite")
    assert workspace.changed == set()


def test_integrity_hash_streams_large_assets_but_model_read_stays_bounded(workspace):
    import hashlib

    w = workspace
    data = b"asset bytes" * 500_000
    (w.folder / "large.json").write_bytes(data)
    assert w.fingerprint()["large.json"] == hashlib.sha256(data).hexdigest()
    with pytest.raises(AlertError, match="bounded"):
        w.read("large.json")


def test_process_identity_errors_fail_closed_instead_of_assuming_absence():
    import ctypes
    import errno
    from types import SimpleNamespace

    from src.core.alert_fix_processes import GateProcesses

    gate = object.__new__(GateProcesses)
    gate.flags, gate.allowed, gate.denied = 1, b"/allow", b"/deny"

    def unavailable(*args):
        ctypes.set_errno(errno.EPERM)
        return -1

    gate.sandbox = SimpleNamespace(sandbox_check=unavailable)
    with pytest.raises(AlertError, match="could not be inspected"):
        gate.belongs(123)


def test_case_alias_cannot_overwrite_gate_manifest(workspace):
    with pytest.raises(AlertError, match="casing|manifests"):
        workspace.write("Package.json", "{}")


async def test_cancelled_repair_waits_for_gate_thread_and_detached_cleanup(workspace, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from src.core.alert_fixer import repair
    from src.core.base import LLMResponse

    w = workspace
    child = "const fs=require('fs'); setInterval(()=>fs.writeFileSync('dist/heartbeat',String(Date.now())),5)"
    w.write(
        "probe.cjs",
        "require('child_process').spawn(process.execPath,['-e',"
        + json.dumps(child)
        + "],{detached:true,stdio:'ignore'}).unref();"
        "setInterval(()=>{},1000);",
    )
    w.check = lambda regression: w.runner.run(["node", "probe.cjs"], timeout=10)
    provider = SimpleNamespace(
        supports_tool_free=True,
        complete=AsyncMock(
            return_value=LLMResponse(content=json.dumps({"action": "check", "regression": "test/new.test.mjs"}))
        ),
    )
    manager = SimpleNamespace(
        agent_configs={"owner": {"llm": {"model": "fixture"}}},
        storage=None,
        defaults={},
        _apply_provider_override=lambda *args: None,
        _get_agent_llm=lambda *args: provider,
        _effective_model=lambda *args: "fixture",
    )
    task = asyncio.create_task(repair(manager, {"owner": "owner"}, w, {}, lambda: None, tmp_path))
    try:
        for _ in range(500):
            if (w.folder / "dist/heartbeat").exists():
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Fixture gate did not start")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    before = (w.folder / "dist/heartbeat").read_text()
    await asyncio.sleep(0.05)
    assert (w.folder / "dist/heartbeat").read_text() == before
    assert w.runner.clean
