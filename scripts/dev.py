#!/usr/bin/env python3
"""kbots dev harness — ephemeral test agent with one-command teardown.

Spin up a disposable test agent in an isolated instance (.dev/ workspace,
own data dir, own lock — a live install is untouched), chat with it in the
terminal, and have everything removed when you're done.

Usage:
    uv run python scripts/dev.py chat              # real Claude, auto-teardown on exit
    uv run python scripts/dev.py chat --mock       # offline mock LLM, zero quota
    uv run python scripts/dev.py chat --keep       # preserve .dev/ after exit
    uv run python scripts/dev.py chat --agent-prompt "You are testing feature X..."
    uv run python scripts/dev.py down              # force-stop + remove leftovers
    uv run python scripts/dev.py test              # pytest
    uv run python scripts/dev.py lint              # ruff
    uv run python scripts/dev.py check             # lint + tests
"""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEV_DIR = ROOT / ".dev"
OVERLAY = DEV_DIR / "overlay"
DATA_DIR = DEV_DIR / "data"
AGENT_NAME = "testbot"

sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from src.core.agent_scaffold import scaffold_agent  # noqa: E402

BOLD, DIM, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"


def _info(msg: str) -> None:
    print(f"  {DIM}{msg}{RESET}")


def _ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def build_workspace(mock: bool, agent_prompt: str | None) -> None:
    """Create (or refresh) the ephemeral .dev/ workspace."""
    for d in (OVERLAY / "config", OVERLAY / "agents", OVERLAY / "tools",
              OVERLAY / "skills", DATA_DIR):
        d.mkdir(parents=True, exist_ok=True)

    # config.dev.yaml — isolated data dir, REPL connector only
    config = {
        "kbots": {"name": "kbots Dev", "log_level": "info", "data_dir": str(DATA_DIR)},
        "connectors": {
            "repl": {"enabled": True},
            "discord": {"enabled": False},
            "telegram": {"enabled": False},
            "http": {"enabled": False, "port": 8080},
        },
        "defaults": {
            "llm": {"provider": "mock" if mock else "claude_code", "model": "sonnet",
                    "max_tokens": 4096},
            "session": {"max_history": 50, "summarize_after": 30},
            "memory": {"backend": "sqlite", "semantic_search": False,
                       "decay_enabled": False, "max_results": 10},
        },
        "security": {
            "rate_limits": {"mode": "log", "defaults": {"max_per_hour": 1000}},
        },
    }
    (OVERLAY / "config" / "config.dev.yaml").write_text(
        yaml.dump(config, default_flow_style=False, sort_keys=False)
    )

    # Test agent — scaffolded exactly like a real one, routed to the REPL
    scaffold_agent(
        OVERLAY,
        AGENT_NAME,
        "TESTBOT",
        "Disposable dev-harness agent for testing features",
        model="sonnet",
        tier="coordinator",
        personality="concise; you are a throwaway test agent used to verify engine features",
        routing={"repl": {"channels": []}},
        engine_root=ROOT,
        agents_file="agents.dev.yaml",
        exist_ok=True,
    )

    # Point the agent at the requested provider (scaffold defaults to claude_code)
    agents_path = OVERLAY / "config" / "agents.dev.yaml"
    agents_cfg = yaml.safe_load(agents_path.read_text())
    agents_cfg["agents"][AGENT_NAME]["llm"]["provider"] = "mock" if mock else "claude_code"
    agents_path.write_text(yaml.dump(agents_cfg, default_flow_style=False, sort_keys=False))

    if agent_prompt:
        (OVERLAY / "agents" / AGENT_NAME / "CLAUDE.md").write_text(agent_prompt + "\n")

    _ok(f"Workspace: {DEV_DIR}  (provider: {'mock — offline' if mock else 'claude_code'})")


def run_engine() -> int:
    """Run the engine in the foreground with the dev profile."""
    env = {**os.environ, "KBOTS_OVERLAY": str(OVERLAY)}
    env.pop("KBOTS_MODULES", None)  # keep the dev instance layer-free
    env.pop("KBOTS_PROFILE", None)
    try:
        return subprocess.run(
            [sys.executable, "-m", "src.main", "--profile", "dev"],
            cwd=str(ROOT), env=env,
        ).returncode
    except KeyboardInterrupt:
        return 130


def teardown() -> None:
    if DEV_DIR.exists():
        shutil.rmtree(DEV_DIR, ignore_errors=True)
        _ok("Test agent stopped and removed (.dev/ deleted)")
    else:
        _info("Nothing to clean up")


def kill_dev_instance() -> None:
    """Kill any engine process running with the dev profile."""
    result = subprocess.run(
        ["pgrep", "-f", "src.main --profile dev"], capture_output=True, text=True
    )
    pids = [p for p in result.stdout.split() if p.strip() and int(p) != os.getpid()]
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGTERM)
            _ok(f"Stopped dev instance (pid {pid})")
        except ProcessLookupError:
            pass
    if pids:
        time.sleep(1)


def cmd_chat(args: argparse.Namespace) -> int:
    reused = DEV_DIR.exists()
    if reused:
        _info("Reusing existing .dev/ workspace (from a --keep session)")
    build_workspace(mock=args.mock, agent_prompt=args.agent_prompt)
    try:
        code = run_engine()
    finally:
        if args.keep:
            _info(f"Kept {DEV_DIR} — next 'chat' reuses it; 'down' removes it")
        else:
            teardown()
    return code


def cmd_down(args: argparse.Namespace) -> int:
    kill_dev_instance()
    teardown()
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    return subprocess.run(
        ["uv", "run", "--extra", "dev", "pytest", "-q", *args.extra], cwd=str(ROOT)
    ).returncode


def cmd_lint(args: argparse.Namespace) -> int:
    return subprocess.run(
        # "." rather than a path list — ruff honours pyproject excludes, so new
        # top-level dirs (extras/) are covered without editing this line.
        ["uv", "run", "--extra", "dev", "ruff", "check", "."],
        cwd=str(ROOT),
    ).returncode


def cmd_check(args: argparse.Namespace) -> int:
    print(f"{BOLD}── lint ──{RESET}")
    lint_rc = cmd_lint(args)
    print(f"{BOLD}── tests ──{RESET}")
    test_rc = subprocess.run(
        ["uv", "run", "--extra", "dev", "pytest", "-q"], cwd=str(ROOT)
    ).returncode
    if lint_rc == 0 and test_rc == 0:
        _ok("All checks passed")
        return 0
    print(f"  {YELLOW}!{RESET} lint rc={lint_rc}, tests rc={test_rc}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="dev.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_chat = sub.add_parser("chat", help="Spin up the test agent and chat (teardown on exit)")
    p_chat.add_argument("--mock", action="store_true", help="Use the offline mock LLM")
    p_chat.add_argument("--keep", action="store_true", help="Keep .dev/ after exit")
    p_chat.add_argument("--agent-prompt", help="Override the test agent's CLAUDE.md")
    p_chat.set_defaults(func=cmd_chat)

    p_down = sub.add_parser("down", help="Force-stop the dev instance and remove .dev/")
    p_down.set_defaults(func=cmd_down)

    p_test = sub.add_parser("test", help="Run the test suite")
    p_test.add_argument("extra", nargs="*", help="Extra pytest args")
    p_test.set_defaults(func=cmd_test)

    p_lint = sub.add_parser("lint", help="Run ruff")
    p_lint.set_defaults(func=cmd_lint)

    p_check = sub.add_parser("check", help="Lint + tests")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
