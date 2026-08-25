"""./setup — one command that brings a checkout up.

The README asked for four in sequence: install uv, clone, `uv sync`, then
`uv run python setup.py`. Skipping the third produced a traceback about a
missing module rather than "run uv sync", and the first is the one a fresh
machine is most likely to need.

These are static checks on the script. They cannot run it (it installs
software and then execs an interactive wizard), so they assert the properties
that were got wrong rather than the behaviour: it must not prune extras, it
must survive uv landing on a PATH the current shell has not got, and it must
not silently continue after a failed sync.
"""

import os
import stat
import subprocess

import pytest

from src.core.base import PROJECT_ROOT

LAUNCHER = PROJECT_ROOT / "setup"
SOURCE = LAUNCHER.read_text() if LAUNCHER.exists() else ""


def test_the_launcher_exists_and_is_executable():
    assert LAUNCHER.is_file(), "no ./setup at the repo root"
    assert os.stat(LAUNCHER).st_mode & stat.S_IXUSR, "./setup is not executable"


def test_it_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(LAUNCHER)]).returncode == 0


def test_it_runs_the_wizard():
    assert "setup.py" in SOURCE


def test_it_syncs_through_the_script_that_preserves_extras():
    """The bug this launcher shipped with for one run: a bare `uv sync` prunes
    every extra the deployment has installed. It removed the graph wheel and
    the test tooling from a working checkout on the first invocation.
    scripts/sync.sh exists to prevent exactly that, and says so in its header.
    """
    # Asserted on the COMMANDS, not on the text: a first version of this test
    # passed against a launcher that only mentioned sync.sh in a comment.
    commands = [ln.strip() for ln in SOURCE.splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
    assert any("bash scripts/sync.sh" in c for c in commands), (
        "scripts/sync.sh is never actually run")
    bare = [c for c in commands if c.startswith("uv sync")]
    assert len(bare) <= 1, f"more than one bare `uv sync`: {bare}"


def test_it_installs_uv_when_missing():
    """The launcher cannot be written in the thing it installs, which is why
    this is bash and not a uv script.
    """
    assert "astral.sh/uv/install.sh" in SOURCE


def test_it_survives_uv_landing_outside_the_current_path():
    """uv installs into ~/.local/bin, which the installing shell usually does
    not have on PATH yet. Without this, `uv: command not found` lands one line
    after a successful install.
    """
    assert ".local/bin/env" in SOURCE or ".local/bin" in SOURCE


def test_a_failed_sync_stops_rather_than_running_the_wizard():
    """A wizard run against half-installed dependencies fails somewhere deep
    with an error that names neither the cause nor the fix.
    """
    sync_at = SOURCE.index("sync.sh")
    wizard_at = SOURCE.index("exec uv run python setup.py")
    assert "die" in SOURCE[sync_at:wizard_at], "no failure exit between sync and wizard"


def test_it_execs_the_wizard_so_ctrl_c_and_the_exit_code_pass_through():
    """Without exec, Ctrl-C is caught by the launcher and the wizard's exit
    code is replaced by the launcher's.
    """
    assert "exec uv run python setup.py" in SOURCE


def test_it_works_from_any_directory():
    """`./setup` is typed from the repo root, but a full path from elsewhere
    has to behave the same or it silently syncs the wrong project."""
    assert 'cd "$(dirname "$0")"' in SOURCE


@pytest.mark.parametrize("doc", ["README.md"])
def test_the_docs_tell_people_to_use_it(doc):
    """A launcher nobody is told about is a file, not a feature."""
    text = (PROJECT_ROOT / doc).read_text()
    assert "./setup" in text, f"{doc} still sends people the long way round"
