"""Three defects a fresh Linux VPS install hit, and the shell-profile trap.

None of these reproduce on the developer's own box, which is why they survived:
a hardened systemd unit, a non-interactive shell and an all-tools agent are all
things a production install has and a dev checkout does not.
"""

import json
import types

import pytest

from src.core import schedules as sched
from src.core.agent_manager import AgentManager
from src.core.tools import tool


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    (tmp_path / "config").mkdir()
    return tmp_path


# --- 1. `tools: "all"` refused every tool on the dispatch path ---------------

@tool(name="vps_ping", description="test tool", category="test")
async def vps_ping(ctx, target: str = "") -> str:
    return f"pinged {target}"


def _bare_manager(tools):
    """A manager with only what _dispatch_tools touches, so the real method runs."""
    mgr = AgentManager.__new__(AgentManager)
    mgr.agent_configs = {"bot": {"tools": tools}}
    mgr.rate_limiter = None
    mgr.access_control = None
    mgr.hitl = None
    mgr.audit = None
    mgr.storage = None
    mgr.behavior_monitor = None
    mgr.alerter = None
    mgr.vault = None
    mgr._get_agent_memory = lambda agent_id: None
    return mgr


def _message():
    return types.SimpleNamespace(channel_id="c1", user_id="u1", raw=None)


@pytest.mark.asyncio
async def test_all_sentinel_is_not_a_substring_test():
    """`tool_name not in "all"` is three characters, not an allowlist.

    Every tool name failed that test, so an agent configured `tools: all`
    could never run a scheduled action or a trigger even though the CLI and
    MCP paths allowed it the same tool.
    """
    mgr = _bare_manager("all")
    results = await mgr._dispatch_tools(
        "bot", "s1", [{"name": "vps_ping", "arguments": '{"target": "x"}'}], None, _message())
    assert results[0]["content"] == "pinged x"
    assert "not available" not in results[0]["content"]


@pytest.mark.asyncio
async def test_explicit_allowlist_still_refuses_what_is_not_on_it():
    mgr = _bare_manager(["something_else"])
    results = await mgr._dispatch_tools(
        "bot", "s1", [{"name": "vps_ping", "arguments": "{}"}], None, _message())
    assert "not available to this agent" in results[0]["content"]


@pytest.mark.asyncio
async def test_explicit_allowlist_permits_a_listed_tool():
    mgr = _bare_manager(["vps_ping"])
    results = await mgr._dispatch_tools(
        "bot", "s1", [{"name": "vps_ping", "arguments": '{"target": "y"}'}], None, _message())
    assert results[0]["content"] == "pinged y"


# --- 2. the schedule store has to live somewhere writable -------------------

def test_store_is_written_under_data_not_the_overlay_root(overlay):
    """ProtectSystem=strict leaves the overlay ROOT read-only.

    Writes there failed silently, so `last_run` was never recorded and a
    `once` schedule re-fired on every tick, forever.
    """
    sched.set_enabled(True)
    assert (overlay / "data" / "schedules.json").exists()
    assert not (overlay / "schedules.json").exists()


def test_a_legacy_root_store_is_still_read(overlay):
    (overlay / "schedules.json").write_text(json.dumps(
        {"enabled": True, "schedules": [{"id": "s1", "enabled": True}]}))
    assert len(sched._load_doc()["schedules"]) == 1


def test_a_legacy_store_migrates_forward_on_the_next_save(overlay):
    (overlay / "schedules.json").write_text(json.dumps(
        {"enabled": True, "schedules": [{"id": "s1", "enabled": True}]}))
    sched.set_enabled(False)
    migrated = json.loads((overlay / "data" / "schedules.json").read_text())
    assert migrated["schedules"][0]["id"] == "s1"
    assert migrated["enabled"] is False


def test_the_current_store_wins_over_a_stale_legacy_one(overlay):
    (overlay / "schedules.json").write_text(json.dumps(
        {"enabled": True, "schedules": [{"id": "old", "enabled": True}]}))
    (overlay / "data").mkdir()
    (overlay / "data" / "schedules.json").write_text(json.dumps(
        {"enabled": True, "schedules": [{"id": "new", "enabled": True}]}))
    assert sched._load_doc()["schedules"][0]["id"] == "new"


# --- 3. an unwritable store must fire nothing, loudly -----------------------

def _once_due(overlay, now):
    (overlay / "data").mkdir(exist_ok=True)
    (overlay / "data" / "schedules.json").write_text(json.dumps({
        "enabled": True,
        "schedules": [{"id": "s1", "agent_id": "bot", "channel_id": "c1",
                       "enabled": True, "spec_type": "once", "spec": str(now - 1),
                       "last_run": 0, "run_count": 0}],
    }))


def test_a_once_schedule_that_fires_records_it(overlay):
    _once_due(overlay, 1_000_000)
    assert len(sched.due_schedules(1_000_000)) == 1
    doc = json.loads((overlay / "data" / "schedules.json").read_text())
    assert doc["schedules"][0]["enabled"] is False
    assert sched.due_schedules(1_000_000) == []


