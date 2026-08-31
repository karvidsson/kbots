"""Two structural gaps behind the same recurring sandbox failure.

Both were diagnosed by the agent maintaining the Proxmox VM template, from a
deploy-blocked report on a fresh VPS, 2026-08-26.

  1. `update.sh` and `self-deploy.sh` had no notion of a systemd unit. A fix to
     `config/kbots.service` could be committed, merged and pulled by every
     machine and still be running on none of them, because the only thing that
     ever rendered a unit was the setup wizard. Their words for it, and the
     reason this is worse than an open bug: "a deploy mechanism that can't
     update its own unit will keep producing 'fixed upstream, broken
     everywhere'."

  2. `setup.py` maintained the unit's writable list by hand while the engine
     decided independently where state goes. Four separate EROFS reports were
     one bug: application state written to a path the unit's writable list did
     not know about. None needed more privilege. They needed the two lists to
     agree.
"""

import importlib.util
import plistlib
import sys

import pytest

import setup
from src.core import base
from src.core.base import PROJECT_ROOT

SPEC = importlib.util.spec_from_file_location(
    "refresh_units", PROJECT_ROOT / "scripts" / "refresh_units.py")
refresh = importlib.util.module_from_spec(SPEC)
sys.modules["refresh_units"] = refresh
SPEC.loader.exec_module(refresh)

UPDATE_SH = (PROJECT_ROOT / "scripts" / "update.sh").read_text()
SELF_DEPLOY_SH = (PROJECT_ROOT / "scripts" / "self-deploy.sh").read_text()
UNIT_TEMPLATE = (PROJECT_ROOT / "config" / "kbots.service").read_text()


