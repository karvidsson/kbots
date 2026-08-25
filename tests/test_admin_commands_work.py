"""Admin commands that reported success and did nothing.

From a slash-command audit on a fresh Linux VPS, 2026-08-25. Every finding is a
command that Discord registers, /help lists, and the user believes: the failure
is not an error message, it is a confirmation.

  /admin reboot   `sudo systemctl restart` under NoNewPrivileges=true, fired
                  through an unpolled Popen. Told the user "back in ~3 seconds"
                  every time, and never restarted anything.
  /admin update   `git pull` inside an engine root the unit mounts read-only.
  /admin restart  resolved the agent, validated it, then did nothing.
  /admin pause    "not yet implemented"
  /admin resume   "not yet implemented"

The audit's own note is the one worth keeping: these are the documented way to
bring a newly provisioned bot online, so the flow that most needs to work was
the one that silently did not.
"""

import asyncio

import pytest

from src.core import restart
from src.core.base import PROJECT_ROOT

SOURCE = (PROJECT_ROOT / "src" / "connectors" / "discord.py").read_text()


# --- /admin reboot ---

def test_the_restart_never_shells_out_to_sudo():
    """The unit sets NoNewPrivileges=true, which blocks setuid escalation
    outright: sudo cannot become root whatever sudoers says. Both halves of
    that conflict ship in this repo.
    """
    # The COMMAND, not the word: the comment explaining the fix is allowed to
    # name sudo, and naming it is the point.
    block = SOURCE.split('name="reboot"')[1][:1500]
    assert '"sudo"' not in block and "subprocess" not in block


def test_a_supervised_process_restarts_by_exiting(monkeypatch):
    """systemd has Restart=always and launchd has KeepAlive, so exiting is a
    restart, needs no privileges, and needs no platform branch.
    """
    monkeypatch.setattr(restart.os, "getppid", lambda: 1)
    assert restart.can_restart()
    with pytest.raises(SystemExit) as exc:
        asyncio.run(restart.restart_self(delay=0))
    assert exc.value.code == 0, "a non-zero exit makes a normal restart look like a crash"


def test_an_unsupervised_process_refuses_and_says_why(monkeypatch):
    """Started by hand from a shell, exiting would just stop the engine. The
    honest answer is to decline, not to quietly kill it.
    """
    monkeypatch.setattr(restart.os, "getppid", lambda: 4242)
    assert not restart.can_restart()
    msg = restart.restart_message()
    assert "NOT restarting" in msg and "supervisor" in msg
    with pytest.raises(RuntimeError):
        asyncio.run(restart.restart_self(delay=0))


def test_the_message_matches_what_will_actually_happen(monkeypatch):
    """The original said "back in ~3 seconds" unconditionally. The message is
    now derived from the same check that decides whether to exit, so the two
    cannot disagree.
    """
    monkeypatch.setattr(restart.os, "getppid", lambda: 1)
    assert "Restarting" in restart.restart_message()
    monkeypatch.setattr(restart.os, "getppid", lambda: 4242)
    assert "Restarting kbots (" not in restart.restart_message()


def test_systemd_is_named_when_it_is_the_supervisor(monkeypatch):
    monkeypatch.setattr(restart.os, "getppid", lambda: 1)
    monkeypatch.setenv("INVOCATION_ID", "abc123")   # set by systemd for every unit
    assert restart.supervisor() == "systemd"


# --- /admin update ---

def test_update_refuses_when_the_engine_root_is_read_only():
    """update.sh begins with `git pull` in the engine root, which a hardened
    unit mounts read-only on purpose. Refusing with the command that does work
    beats a 600-second run ending in a git error about a read-only filesystem,
    which reads like a broken repo rather than a sandbox.
    """
    assert "engine_root_writable" in SOURCE
    block = SOURCE.split('name="update"')[1][:2500]
    assert "self-deploy.sh" in block, "does not name the command that works"
    assert "read-only" in block


def test_writability_is_probed_by_actually_writing(monkeypatch, tmp_path):
    """os.access reads permission bits and cannot see a read-only MOUNT, which
    is exactly what the systemd sandbox imposes: the bits say writable and the
    write fails. So the check has to attempt the write it is asking about.

    Asserted on the attempt, not on the answer. A permission-bit version gives
    the same answer for a chmod fixture, which is the only kind of read-only a
    test can make, so an answer-only test cannot tell the two apart. It was
    written that way first and a mutant walked straight through it.
    """
    from pathlib import Path as _Path

    from src.core import base

    root = tmp_path / "engine"
    root.mkdir()
    monkeypatch.setattr(base, "PROJECT_ROOT", root)

    attempted = []
    real_touch = _Path.touch
    monkeypatch.setattr(_Path, "touch",
                        lambda self, *a, **k: (attempted.append(self), real_touch(self))[1])

    assert restart.engine_root_writable() is True
    assert attempted, "the probe never tried to write — a permission bit is not a mount"
    assert attempted[0].parent == root


def test_a_failed_write_reads_as_not_writable(monkeypatch, tmp_path):
    from pathlib import Path as _Path

    from src.core import base

    root = tmp_path / "engine"
    root.mkdir()
    monkeypatch.setattr(base, "PROJECT_ROOT", root)

    def refuse(self, *a, **k):
        raise OSError(30, "Read-only file system")
    monkeypatch.setattr(_Path, "touch", refuse)
    assert restart.engine_root_writable() is False


def test_the_probe_leaves_nothing_behind(monkeypatch, tmp_path):
    from src.core import base

    # A subdirectory: tmp_path already holds the autouse roster isolation.
    root = tmp_path / "engine"
    root.mkdir()
    monkeypatch.setattr(base, "PROJECT_ROOT", root)
    restart.engine_root_writable()
    assert list(root.iterdir()) == []


# --- shipped stubs ---

@pytest.mark.parametrize("name", ["restart", "pause", "resume"])
def test_the_stub_commands_are_no_longer_registered(name):
    """A command that exists and does nothing is worse than one that does not
    exist: the user stops looking for another way to do the thing.
    """
    assert f'@admin_group.command(name="{name}"' not in SOURCE


def test_nothing_answers_not_yet_implemented():
    """The string is allowed in the comment recording why they were removed;
    what must not exist is a handler that SENDS it.
    """
    sending = [ln for ln in SOURCE.splitlines()
               if "not yet implemented" in ln.lower()
               and not ln.strip().startswith("#")]
    assert sending == []


# --- the public deferral ---

def test_a_direct_command_skill_checks_admin_before_deferring():
    """Deferring first posts a public "thinking" bubble in-channel and only
    then denies, which tells the whole channel that someone tried.
    """
    body = SOURCE.split("async def _skill_cmd(")[1][:1400]
    gate = body.index("_is_admin")
    defer = body.index("response.defer()")
    assert gate < defer, "the deferral still happens before the admin check"


def test_the_denial_is_ephemeral():
    body = SOURCE.split("async def _skill_cmd(")[1][:1400]
    denial = body[body.index("_is_admin"):body.index("response.defer()")]
    assert "ephemeral=True" in denial