def test_an_unwritable_store_fires_nothing_and_logs(overlay, monkeypatch, caplog):
    """Fail CLOSED.

    The old code wrapped the save in contextlib.suppress(Exception), so a
    read-only store produced no log line and no error while the same `once`
    schedule fired every 30 seconds indefinitely. Firing work whose state
    cannot be recorded is what makes that loop.
    """
    _once_due(overlay, 1_000_000)

    def boom(doc):
        raise PermissionError("Read-only file system")

    monkeypatch.setattr(sched, "_save_doc", boom)
    with caplog.at_level("ERROR"):
        assert sched.due_schedules(1_000_000) == []
    assert "Read-only file system" in caplog.text


# --- 4. the export has to sit above the interactive guard -------------------

GUARDED_BASHRC = (
    "# ~/.bashrc\n"
    "\n"
    "# If not running interactively, don't do anything\n"
    "case $- in\n"
    "    *i*) ;;\n"
    "      *) return;;\n"
    "esac\n"
    "\n"
    "alias ll='ls -alF'\n"
)

BLOCK = "\n# kbots environment\nexport KBOTS_OVERLAY=/srv/kbots-overlay\n"


def _lines(text):
    return text.splitlines()


def _index_of(text, needle):
    return next(i for i, line in enumerate(_lines(text)) if needle in line)


def test_a_fresh_write_lands_above_the_guard(tmp_path):
    import setup as setup_mod
    p = tmp_path / ".bashrc"
    p.write_text(GUARDED_BASHRC)
    setup_mod._write_profile_block(p, BLOCK)
    text = p.read_text()
    assert _index_of(text, "export KBOTS_OVERLAY") < _index_of(text, "case $- in")


def test_a_profile_with_no_guard_is_appended_to(tmp_path):
    import setup as setup_mod
    p = tmp_path / ".zshrc"
    p.write_text("alias ll='ls -alF'\n")
    setup_mod._write_profile_block(p, BLOCK)
    assert p.read_text().endswith(BLOCK)


def test_the_written_block_is_verbatim_so_undo_still_matches(tmp_path):
    import setup as setup_mod
    p = tmp_path / ".bashrc"
    p.write_text(GUARDED_BASHRC)
    setup_mod._write_profile_block(p, BLOCK)
    setup_mod._strip_profile_block(p, BLOCK)
    assert "KBOTS_OVERLAY" not in p.read_text()


def test_an_already_installed_host_gets_its_export_moved_up(tmp_path):
    """Setup's idempotency check is "is KBOTS_OVERLAY in the file".

    Without a migration branch, every host installed before the fix keeps its
    export stranded below the guard forever and re-running setup leaves it
    there while reporting success.
    """
    import setup as setup_mod
    p = tmp_path / ".bashrc"
    p.write_text(GUARDED_BASHRC + BLOCK)
    assert setup_mod._relocate_profile_block(p) is True
    text = p.read_text()
    assert _index_of(text, "export KBOTS_OVERLAY") < _index_of(text, "case $- in")
    assert text.count("export KBOTS_OVERLAY") == 1
    assert "alias ll='ls -alF'" in text


def test_relocation_is_a_no_op_once_the_export_is_already_above(tmp_path):
    import setup as setup_mod
    p = tmp_path / ".bashrc"
    p.write_text(GUARDED_BASHRC)
    setup_mod._write_profile_block(p, BLOCK)
    before = p.read_text()
    assert setup_mod._relocate_profile_block(p) is False
    assert p.read_text() == before


def test_the_ps1_guard_form_is_recognised_too(tmp_path):
    import setup as setup_mod
    p = tmp_path / ".bashrc"
    p.write_text('[ -z "$PS1" ] && return\n\nalias ll=\'ls -alF\'\n')
    setup_mod._write_profile_block(p, BLOCK)
    text = p.read_text()
    assert _index_of(text, "export KBOTS_OVERLAY") < _index_of(text, "$PS1")


# --- 5. an unset variable is not evidence of an engine-local install --------