def _commands(script: str) -> list[str]:
    """Executable lines only. A comment naming the fix is not the fix — an
    earlier test in this repo passed against a script that only mentioned the
    command it was supposed to run.
    """
    return [ln.strip() for ln in script.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


# --- one writable list ------------------------------------------------------

def test_the_units_writable_paths_come_from_the_engines_own_list(tmp_path):
    """Not "the two lists match" — that a hand-maintained copy would also pass
    on the day it was written. Every overlay dir in the unit is one the engine
    named, sourced from it, so they cannot drift apart later.
    """
    granted = setup.service_writable_dirs(tmp_path)
    for d in base.overlay_writable_dirs(tmp_path):
        assert d in granted, f"{d} is writable per the engine but not per the unit"


def test_a_state_file_lands_inside_a_granted_directory(monkeypatch, tmp_path):
    """The invariant the four bug reports were each a violation of."""
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    path = base.overlay_state_path("anything.json")
    assert path.parent in setup.service_writable_dirs(tmp_path)


def test_moving_the_state_directory_cannot_move_it_outside_the_grant(monkeypatch, tmp_path):
    """The mutation the old code lost to. Point state at a new subdirectory and
    the unit follows, because both read the same constant — where before, the
    unit kept granting `data` and every write failed at runtime, on Linux only,
    days after the change.
    """
    monkeypatch.setattr(base, "OVERLAY_STATE_DIR", "state")
    monkeypatch.setattr(base, "OVERLAY_WRITABLE_SUBDIRS", ("state", "tools"))
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    assert base.overlay_state_path("x.json") == tmp_path / "state" / "x.json"
    assert tmp_path / "state" in setup.service_writable_dirs(tmp_path)


def test_the_rendered_unit_grants_every_directory_the_engine_writes(tmp_path):
    line = next(ln for ln in setup.render_service_unit(UNIT_TEMPLATE, tmp_path, []).splitlines()
                if ln.startswith("ReadWritePaths="))
    for d in base.overlay_writable_dirs(tmp_path):
        assert str(d) in line, f"{d} missing from {line}"


def test_the_wizard_creates_every_path_it_grants(tmp_path):
    """systemd builds the mount namespace before exec, so a granted path that
    does not exist fails at step NAMESPACE with status=226 and restart-loops
    without ever reaching the code that would have created it.
    """
    refresh.ensure_writable_dirs(tmp_path)
    for d in setup.service_writable_dirs(tmp_path):
        if d.is_relative_to(tmp_path):
            assert d.is_dir(), f"{d} is granted but nothing creates it"


# --- the deploy path ships unit changes -------------------------------------

@pytest.mark.parametrize("script,name", [(UPDATE_SH, "update.sh"),
                                         (SELF_DEPLOY_SH, "self-deploy.sh")])
def test_the_deploy_path_re_renders_the_unit(script, name):
    """The finding itself: neither script had a single systemd reference, so
    neither could ship a unit change no matter what the pull contained.
    """
    assert any("refresh_units.py" in c for c in _commands(script)), \
        f"{name} still pulls code and never touches the unit"


def test_the_refresh_runs_before_the_restart():
    """Order is the whole point. Re-rendering after the restart means the fix
    lands one deploy late — and looks like it landed on time.
    """
    body = SELF_DEPLOY_SH
    assert body.index("refresh_units.py") < body.index('log "restarting service"')


def test_a_changed_unit_forces_a_restart_that_update_would_have_skipped():
    """update.sh skips the restart when only skills/tools changed. A unit that
    is reloaded but never restarted into is not installed, so the changed exit
    status has to override that decision.
    """
    tail = UPDATE_SH.split("refresh_units.py")[-1][:400]
    assert f"{refresh.CHANGED})" in tail, "the changed status is not branched on"
    assert "NEED_RESTART=1" in tail


def test_a_changed_unit_is_not_treated_as_a_deploy_failure():
    """self-deploy rolls back on any non-zero, and "a unit changed" is the
    success case — rolling back on it would make every unit fix undeployable.
    """
    tail = SELF_DEPLOY_SH.split("refresh_units.py")[-1][:600]
    assert f'-ne {refresh.CHANGED} ' in tail, "the changed status is not exempted"
    assert "rollback" in tail


# --- what the refresh renders -----------------------------------------------

def test_it_carries_the_installs_own_values_forward():
    """Re-deriving them is how a refresh becomes a reconfiguration: a different
    shell finds a different uv and the unit quietly starts running a binary
    nobody chose.
    """
    unit = ("ExecStart=/opt/homebrew/bin/uv run --no-sync python -m src.main\n"
            "Environment=KBOTS_OVERLAY=/srv/overlay\n"
            "Environment=KBOTS_MODULES=/srv/mod\n"
            "Environment=HOME=/home/kbots\n")
    env_lines, uv_path = refresh.systemd_install_values(unit)
    assert uv_path == "/opt/homebrew/bin/uv"
    assert env_lines == ["Environment=KBOTS_OVERLAY=/srv/overlay",
                         "Environment=KBOTS_MODULES=/srv/mod"], \
        "HOME is re-derived from the unit's User=, not carried over"


def test_the_overlay_comes_from_the_unit_not_the_shell(tmp_path, monkeypatch):
    """$KBOTS_OVERLAY in the shell running a deploy is not necessarily the one
    the service runs with, and it is the service's the unit has to match.
    """
    unit = tmp_path / "kbots.service"
    unit.write_text("Environment=KBOTS_OVERLAY=/srv/real-overlay\n")
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path / "some-other-overlay"))
    assert str(refresh.resolve_overlay(unit)) == "/srv/real-overlay"


# --- refusals ----------------------------------------------------------------

def test_it_refuses_to_move_the_install_to_another_checkout(capsys):
    """Found in this script's own first dry-run, which offered to repoint a
    working install from ~/kbots at a development clone. WorkingDirectory is
    rendered from the checkout the script lives in, so a refresh run from the
    wrong tree ships whatever is checked out there, gated by nothing.
    """
    other = PROJECT_ROOT.parent / "not-this-checkout"
    assert refresh.assert_install_root(other) is False
    assert "REFUSING" in capsys.readouterr().out
    assert refresh.assert_install_root(refresh.ENGINE_ROOT) is True


def test_the_working_directory_is_read_back_out_of_the_unit():
    root = refresh.installed_engine_root(
        "WorkingDirectory=/opt/kbots\nExecStart=/usr/local/bin/uv run\n")
    assert str(root) == "/opt/kbots"


