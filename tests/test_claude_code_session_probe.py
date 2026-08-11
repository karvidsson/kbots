"""Tests for ClaudeCodeProvider._session_file_exists — the pre-flight probe
that prevents hangs on --resume of sessions the CLI no longer has state for.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

from src.llm.claude_code import ClaudeCodeProvider


def test_probe_returns_true_for_existing_session_file():
    with tempfile.TemporaryDirectory() as home:
        cwd = Path("/opt/fake/agent")
        session_id = "abc-123"
        slug = str(cwd).replace("/", "-")
        session_file = Path(home) / ".claude" / "projects" / slug / f"{session_id}.jsonl"
        session_file.parent.mkdir(parents=True)
        session_file.write_text("")

        with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(Path(home) / ".claude")}):
            provider = ClaudeCodeProvider({})
            assert provider._session_file_exists(cwd, session_id) is True


def test_probe_returns_false_for_missing_session_file():
    with tempfile.TemporaryDirectory() as home:
        cwd = Path("/opt/fake/agent")
        with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(Path(home) / ".claude")}):
            provider = ClaudeCodeProvider({})
            assert provider._session_file_exists(cwd, "never-existed") is False


def test_probe_uses_claude_config_dir_env_var():
    with tempfile.TemporaryDirectory() as custom_dir:
        cwd = Path("/opt/fake/agent")
        session_id = "xyz-789"
        slug = str(cwd).replace("/", "-")
        session_file = Path(custom_dir) / "projects" / slug / f"{session_id}.jsonl"
        session_file.parent.mkdir(parents=True)
        session_file.write_text("")

        with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": custom_dir}):
            provider = ClaudeCodeProvider({})
            assert provider._session_file_exists(cwd, session_id) is True