def test_overlay_is_detected_from_the_service_unit(tmp_path, monkeypatch):
    """Answering "y" to the old prompt wrote secrets the service never reads."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "vault_manage", str(__import__("pathlib").Path(__file__).parent.parent / "vault-manage.py"))
    vm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vm)

    monkeypatch.delenv("KBOTS_OVERLAY", raising=False)
    home = tmp_path / "home"
    (home / ".config/systemd/user").mkdir(parents=True)
    (home / ".config/systemd/user/kbots.service").write_text(
        "[Service]\nEnvironment=KBOTS_OVERLAY=/srv/kbots-overlay\n")
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: home))

    found = vm.detect_overlay()
    assert found is not None
    assert found[0] == "/srv/kbots-overlay"


def test_overlay_is_detected_from_the_shell_profile_a_stale_shell_never_read(
        tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "vault_manage", str(__import__("pathlib").Path(__file__).parent.parent / "vault-manage.py"))
    vm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vm)

    monkeypatch.delenv("KBOTS_OVERLAY", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text(GUARDED_BASHRC + BLOCK)
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: home))

    found = vm.detect_overlay()
    assert found == ("/srv/kbots-overlay", "~/.bashrc")


# --- 5. The generated systemd unit (2026-08-24, second Linux bring-up) -------
#
# Four defects from provisioning a fresh Debian host. Two were already fixed
# (the state files moved under data/, the shell export moved above the guard);
# these are the two that were not, plus one the report did not reach because
# the service never stayed up long enough to try an agent-authored tool.

import pathlib  # noqa: E402

import setup as setup_wizard  # noqa: E402

TEMPLATE = pathlib.Path(__file__).resolve().parents[1] / "config" / "kbots.service"
RESCUE = pathlib.Path(__file__).resolve().parents[1] / "config" / "kbots-rescue.service"


def _render(tmp_path, unit=None):
    return setup_wizard.render_service_unit(
        (unit or TEMPLATE).read_text(), tmp_path / "overlay",
        [f"Environment=KBOTS_OVERLAY={tmp_path / 'overlay'}"])


def test_no_percent_h_survives_into_the_installed_unit(tmp_path):
    """`%h` in a SYSTEM unit is the service manager's home, so it expands to
    /root and not to the User='s. The install died on
    `failed to create directory /root/.cache/uv: Permission denied`.
    """
    assert "%h" not in _render(tmp_path)


def test_home_is_the_service_accounts_not_the_installers(tmp_path):
    """Resolved from the unit's own User=, because setup is routinely run
    under sudo and Path.home() would then be root's.
    """
    rendered = _render(tmp_path)
    assert "Environment=HOME=" in rendered
    home = [ln for ln in rendered.splitlines() if ln.startswith("Environment=HOME=")][0]
    assert home.split("=", 2)[2] not in ("/root", ""), home
    assert "kbots" in home, "HOME does not belong to the User= account"


def test_the_credential_paths_follow_the_same_home(tmp_path):
    """The quiet half of the %h bug: Claude Code would look in
    /root/.claude/.credentials.json, so an authenticated service account reads
    as unauthenticated with no error anywhere.
    """
    rendered = _render(tmp_path)
    home = [ln for ln in rendered.splitlines()
            if ln.startswith("Environment=HOME=")][0].split("=", 2)[2]
    rw = [ln for ln in rendered.splitlines() if ln.startswith("ReadWritePaths=")][0]
    assert f"{home}/.claude" in rw and f"{home}/.cache" in rw


def test_the_rescue_unit_is_rendered_too(tmp_path):
    """It carries the same two lines and would strand the rescue service in
    exactly the state it exists to recover from.
    """
    assert "%h" not in _render(tmp_path, RESCUE)


def test_agent_authored_tools_and_skills_are_writable(tmp_path):
    """create_tool writes a .py into <overlay>/tools and tool_scope keeps its
    sidecar there; create_skill writes into <overlay>/skills. Neither was in
    ReadWritePaths, so both fail under the sandbox and work perfectly on a Mac,
    which has none.
    """
    rw = [ln for ln in _render(tmp_path).splitlines()
          if ln.startswith("ReadWritePaths=")][0]
    assert f"{tmp_path / 'overlay' / 'tools'}" in rw
    assert f"{tmp_path / 'overlay' / 'skills'}" in rw


def test_every_writable_path_is_one_setup_creates(tmp_path):
    """systemd builds the mount namespace BEFORE exec, so a ReadWritePaths
    entry that does not exist fails at step NAMESPACE with status=226 and a
    message naming the path rather than the permission. The service then
    restart-loops without ever reaching the code that creates it lazily.
    """
    overlay = tmp_path / "overlay"
    rw = [ln for ln in _render(tmp_path).splitlines()
          if ln.startswith("ReadWritePaths=")][0].split("=", 1)[1].split()
    created = {str(d) for d in setup_wizard.service_writable_dirs(overlay)}
    for path in rw:
        if path == "/tmp" or "/.cache" in path or "/.claude" in path:
            continue   # always present, or owned by an account setup cannot touch
        assert path in created, f"{path} is granted but nothing creates it"


def test_the_overlay_root_stays_read_only(tmp_path):
    """The sandbox is right and the paths were wrong. Widening ReadOnlyPaths
    to make writes work would undo the design rather than fix the bug.
    """
    ro = [ln for ln in _render(tmp_path).splitlines()
          if ln.startswith("ReadOnlyPaths=")][0]
    assert str(tmp_path / "overlay") in ro


def test_the_overlay_env_injection_survives_the_rewrite(tmp_path):
    """The PATH line is where KBOTS_OVERLAY is injected. Rewriting that line
    without re-adding the block would leave the service with no overlay, which
    is the failure the injection exists to prevent.
    """
    assert f"Environment=KBOTS_OVERLAY={tmp_path / 'overlay'}" in _render(tmp_path)