def test_a_hand_edited_live_unit_is_left_alone(tmp_path, monkeypatch, capsys):
    """install-systemd.sh makes /etc/systemd/system/kbots.service a SYMLINK
    into the overlay, so writing the overlay copy IS the install. `sed -i`
    replaces that symlink with a regular file and orphans the overlay copy.
    Overwriting the local edit to fix that is not this script's call.
    """
    overlay = tmp_path / "overlay"
    (overlay / "systemd").mkdir(parents=True)
    (overlay / "systemd" / "kbots.service").write_text(
        f"WorkingDirectory={refresh.ENGINE_ROOT}\n"
        f"Environment=KBOTS_OVERLAY={overlay}\n")
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))

    live = tmp_path / "etc" / "kbots.service"
    live.parent.mkdir()
    live.write_text("hand edited\n")

    # Redirect only the /etc path the function consults; everything else keeps
    # the real Path, so the rest of the function under test is unchanged.
    orig = refresh.Path
    monkeypatch.setattr(refresh, "Path",
                        lambda *a: live if a == ("/etc/systemd/system/kbots.service",)
                        else orig(*a))
    assert refresh.refresh_systemd(dry_run=True, reload=False) == 0
    out = capsys.readouterr().out
    assert "regular file" in out and "Not touching" in out
    assert live.read_text() == "hand edited\n"


def test_the_plist_reload_is_a_real_reload_not_a_kickstart():
    """`launchctl kickstart -k` restarts the LOADED job definition, so a plist
    edit it is supposed to apply is not applied. The distinction is invisible
    in the logs: the service restarts and reports healthy on the old unit.
    """
    src = (PROJECT_ROOT / "scripts" / "refresh_units.py").read_text()
    body = src.split("def refresh_launchd(")[1]
    assert '"bootout"' in body and '"bootstrap"' in body
    assert '"kickstart"' not in body


def test_a_plist_that_fails_to_load_is_rolled_back():
    """bootout leaves the service genuinely down. If the new plist does not
    load, the box must not be left with nothing running.
    """
    src = (PROJECT_ROOT / "scripts" / "refresh_units.py").read_text()
    body = src.split("def refresh_launchd(")[1]
    restore = body.index("installed.write_text(previous)")
    assert restore < body.index("CRITICAL")
    assert body.count('"bootstrap"') >= 2, "nothing loads the restored plist"


def test_a_writable_path_it_adds_is_created_before_the_reload():
    """The other half of the status=226 crash loop: a refresh that widens
    ReadWritePaths without creating the new directory ships that crash itself.
    """
    src = (PROJECT_ROOT / "scripts" / "refresh_units.py").read_text()
    body = src.split("def refresh_systemd(")[1].split("def refresh_launchd(")[0]
    assert body.index("ensure_writable_dirs") < body.index("daemon-reload")


# --- the renderer is shared -------------------------------------------------

def test_the_wizard_and_the_refresh_render_through_the_same_function():
    """Two renderers is the same class of bug one layer up: they agree the day
    they are written and diverge quietly afterwards.
    """
    wizard = setup.__dict__["step_systemd"].__code__.co_names
    assert "build_service_unit" in wizard
    assert "build_service_unit" in refresh.refresh_systemd.__code__.co_names


def test_the_launchd_plist_renders_from_the_same_builder(tmp_path):
    rendered = setup.build_launchd_plist(
        (PROJECT_ROOT / "config" / "kbots.launchd.plist").read_text(),
        tmp_path, "/opt/homebrew/bin/uv", tmp_path / "home", "/usr/bin", "")
    data = plistlib.loads(rendered.encode())
    assert data["EnvironmentVariables"]["KBOTS_OVERLAY"] == str(tmp_path)
    assert "KBOTS_MODULES" not in data["EnvironmentVariables"]
    assert data["ProgramArguments"][0] == "/opt/homebrew/bin/uv"


def test_modules_survive_the_round_trip(tmp_path):
    rendered = setup.build_launchd_plist(
        (PROJECT_ROOT / "config" / "kbots.launchd.plist").read_text(),
        tmp_path, "/uv", tmp_path, "/usr/bin", "/srv/mod")
    assert plistlib.loads(rendered.encode())[
        "EnvironmentVariables"]["KBOTS_MODULES"] == "/srv/mod"
