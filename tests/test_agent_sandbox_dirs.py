"""Which directories an agent session can reach.

The permission allow-list and the working-directory boundary are separate
gates, and settings.json only spoke to the first. So `Read($KBOTS_TMP/**)` was
granted, the media tools wrote there, and every agent was refused permission to
open the file it had just produced. The tool reported success and a path; the
read was denied; nothing in the refusal named the cause.
"""

import json

import pytest

from src.core.base import agent_session_dirs
from src.llm.claude_code import _extra_dir_args


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    (tmp_path / "tmp" / "media").mkdir(parents=True)
    (tmp_path / "codex").mkdir()
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    monkeypatch.delenv("KBOTS_TMP", raising=False)
    return tmp_path


def test_every_agent_reaches_the_shared_temp_dir(overlay):
    """No per-agent configuration should be needed for an agent to open its own
    screenshot."""
    assert str(overlay / "tmp") in agent_session_dirs()


def test_every_agent_reaches_the_codex(overlay):
    """The startup context lists the shared documents and says to open the file
    when the work touches it, which is not followable if it is out of bounds."""
    assert str(overlay / "codex") in agent_session_dirs()


def test_the_overlay_root_is_not_granted(overlay):
    """It holds config/ (the encrypted vault), data/ (every agent's memory,
    turns and audit log) and agents/<other-agent>/. Widening to it is a change
    to the isolation model, not a path fix."""
    assert str(overlay) not in agent_session_dirs()


def test_per_agent_extra_dirs_are_still_honoured(overlay):
    repo = overlay / "repo"
    repo.mkdir()
    assert str(repo) in agent_session_dirs(extra_dirs=[str(repo)])


def test_a_deployment_can_add_directories_for_every_agent(overlay):
    extra = overlay / "skills"
    extra.mkdir()
    assert str(extra) in agent_session_dirs(configured=[str(extra)])


def test_a_missing_directory_is_dropped_rather_than_passed_on(overlay):
    """--add-dir on a path that is not there is refused, which would take the
    whole session down instead of just that one directory."""
    dirs = agent_session_dirs(extra_dirs=[str(overlay / "nope")])
    assert not any("nope" in d for d in dirs)
    assert str(overlay / "tmp") in dirs, "the rest must survive"


def test_the_same_directory_twice_is_passed_once(overlay):
    dirs = agent_session_dirs(extra_dirs=[str(overlay / "tmp")],
                             configured=[str(overlay / "tmp")])
    assert dirs.count(str(overlay / "tmp")) == 1


def test_kbots_tmp_wins_over_the_overlay_default(tmp_path, monkeypatch):
    """A hardened host overrides KBOTS_TMP; the grant has to follow it, or the
    override becomes the thing that breaks reading your own output."""
    elsewhere = tmp_path / "scratch"
    elsewhere.mkdir()
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    monkeypatch.setenv("KBOTS_TMP", str(elsewhere))
    assert str(elsewhere) in agent_session_dirs()


def test_an_engine_local_install_still_works(tmp_path, monkeypatch):
    monkeypatch.delenv("KBOTS_OVERLAY", raising=False)
    monkeypatch.setenv("KBOTS_TMP", str(tmp_path))
    assert agent_session_dirs() == [str(tmp_path)]


# --- what actually reaches the CLI -----------------------------------------

def test_the_cli_gets_add_dir_for_the_temp_dir_with_no_agent_config(overlay):
    args = _extra_dir_args(None)
    assert args[:2] == ["--add-dir", str(overlay / "tmp")]


def test_add_dir_args_pair_every_flag_with_a_path(overlay):
    repo = overlay / "repo"
    repo.mkdir()
    args = _extra_dir_args([str(repo)])
    assert args.count("--add-dir") == len(args) // 2
    assert all(args[i] == "--add-dir" for i in range(0, len(args), 2))


# --- the file a scaffolded agent gets --------------------------------------

def test_a_new_agent_settings_file_lists_the_same_directories(overlay):
    """The engine passes them via --add-dir; settings.json is what the same
    agent dir sees when it is opened by hand. The two must not disagree."""
    from src.core.agent_scaffold import scaffold_agent

    (overlay / "config").mkdir(exist_ok=True)
    (overlay / "agents").mkdir(exist_ok=True)
    scaffold_agent(overlay, "research", "Research Bot", "Finds things out",
                   tier="assistant", engine_root=overlay / "engine")

    settings = json.loads(
        (overlay / "agents" / "research" / ".claude" / "settings.json").read_text())
    granted = settings["permissions"]["additionalDirectories"]
    assert str(overlay / "tmp") in granted
    assert str(overlay / "codex") in granted
    assert str(overlay) not in granted
