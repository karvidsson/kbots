"""Codex CLI provider — OpenAI's coding agent as a kbots LLM engine.

Each call spawns `codex exec --json` in the agent's project directory.
Codex reads AGENTS.md natively for identity/instructions (the same canonical
file Claude Code reaches through its CLAUDE.md stub), so agents keep one
identity across providers. Sessions persist via `codex exec resume <id>`.

MCP: codex has no per-project .mcp.json — servers live in ~/.codex/config.toml.
The agent's scaffolded .mcp.json is translated to `-c mcp_servers.*` config
overrides per invocation, so kbots-tools and mcp.yaml servers work without
touching the user's global codex config.

Config (under the agent's llm block or defaults.llm):
  provider: codex_cli
  model: gpt-5-codex          # optional — omitted -> codex default
  codex_bin: codex            # optional
  sandbox: workspace-write    # read-only | workspace-write | danger-full-access
  timeout: 600                # seconds per turn

Limitations (v1): per-tool allow/deny lists are not mapped — MCP exposure is
per-server; shell/file access is governed by the codex sandbox instead.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from src.core.base import LLMProvider, LLMResponse, Message
from src.llm.claude_code import build_cli_prompt

logger = logging.getLogger(__name__)

_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
# kbots effort levels -> codex model_reasoning_effort
_EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high",
               "xhigh": "xhigh", "max": "xhigh"}


def _toml_str(value: str) -> str:
    """A TOML basic string (json string quoting is valid TOML)."""
    return json.dumps(value)


def _toml_inline_table(d: dict) -> str:
    return "{" + ", ".join(f"{k} = {_toml_str(str(v))}" for k, v in d.items()) + "}"


def mcp_config_args(project_dir: Path) -> list[str]:
    """Translate the agent's .mcp.json into codex `-c mcp_servers.*` overrides.

    Codex stdio servers take command/args/env but no cwd — when .mcp.json
    pins one (kbots-tools runs from the engine root), wrap through /bin/sh.
    """
    mcp_file = Path(project_dir) / ".mcp.json"
    if not mcp_file.exists():
        return []
    try:
        servers = json.loads(mcp_file.read_text()).get("mcpServers", {})
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Unreadable .mcp.json in {project_dir}: {e}")
        return []

    args: list[str] = []
    for name, spec in servers.items():
        command = spec.get("command")
        if not command:
            continue  # url/http servers unsupported in v1
        cmd_args = [str(a) for a in spec.get("args", [])]
        cwd = spec.get("cwd")
        if cwd:
            shell_cmd = "cd " + _sh_quote(cwd) + " && exec " + " ".join(
                _sh_quote(c) for c in [command, *cmd_args])
            command, cmd_args = "/bin/sh", ["-c", shell_cmd]
        args.extend(["-c", f"mcp_servers.{name}.command = {_toml_str(command)}"])
        if cmd_args:
            args.extend(["-c", f"mcp_servers.{name}.args = {json.dumps(cmd_args)}"])
        env = spec.get("env")
        if env:
            args.extend(["-c", f"mcp_servers.{name}.env = {_toml_inline_table(env)}"])
    return args


def _sh_quote(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


class CodexCLIProvider(LLMProvider):
    """LLM provider that spawns the Codex CLI headless per turn."""
    name = "codex_cli"
    # The CLI loads the agent identity (AGENTS.md) from project_dir itself —
    # the engine must not inject it as a system message.
    reads_project_context = True

    def __init__(self, config: dict):
        super().__init__(config)
        self._codex_bin = config.get("codex_bin", "codex")
        self._default_model = config.get("model")  # None -> codex default
        self._sandbox = config.get("sandbox", "workspace-write")
        if self._sandbox not in _SANDBOX_MODES:
            raise ValueError(f"Invalid codex sandbox mode: {self._sandbox}")
        self._timeout = float(config.get("timeout", 600))

    async def complete(
        self,
        messages: list[Message],
        tools=None,
        stream: bool = False,
        **kwargs,
    ) -> LLMResponse:
        project_dir = kwargs.get("project_dir", ".")
        model = kwargs.get("model") or self._default_model
        session_id = kwargs.get("session_id")
        effort = kwargs.get("effort")
        agent_id = kwargs.get("agent_id")
        tag = f"[{agent_id}] " if agent_id else ""

        cwd = Path(project_dir).resolve()
        cwd.mkdir(parents=True, exist_ok=True)

        env = {**os.environ, **(kwargs.get("extra_env") or {})}

        # One retry: a stale/unknown session id drops resume and starts fresh.
        for resuming in ([True, False] if session_id else [False]):
            prompt = build_cli_prompt(messages, resuming=resuming)
            if not resuming:
                # No --system-prompt equivalent: a fresh session with an
                # explicit system message gets it inlined ahead of the prompt.
                system = next(
                    (m.content for m in messages
                     if getattr(m.role, "value", m.role) == "system" and m.content),
                    None)
                if system:
                    prompt = f"<system>\n{system}\n</system>\n\n{prompt}"
            args = self._build_args(
                cwd, model, effort, session_id if resuming else None, prompt)
            logger.debug(f"{tag}codex exec: cwd={cwd} model={model} "
                         f"resume={resuming} prompt_len={len(prompt)}")
            result = await self._run(args, cwd, env, tag)
            if result is not None:
                return result
            if resuming:
                logger.warning(
                    f"{tag}codex resume {session_id} failed — starting fresh")
        raise RuntimeError("codex exec failed (see logs for stderr)")

    def _build_args(self, cwd: Path, model, effort, session_id, prompt) -> list[str]:
        args = [self._codex_bin, "exec", "--json", "--skip-git-repo-check",
                "-s", self._sandbox]
        if model:
            args.extend(["-m", str(model)])
        mapped = _EFFORT_MAP.get(effort or "")
        if mapped:
            args.extend(["-c", f"model_reasoning_effort = {_toml_str(mapped)}"])
        args.extend(mcp_config_args(cwd))
        if session_id:
            args.extend(["resume", session_id])
        args.append(prompt if prompt else "Continue.")
        return args

    async def _run(self, args, cwd, env, tag) -> LLMResponse | None:
        """One codex exec invocation. None = retriable failure (resume drop)."""
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(cwd), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(
                f"codex exec timed out after {self._timeout:.0f}s")

        thread_id, content, tokens = self._parse_events(stdout, tag)
        if proc.returncode != 0 or content is None:
            err = (stderr or b"").decode(errors="replace").strip()
            if "login" in err.lower() or "auth" in err.lower():
                raise RuntimeError(
                    f"codex auth error — run `codex login` on the host: {err[:300]}")
            logger.warning(
                f"{tag}codex exec rc={proc.returncode}, "
                f"content={'yes' if content else 'no'}: {err[:300]}")
            return None
        return LLMResponse(
            content=content,
            tokens_used=tokens,
            model=str(args[args.index('-m') + 1]) if '-m' in args else "codex-default",
            stop_reason="stop",
            session_id=thread_id,
        )

    @staticmethod
    def _parse_events(stdout: bytes, tag: str) -> tuple[str | None, str | None, int | None]:
        """(thread_id, last agent message, total tokens) from JSONL events."""
        thread_id = content = None
        tokens = None
        for line in (stdout or b"").decode(errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "thread.started":
                thread_id = event.get("thread_id")
            elif etype == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    content = item["text"]
            elif etype == "turn.completed":
                usage = event.get("usage") or {}
                tokens = (usage.get("input_tokens", 0) or 0) + \
                         (usage.get("output_tokens", 0) or 0)
            elif etype in ("turn.failed", "error"):
                logger.warning(f"{tag}codex event {etype}: "
                               f"{json.dumps(event)[:300]}")
        return thread_id, content, tokens
